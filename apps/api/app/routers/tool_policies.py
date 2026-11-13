"""Tool policies over HTTP (BOSSAPP-815/817): the ordered rule table, CRUD, and simulate.

  GET    /v1/tool-policies?project_id=&source=           the rules, in the order they are read
  POST   /v1/tool-policies                               write one (source=operator)
  PATCH  /v1/tool-policies/{policy_id}                   change one (priority, enable/disable…)
  DELETE /v1/tool-policies/{policy_id}?project_id=&actor= revoke one (kept for the audit)
  POST   /v1/tool-policies/simulate                      a hypothetical call, decided as a real
                                                         one would be; nothing is recorded

WHO MAY DO WHAT: reading and simulating is mcp_server:read; every change is mcp_server:review —
the same permission the per-tool Allow / Ask / Off switch takes — attributed to the signed-in
member (require_write_actor with pack_manage), never to a claimed actor. "Always allow" rules
(source=decision) are made from an approval (POST /v1/decisions/{id}/approve `always_allow`)
and listed and revoked here.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class RuleBody(Body):
    scope: str = Field(default="project", max_length=200)
    priority: int = 100
    effect: str
    selector: dict[str, Any] = Field(default_factory=dict)
    conditions: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    rate_limit: Optional[dict[str, Any]] = None
    schema_sha256: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default=None, max_length=2000)
    expires_at: Optional[str] = None
    status: str = "active"


class PatchBody(Body):
    scope: Optional[str] = Field(default=None, max_length=200)
    priority: Optional[int] = None
    effect: Optional[str] = None
    selector: Optional[dict[str, Any]] = None
    conditions: Optional[list[dict[str, Any]]] = Field(default=None, max_length=8)
    rate_limit: Optional[dict[str, Any]] = None
    schema_sha256: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default=None, max_length=2000)
    expires_at: Optional[str] = None
    status: Optional[str] = None


class SimulateBody(Body):
    server: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    role_key: Optional[str] = Field(default=None, max_length=200)
    workflow_id: Optional[str] = Field(default=None, max_length=200)
    run_id: Optional[str] = Field(default=None, max_length=200)
    tier: Optional[int] = Field(default=None, ge=0, le=3)


def build_tool_policies_router(*, engine, require_reader: Callable[..., None],
                               require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import tool_policy_engine as tpe

    router = APIRouter(tags=["tool-policies"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except tpe.ToolPolicyNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except tpe.ToolPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _write(payload: Body, fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "pack_manage")
            return _errors(lambda: fn(c, actor))

    @router.get("/v1/tool-policies")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def list_policies(project_id: str,
                      source: Optional[str] = Query(None, pattern="^(operator|decision)$"),
                      include_revoked: bool = False) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            rules = tpe.list_rules(c, project_id, source=source,
                                   include_revoked=include_revoked)
        return {"policies": [tpe.public(r) for r in rules],
                "expiry_choices": list(tpe.EXPIRY_CHOICES)}

    @router.get("/v1/tool-policies/suggestions")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def suggestions(project_id: str) -> dict:
        """The catalogue's default rules (rate limits first) for this project's catalogue
        servers that it does not have yet (BOSSAPP-868). Each is a body POST /v1/tool-policies
        takes as it is; nothing is applied until a person adds it."""
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"suggestions": tpe.suggested_rules(c, project_id),
                    "buckets": list(tpe.RATE_BUCKETS)}

    @router.post("/v1/tool-policies/simulate")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(body="project_id"))
    def simulate(payload: SimulateBody) -> dict:
        body = payload.model_dump(exclude={"project_id", "actor"})
        with begin(engine) as c:
            require_reader(c, payload.project_id)
            return _errors(lambda: tpe.simulate(c, payload.project_id, body))

    @router.post("/v1/tool-policies", status_code=201)
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def create_policy(payload: RuleBody) -> dict:
        fields = payload.model_dump(exclude={"project_id", "actor"})
        return {"policy": tpe.public(_write(payload, lambda c, actor: tpe.create_rule(
            c, payload.project_id, fields, actor=actor)))}

    @router.patch("/v1/tool-policies/{policy_id}")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def update_policy(policy_id: str, payload: PatchBody) -> dict:
        fields = payload.model_dump(exclude_unset=True, exclude={"project_id", "actor"})
        return {"policy": tpe.public(_write(payload, lambda c, actor: tpe.update_rule(
            c, payload.project_id, policy_id, fields, actor=actor)))}

    @router.delete("/v1/tool-policies/{policy_id}")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(query="project_id"))
    def revoke_policy(policy_id: str, project_id: str, actor: Optional[str] = None) -> dict:
        with begin(engine) as c:
            who = require_write_actor(c, project_id, actor, "pack_manage")
            rule = _errors(lambda: tpe.revoke_rule(c, project_id, policy_id, actor=who))
        return {"policy": tpe.public(rule)}

    return router
