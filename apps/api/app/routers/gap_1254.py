"""Workspace design system (BOSSAPP-1254).

GET  /v1/workspaces/{workspace_id}/design-system
PUT  /v1/workspaces/{workspace_id}/design-system
POST /v1/workspaces/{workspace_id}/apps

The caller's org is the only scope. Another org gets not_found and none of the saved values.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["design-system"])


class DesignSystemBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    colors: dict[str, Any] = Field(default_factory=dict)
    type: dict[str, Any] = Field(default_factory=dict)
    spacing: dict[str, Any] = Field(default_factory=dict)


class AppBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


def _caller(request: Request) -> str:
    from apps.api.app import identity_guard
    from boss_engine.config import get_settings

    ctx = identity_guard.request_auth(request)
    if ctx is not None:
        return str(ctx.principal.org_id)
    return str(get_settings().default_org_id)


def _engine():
    from boss_engine.config import get_settings
    from boss_engine.db import get_engine

    return get_engine(get_settings())


def _failure(error: Any) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


@router.get("/v1/workspaces/{workspace_id}/design-system")
@permit(P.ORG_READ)
def read_design_system(workspace_id: str, request: Request) -> Any:
    """The workspace's design system, or not_found when this org cannot see one."""
    from boss_engine.db import begin, database_is_reachable
    from boss_engine.services.design_system import DesignSystemError, read_system

    engine = _engine()
    if not database_is_reachable(engine):
        return JSONResponse(
            {"error": "database", "message": "Postgres is not reachable."}, status_code=503)
    org_id = _caller(request)
    with begin(engine) as connection:
        found = read_system(connection, org_id=org_id, workspace_id=workspace_id)
    if found is None:
        return _failure(DesignSystemError(
            404, "not_found", "No design system for that workspace."))
    return found


@router.put("/v1/workspaces/{workspace_id}/design-system")
@permit(P.ORG_UPDATE)
def save_design_system(workspace_id: str, payload: DesignSystemBody, request: Request) -> Any:
    """Save the name, colors, type and spacing for this workspace."""
    from boss_engine.db import begin, database_is_reachable
    from boss_engine.services.design_system import DesignSystemError, save_system

    engine = _engine()
    if not database_is_reachable(engine):
        return JSONResponse(
            {"error": "database", "message": "Postgres is not reachable."}, status_code=503)
    org_id = _caller(request)
    try:
        with begin(engine) as connection:
            return save_system(
                connection, org_id=org_id, workspace_id=workspace_id,
                name=payload.name, colors=payload.colors, type=payload.type,
                spacing=payload.spacing)
    except DesignSystemError as error:
        return _failure(error)


@router.post("/v1/workspaces/{workspace_id}/apps", status_code=201)
@permit(P.PROJECT_CREATE, scope=Scope.org())
def create_workspace_app(workspace_id: str, payload: AppBody, request: Request) -> Any:
    """Create an app in the workspace. The response is the spec the app read."""
    from boss_engine.db import begin, database_is_reachable
    from boss_engine.services.design_system import DesignSystemError, create_app

    engine = _engine()
    if not database_is_reachable(engine):
        return JSONResponse(
            {"error": "database", "message": "Postgres is not reachable."}, status_code=503)
    org_id = _caller(request)
    try:
        with begin(engine) as connection:
            return create_app(
                connection, org_id=org_id, workspace_id=workspace_id, name=payload.name)
    except DesignSystemError as error:
        return _failure(error)
