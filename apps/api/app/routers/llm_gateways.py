"""LLM gateways for the coding CLIs over HTTP (BOSSAPP-887).

  GET    /v1/llm-gateways?project_id=            the project's and its org's gateways (no keys)
  POST   /v1/llm-gateways                        add one: protocol, base URL, key (to the store)
  PATCH  /v1/llm-gateways/{gateway_id}           rename, re-point, rotate the key, enable/disable
  DELETE /v1/llm-gateways/{gateway_id}?project_id= remove it and its stored key
  POST   /v1/llm-gateways/{gateway_id}/test      one live call: GET <base>/v1/models

Reading is model_provider:read; every change and the live test is model_provider:manage,
attributed to the signed-in member (providerops_manage). A key goes IN and never comes back out:
answers say only whether one is set.
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


class CreateBody(Body):
    name: str = Field(min_length=1, max_length=80)
    protocol: str = Field(pattern="^(openai|anthropic)$")
    base_url: str = Field(min_length=8, max_length=500)
    secret: str = Field(min_length=1, max_length=4000, repr=False)
    scope: str = Field(default="project", pattern="^(project|org)$")
    harnesses: list[str] = Field(default_factory=list, max_length=10)


class PatchBody(Body):
    name: Optional[str] = Field(default=None, max_length=80)
    base_url: Optional[str] = Field(default=None, max_length=500)
    secret: Optional[str] = Field(default=None, max_length=4000, repr=False)
    harnesses: Optional[list[str]] = Field(default=None, max_length=10)
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")


def build_llm_gateways_router(*, engine, settings: Callable[[], Any],
                              require_reader: Callable[..., None],
                              require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import llm_gateway as gw

    router = APIRouter(tags=["llm-gateways"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except gw.GatewayError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _write(project_id: str, claimed: Optional[str], fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, project_id, claimed, "providerops_manage")
            return _errors(lambda: fn(c, actor))

    @router.get("/v1/llm-gateways")
    @permit(P.MODEL_PROVIDER_READ, scope=Scope.project(query="project_id"))
    def list_gateways(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            items = gw.list_gateways(c, project_id)
        return {"gateways": items, "protocols": list(gw.PROTOCOLS),
                "harness_protocol": dict(gw.HARNESS_PROTOCOL)}

    @router.post("/v1/llm-gateways", status_code=201)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def create_gateway(payload: CreateBody) -> dict:
        return {"gateway": _write(payload.project_id, payload.actor, lambda c, actor:
                                  gw.create_gateway(c, settings(), payload.project_id, actor=actor,
                                                    name=payload.name, protocol=payload.protocol,
                                                    base_url=payload.base_url,
                                                    secret=payload.secret, scope=payload.scope,
                                                    harnesses=payload.harnesses))}

    @router.patch("/v1/llm-gateways/{gateway_id}")
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def update_gateway(gateway_id: str, payload: PatchBody) -> dict:
        return {"gateway": _write(payload.project_id, payload.actor, lambda c, actor:
                                  gw.update_gateway(c, settings(), payload.project_id, gateway_id,
                                                    actor=actor, name=payload.name,
                                                    base_url=payload.base_url,
                                                    harnesses=payload.harnesses,
                                                    status=payload.status,
                                                    secret=payload.secret))}

    @router.delete("/v1/llm-gateways/{gateway_id}")
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(query="project_id"))
    def delete_gateway(gateway_id: str, project_id: str, actor: Optional[str] = None) -> dict:
        return _write(project_id, actor, lambda c, who: gw.delete_gateway(
            c, settings(), project_id, gateway_id, actor=who))

    @router.post("/v1/llm-gateways/{gateway_id}/test")
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def test_gateway(gateway_id: str, payload: Body) -> dict:
        return _write(payload.project_id, payload.actor, lambda c, actor: gw.live_test(
            c, settings(), payload.project_id, gateway_id, actor=actor))

    return router
