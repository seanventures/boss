"""/v1/instance: running the install, apart from any org's data (BOSSAPP-872).

    GET    /v1/instance/admins                 who the instance admins are (instance:read)
    POST   /v1/instance/admins                 {principal_id}: name a person one (instance:manage)
    DELETE /v1/instance/admins/{principal_id}  end it; never the last one (instance:manage)
    GET    /v1/instance/backups                the install's self-backups, newest first (instance:read)

Org Owner and Admin hold instance:* until the first instance admin is named; from then on only
instance admins do (identity/instance_admin.py). Naming and removing are audit events
(instance_admin.*).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, audit, permit


def _refuse(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _context(request: Request):
    try:
        from apps.api.app import identity_guard

        return identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
        return None


def build_instance_router(engine_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.identity import instance_admin

    router = APIRouter(tags=["instance"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    def _audit(request: Request, db: Any, event_type: str, principal_id: str) -> None:
        ctx = _context(request)
        if ctx is None:
            return
        audit.record(audit.SecurityEvent.from_context(
            ctx, event_type, audit.Outcome.SUCCESS, target_type="principal",
            target_id=principal_id, permission=P.INSTANCE_MANAGE.value, scope="org",
            details={}), db=db)

    def _actor(request: Request) -> str:
        ctx = _context(request)
        return ctx.principal.id if ctx is not None else "operator"

    @router.get("/v1/instance/admins")
    @permit(P.INSTANCE_READ, scope=Scope.org())
    def list_instance_admins() -> Any:
        with _engine().begin() as c:
            admins = instance_admin.list_admins(c)
        return {"admins": admins, "separated": bool(admins)}

    @router.post("/v1/instance/admins", status_code=201)
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def add_instance_admin(request: Request, payload: dict = Body(...)) -> Any:
        principal_id = str((payload or {}).get("principal_id") or "").strip()
        if not principal_id:
            return _refuse(422, "principal_id_required")
        try:
            with _engine().begin() as c:
                out = instance_admin.grant(c, principal_id, actor=_actor(request))
                _audit(request, c, "instance_admin.granted", principal_id)
        except instance_admin.InstanceAdminError as refused:
            return _refuse(409, "instance_admin_refused", str(refused))
        return out

    @router.delete("/v1/instance/admins/{principal_id}")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def remove_instance_admin(principal_id: str, request: Request) -> Any:
        try:
            with _engine().begin() as c:
                out = instance_admin.revoke(c, principal_id, actor=_actor(request))
                _audit(request, c, "instance_admin.revoked", principal_id)
        except instance_admin.InstanceAdminError as refused:
            return _refuse(409, "instance_admin_refused", str(refused))
        return out

    @router.get("/v1/instance/backups")
    @permit(P.INSTANCE_READ, scope=Scope.org())
    def list_instance_backups(limit: int = 50) -> Any:
        with _engine().begin() as c:
            return {"backups": instance_admin.list_backups(c, limit=limit)}

    return router
