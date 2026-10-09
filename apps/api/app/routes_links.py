"""/v1/apps/{app_key}/links and test users (F5, cross-app wiring). Owner: stream S5.

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §5.6 (and D7, D8, D9). The engine is
boss_engine.app_links.service; the gateway that enforces a link is apps/egress/boss_egress/links.py.

Permissions (the spec's "project:write" does not exist in the catalogue):
  list, test-user presence   project:read     a link shows no credential value
  create, revoke             app_link:manage  Owner, Admin, Operator: an operator who previews
                                              both apps is the person the spec's §5.1 story is
                                              about; a link can only hand out GENERATED test
                                              users of the same project's preview (D7, S5.3),
                                              so it is not a step-up or critical action
  reveal test users          app_secret:write step-up (D9); every reveal is an audit event

`app_key` in a path is the app's key, one of its run ids, or its name; `project_id` is required
(query for GET/DELETE, body otherwise): an app key is unique per project only. Creating and
revoking a link are audit events (app_link.created / app_link.revoked).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.identity import P, Scope, audit, permit

SPEC = "APP_TEMPLATES_AND_SECURITY_LOOP.md §5.6"


def _refuse(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _context(request: Request):
    from apps.api.app import identity_guard

    return identity_guard.request_auth(request)


def _audit(request: Request, db: Any, event_type: str, *, target_id: str, permission: str,
           project_id: str, details: dict[str, Any]) -> None:
    ctx = _context(request)
    if ctx is None:
        return
    audit.record(audit.SecurityEvent.from_context(
        ctx, event_type, audit.Outcome.SUCCESS, target_type="app_link", target_id=target_id,
        permission=permission, scope=f"project:{project_id}", details=details), db=db)


def _actor(request: Request) -> Optional[str]:
    ctx = _context(request)
    return ctx.principal.id if ctx is not None else None


def build_links_router() -> APIRouter:
    from boss_engine.app_links import service as links

    router = APIRouter(tags=["links"])

    @router.get("/v1/apps/{app_key}/links")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def list_links(app_key: str, project_id: str) -> Any:
        with begin() as db:
            key = links.resolve_app_key(db, project_id, app_key)
            if key is None:
                return _refuse(404, "unknown_app")
            return {"app_key": key, "links": links.list_links(db, project_id, key)}

    @router.post("/v1/apps/{app_key}/links")
    @permit(P.APP_LINK_MANAGE, scope=Scope.project(body="project_id"))
    def create_link(app_key: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        try:
            with begin() as db:
                row = links.create_link(
                    db, project_id=project_id, consumer=app_key,
                    provider=str(payload.get("provider_app_key") or ""),
                    alias=str(payload.get("alias") or ""), env=str(payload.get("env") or "preview"),
                    credentials=str(payload.get("credentials") or "none"), created_by=_actor(request),
                    new_consumer=payload.get("new_app") is True)
                _audit(request, db, "app_link.created", target_id=row["id"], permission=P.APP_LINK_MANAGE.value,
                       project_id=project_id,
                       details={"consumer": row["consumer_app_key"], "provider": row["provider_app_key"],
                                "alias": row["alias"], "env": row["env"], "gives": row["credentials"],
                                "gateway_port": row["gateway_port"]})
                shown = next((r for r in links.list_links(db, project_id, row["consumer_app_key"])
                              if r["id"] == row["id"]), {"id": row["id"], "alias": row["alias"]})
        except links.LinkError as exc:
            return _refuse(exc.status, exc.code, exc.detail)
        return JSONResponse(shown, status_code=201)

    @router.delete("/v1/apps/{app_key}/links/{link_id}")
    @permit(P.APP_LINK_MANAGE, scope=Scope.of("app_link", path="link_id"))
    def revoke_link(app_key: str, link_id: str, request: Request, project_id: Optional[str] = None) -> Any:
        try:
            with begin() as db:
                current = db.execute(text("SELECT project_id FROM app_links WHERE id = :i"),
                                     {"i": link_id}).scalar()
                if current is None or (project_id and project_id != current):
                    return _refuse(404, "not_found")
                row = links.revoke_link(db, project_id=current, link_id=link_id, revoked_by=_actor(request))
                _audit(request, db, "app_link.revoked", target_id=link_id, permission=P.APP_LINK_MANAGE.value,
                       project_id=current, details={"consumer": row["consumer_app_key"],
                                                    "provider": row["provider_app_key"], "alias": row["alias"]})
        except links.LinkError as exc:
            return _refuse(exc.status, exc.code, exc.detail)
        return {"id": link_id, "status": row["status"],
                "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None}

    # Presence and count only.
    @router.get("/v1/apps/{app_key}/test-users")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def test_users(app_key: str, project_id: str) -> Any:
        with begin() as db:
            key = links.resolve_app_key(db, project_id, app_key)
            if key is None:
                return _refuse(404, "unknown_app")
            presence = links.test_users_presence(db, project_id, [key])
        return {"app_key": key, "envs": {env: presence[(key, env)] for env in links.ENVS
                                         if (key, env) in presence}}

    # D9: the generated test-user passwords, behind app_secret:write (step-up), audited.
    @router.post("/v1/apps/{app_key}/test-users/reveal")
    @permit(P.APP_SECRET_WRITE, scope=Scope.project(body="project_id"))
    def reveal_test_users(app_key: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        env = str(payload.get("env") or "preview")
        if env not in links.ENVS:
            return _refuse(422, "bad_env")
        with begin() as db:
            key = links.resolve_app_key(db, project_id, app_key)
            if key is None:
                return _refuse(404, "unknown_app")
            users = links.read_test_users(db, project_id, key, env)
            if not users:
                return _refuse(404, "no_test_users")
            _audit(request, db, "app_link.test_users_revealed", target_id=key,
                   permission=P.APP_SECRET_WRITE.value, project_id=project_id,
                   details={"app": key, "env": env, "count": len(users)})
        return JSONResponse({"app_key": key, "env": env, "users": users},
                            headers={"cache-control": "no-store"})

    return router
