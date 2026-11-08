"""Folder on host (Add a project, screen 03): engine project_experience/host_folder.py.

GET  /v1/projects/{id}/host-folders/hosts    the verified servers this project may read
POST /v1/projects/{id}/host-folders/probe    read one folder on one server (read-only ssh)
POST /v1/projects/{id}/host-folders          record a folder as the project's source
GET  /v1/projects/{id}/host-folders          the project's recorded folders

READING A FOLDER CREATES NOTHING, SO IT NEVER ASKS FOR A STEP-UP (operator, 2026-11-07: "Couldn't
read it now: Not created: you cancelled the check" on a probe). The probe is one fixed, read-only
command over the host's own key (host_folder.command_for): it needs `assistant:chat` on the
project, the bar Add a project's other reads by Helm use (understand, connectors): never a viewer
or an auditor, no fresh second factor. Recording the source is `project:update` (step-up), and
Create and start re-runs the probe itself under `project:create`. The probe is repeated server-side on register, so what is stored is what the
server said, never what the browser sent.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)


class FolderBody(BaseModel):
    host_id: str = Field(..., description="A verified deployment host this project may use.")
    path: str = Field(..., max_length=400, description="An absolute folder path on that host.")


def _require(connection, project_id: str) -> None:
    from boss_engine.services.project_experience.common import require_project

    try:
        require_project(connection, project_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None


def _refused(error: Exception) -> JSONResponse:
    status = int(getattr(error, "status", 422) or 422)
    return JSONResponse({"error": "folder_refused", "message": str(error)}, status_code=status)


def _actor(request: Request) -> str:
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    return identity_guard.actor_for(ctx) if ctx is not None else "operator"


@router.get("/v1/projects/{project_id}/host-folders/hosts")
@permit(P.HOST_READ, scope=Scope.project(path="project_id"))
def host_folder_hosts(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import host_folder

    with begin(_engine) as connection:
        _require(connection, project_id)
        return {"hosts": host_folder.hosts(connection, project_id)}


@router.post("/v1/projects/{project_id}/host-folders/probe")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(path="project_id"))
def host_folder_probe(project_id: str, payload: FolderBody) -> Any:
    from boss_engine.services.project_experience import host_folder

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return host_folder.probe(connection, _settings, project_id, payload.host_id,
                                     payload.path)
        except host_folder.FolderError as refused:
            return _refused(refused)


@router.post("/v1/projects/{project_id}/host-folders")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def host_folder_register(project_id: str, payload: FolderBody, request: Request) -> Any:
    from boss_engine.services.project_experience import host_folder

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            facts = host_folder.probe(connection, _settings, project_id, payload.host_id,
                                      payload.path)
            return host_folder.register(connection, project_id, payload.host_id, payload.path,
                                        facts, actor=_actor(request))
        except host_folder.FolderError as refused:
            return _refused(refused)


@router.get("/v1/projects/{project_id}/host-folders")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def host_folder_list(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import host_folder

    with begin(_engine) as connection:
        _require(connection, project_id)
        return {"folders": host_folder.listing(connection, project_id)}


# ── Infra › server (screen 14): the shared server at a glance ──────────────────────────────

@router.get("/v1/infra/hosts/{host_id}/overview")
@permit(P.HOST_READ, scope=Scope.project(query="project_id"))
def infra_host_overview(host_id: str, project_id: str, request: Request) -> Any:
    """The server's header, capacity tiles, the projects on it (every project the viewer may
    read, each linking to its Project › Deployments), host plugins and recent changes."""
    from apps.api.app.main import visible_project_rows
    from boss_engine.services.project_experience import host_overview

    visible = [str(r["id"]) for r in visible_project_rows(request)]
    with begin(_engine) as connection:
        try:
            return host_overview.overview(connection, project_id.strip(), host_id, visible)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None


class CapacityBody(BaseModel):
    project_id: str


@router.post("/v1/infra/hosts/{host_id}/capacity")
@permit(P.HOST_EXEC, scope=Scope.project(body="project_id"))
def infra_host_capacity(host_id: str, payload: CapacityBody) -> Any:
    """Read the server's cpu, memory, disk and running images now (one read-only ssh session)."""
    from boss_engine.services.project_experience import host_folder, host_overview

    with begin(_engine) as connection:
        try:
            return host_overview.read_capacity(connection, _settings, payload.project_id,
                                               host_id)
        except host_folder.FolderError as refused:
            return _refused(refused)
