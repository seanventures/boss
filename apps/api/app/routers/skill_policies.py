"""Skill-access policy over HTTP (BOSSAPP-890): the ordered rules, CRUD, and the simulator.

  GET    /v1/skill-policies?project_id=                 the project's and its org's rules, in order
  POST   /v1/skill-policies                             write one (scope project | org)
  PATCH  /v1/skill-policies/{rule_id}                   change one (position, enable/disable…)
  DELETE /v1/skill-policies/{rule_id}?project_id=       revoke one (kept for the audit)
  POST   /v1/skill-policies/simulate                    a hypothetical action, decided as a real
                                                        one would be; nothing is counted

WHO MAY DO WHAT: reading and simulating is skill:read; every change is skill:manage, attributed to
the signed-in member (require_write_actor with pack_manage), never to a claimed actor.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class RuleBody(Body):
    scope: str = Field(default="project", pattern="^(project|org)$")
    position: int = 100
    effect: str = Field(pattern="^(allow|deny)$")
    actions: list[str] = Field(default_factory=lambda: ["*"], max_length=9)
    skill_pattern: str = Field(default="*", max_length=200)
    role_pattern: str = Field(default="*", max_length=200)
    actor_kind: str = Field(default="any", pattern="^(any|person|agent)$")
    source_pattern: str = Field(default="*", max_length=200)
    min_risk_tier: Optional[int] = Field(default=None, ge=0, le=3)
    note: Optional[str] = Field(default=None, max_length=2000)


class PatchBody(Body):
    position: Optional[int] = None
    effect: Optional[str] = Field(default=None, pattern="^(allow|deny)$")
    actions: Optional[list[str]] = Field(default=None, max_length=9)
    skill_pattern: Optional[str] = Field(default=None, max_length=200)
    role_pattern: Optional[str] = Field(default=None, max_length=200)
    actor_kind: Optional[str] = Field(default=None, pattern="^(any|person|agent)$")
    source_pattern: Optional[str] = Field(default=None, max_length=200)
    min_risk_tier: Optional[int] = Field(default=None, ge=0, le=3)
    note: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")


class SimulateBody(Body):
    action: str = Field(min_length=1, max_length=20)
    skill_key: str = Field(min_length=1, max_length=200)
    role_key: Optional[str] = Field(default=None, max_length=200)
    actor_kind: str = Field(default="person", pattern="^(person|agent)$")
    source: Optional[str] = Field(default=None, max_length=500)
    risk_tier: Optional[int] = Field(default=None, ge=0, le=3)


def build_skill_policies_router(*, engine, require_reader: Callable[..., None],
                                require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import skill_policy as sp

    router = APIRouter(tags=["skill-policies"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except sp.SkillPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _write(payload: Body, fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "pack_manage")
            return _errors(lambda: fn(c, actor))

    @router.get("/v1/skill-policies")
    @permit(P.SKILL_READ, scope=Scope.project(query="project_id"))
    def list_policies(project_id: str, include_revoked: bool = False) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            rules = sp.list_rules(c, project_id, include_revoked=include_revoked)
        return {"rules": rules, "actions": list(sp.ACTIONS),
                "default": "allow (no rule matches)"}

    @router.post("/v1/skill-policies/simulate")
    @permit(P.SKILL_READ, scope=Scope.project(body="project_id"))
    def simulate(payload: SimulateBody) -> dict:
        body = payload.model_dump(exclude={"project_id", "actor"})
        with begin(engine) as c:
            require_reader(c, payload.project_id)
            return _errors(lambda: sp.simulate(c, payload.project_id, body))

    @router.post("/v1/skill-policies", status_code=201)
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def create_policy(payload: RuleBody) -> dict:
        fields = payload.model_dump(exclude={"project_id", "actor"})
        return {"rule": _write(payload, lambda c, actor: sp.create_rule(
            c, payload.project_id, fields, actor=actor))}

    @router.patch("/v1/skill-policies/{rule_id}")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def update_policy(rule_id: str, payload: PatchBody) -> dict:
        fields = payload.model_dump(exclude_unset=True, exclude={"project_id", "actor"})
        return {"rule": _write(payload, lambda c, actor: sp.update_rule(
            c, payload.project_id, rule_id, fields, actor=actor))}

    @router.delete("/v1/skill-policies/{rule_id}")
    @permit(P.SKILL_MANAGE, scope=Scope.project(query="project_id"))
    def revoke_policy(rule_id: str, project_id: str, actor: Optional[str] = None) -> dict:
        with begin(engine) as c:
            who = require_write_actor(c, project_id, actor, "pack_manage")
            rule = _errors(lambda: sp.revoke_rule(c, project_id, rule_id, actor=who))
        return {"rule": rule}

    return router
