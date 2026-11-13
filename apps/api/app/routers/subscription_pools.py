"""Multi-account pools over HTTP (BOSSAPP-886): several Claude or Codex subscriptions as one.

  GET    /v1/subscription-pools?project_id=                   pools, accounts, each one's headroom
  POST   /v1/subscription-pools                               a pool for one engine
  PATCH  /v1/subscription-pools/{pool_id}                     turn the pool on or off
  POST   /v1/subscription-pools/{pool_id}/members             add an account (its login → store)
  PATCH  /v1/subscription-pools/members/{member_id}           on/off, order, a fresh login
  DELETE /v1/subscription-pools/members/{member_id}?project_id= remove it and its stored login
  DELETE /v1/subscription-pools/{pool_id}?project_id=          delete an EMPTY pool (one with
                                                               accounts is refused: 422)

Reading is entitlement:read; every change is entitlement:manage, attributed to the signed-in
member (providerops_manage). A login goes IN and never comes back out.
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


class PoolBody(Body):
    engine: str = Field(pattern="^(claude_code|codex)$")
    name: str = Field(default="", max_length=80)


class PoolStatusBody(Body):
    status: str = Field(pattern="^(active|disabled)$")


class MemberBody(Body):
    label: str = Field(min_length=1, max_length=80)
    credential: str = Field(min_length=1, max_length=20000, repr=False)


class MemberPatchBody(Body):
    status: Optional[str] = Field(default=None, pattern="^(active|disabled|needs_reauth)$")
    position: Optional[int] = Field(default=None, ge=0, le=1000)
    credential: Optional[str] = Field(default=None, max_length=20000, repr=False)


def build_subscription_pools_router(*, engine, settings: Callable[[], Any],
                                    require_reader: Callable[..., None],
                                    require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import subscription_pools as sp

    router = APIRouter(tags=["subscription-pools"])

    def _write(project_id: str, claimed: Optional[str], fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, project_id, claimed, "providerops_manage")
            try:
                return fn(c, actor)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except sp.PoolError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/subscription-pools")
    @permit(P.ENTITLEMENT_READ, scope=Scope.project(query="project_id"))
    def list_pools(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"pools": sp.describe(c, project_id), "engines": list(sp.ENGINES)}

    @router.post("/v1/subscription-pools", status_code=201)
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def create_pool(payload: PoolBody) -> dict:
        return {"pool": _write(payload.project_id, payload.actor, lambda c, actor: sp.create_pool(
            c, payload.project_id, engine=payload.engine, name=payload.name, actor=actor))}

    @router.patch("/v1/subscription-pools/{pool_id}")
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def set_pool_status(pool_id: str, payload: PoolStatusBody) -> dict:
        return {"pool": _write(payload.project_id, payload.actor, lambda c, actor:
                               sp.set_pool_status(c, payload.project_id, pool_id,
                                                  status=payload.status, actor=actor))}

    @router.post("/v1/subscription-pools/{pool_id}/members", status_code=201)
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def add_member(pool_id: str, payload: MemberBody) -> dict:
        return {"member": _write(payload.project_id, payload.actor, lambda c, actor:
                                 sp.add_member(c, settings(), payload.project_id, pool_id,
                                               label=payload.label,
                                               credential=payload.credential, actor=actor))}

    @router.patch("/v1/subscription-pools/members/{member_id}")
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def update_member(member_id: str, payload: MemberPatchBody) -> dict:
        return {"member": _write(payload.project_id, payload.actor, lambda c, actor:
                                 sp.update_member(c, settings(), payload.project_id, member_id,
                                                  actor=actor, status=payload.status,
                                                  position=payload.position,
                                                  credential=payload.credential))}

    @router.delete("/v1/subscription-pools/members/{member_id}")
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(query="project_id"))
    def remove_member(member_id: str, project_id: str, actor: Optional[str] = None) -> dict:
        return _write(project_id, actor, lambda c, who: sp.remove_member(
            c, settings(), project_id, member_id, actor=who))

    @router.delete("/v1/subscription-pools/{pool_id}")
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(query="project_id"))
    def delete_pool(pool_id: str, project_id: str, actor: Optional[str] = None) -> dict:
        """BOSSAPP-1042: an empty pool goes; one with accounts is refused (422) so no stored
        login is ever orphaned — remove its accounts first, each with its login."""
        return _write(project_id, actor, lambda c, who: sp.delete_pool(
            c, project_id, pool_id, actor=who))

    return router
