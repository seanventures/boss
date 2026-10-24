"""Archive and restore a project (BOSSAPP-768 S5; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.7, §3).

GET  /v1/projects/{project_id}/archive     what archiving would pause, counted (the confirm dialog)
POST /v1/projects/{project_id}/archive     archive: hidden from the list, no new work, schedulers skip it
POST /v1/projects/{project_id}/unarchive   restore: everything resumes on the next tick

Both writes need `project:delete` (owner, step-up) and `step_up: "confirmed"` in the body, the
explicit confirmation the console's dialog sends. Each one is audited (`project.archived` /
`project.restored` in the org's security log, for an identity principal) and evented (an `events`
row in the project). The work is services/projects_archive.py, the same service the agent's
archive_project / restore_project tools call.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from boss_engine.identity import P, Scope, permit
from boss_engine.services import projects_archive as archive_service

router = APIRouter(tags=["projects"])


class ArchiveBody(BaseModel):
    actor: Optional[str] = None
    step_up: Optional[str] = None
    note: Optional[str] = None


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _answer(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in ("id", "name", "slug", "workspace_id", "workspace_name",
                                   "org_id", "org_name", "archived_by", "changed", "counts",
                                   "stops", "keeps")}
    out["created_at"] = _iso(row.get("created_at"))
    out["archived_at"] = _iso(row.get("archived_at"))
    out["archived"] = row.get("archived_at") is not None
    return {k: v for k, v in out.items() if v is not None or k in ("archived_at", "archived_by")}


def _caller(request: Request, connection, project_id: str, body: Optional[ArchiveBody]):
    """(auth context, actor) for a write; the project must exist and be in the caller's org."""
    from apps.api.app import identity_guard
    from apps.api.app.main import (current_api_principal, current_transport_authenticated,
                                   require_project_actor, settings)

    row = archive_service.state(connection, project_id)
    ctx = identity_guard.request_auth(request)
    if row is None or (ctx is not None and str(row["org_id"]) != str(ctx.principal.org_id)):
        raise HTTPException(status_code=404, detail=f"There is no project {project_id}.")
    principal = current_api_principal.get()
    if ctx is not None:
        actor = identity_guard.actor_for(ctx)
    else:
        actor = (body.actor if body and body.actor else None) or principal or "operator"
    if not identity_guard.identity_enforced() and principal and current_transport_authenticated.get():
        # Pre-identity installs: the bar for changing a project's membership, at the home project
        # (the same one POST /v1/projects uses). Under enforce, @permit decided project:delete.
        require_project_actor(connection, settings.default_project_id, principal, "member_manage")
    return ctx, actor


def _confirmed(body: ArchiveBody) -> None:
    if body.step_up != "confirmed":
        raise HTTPException(status_code=428, detail={
            "error": "confirmation_required",
            "message": "Archiving or restoring a project needs an explicit confirmation "
                       "(step_up: \"confirmed\"), the one the console's dialog sends."})


@router.get("/v1/projects/{project_id}/archive")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def archive_preview(project_id: str) -> dict[str, Any]:
    """The project's archive state and, while it is open, what archiving it would pause."""
    from apps.api.app.main import begin, engine

    with begin(engine) as connection:
        row = archive_service.state(connection, project_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"There is no project {project_id}.")
        if row["archived_at"] is None:
            row.update(archive_service.what_stops(connection, project_id))
        return _answer(row)


@router.post("/v1/projects/{project_id}/archive")
@permit(P.PROJECT_DELETE, scope=Scope.org())
def archive_project(project_id: str, body: ArchiveBody, request: Request) -> dict[str, Any]:
    """Archive the project: hidden from GET /v1/projects, new work refused (409
    project_archived), every scheduler skips it. Reads keep working; nothing is deleted."""
    from apps.api.app.main import begin, engine, settings

    _confirmed(body)
    with begin(engine) as connection:
        ctx, actor = _caller(request, connection, project_id, body)
        try:
            row = archive_service.archive(connection, settings, project_id=project_id,
                                          actor=actor, auth=ctx, note=body.note)
        except archive_service.ArchiveRefused as refused:
            raise HTTPException(status_code=409, detail={
                "error": "archive_refused", "message": str(refused)}) from None
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None
        return _answer(row)


@router.post("/v1/projects/{project_id}/unarchive")
@permit(P.PROJECT_DELETE, scope=Scope.org())
def unarchive_project(project_id: str, body: ArchiveBody, request: Request) -> dict[str, Any]:
    """Restore an archived project: listed again, takes work again, schedulers resume."""
    from apps.api.app.main import begin, engine, settings

    _confirmed(body)
    with begin(engine) as connection:
        ctx, actor = _caller(request, connection, project_id, body)
        try:
            row = archive_service.restore(connection, settings, project_id=project_id,
                                          actor=actor, auth=ctx)
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None
        return _answer(row)
