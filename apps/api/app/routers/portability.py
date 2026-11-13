"""Project and org export / import (BOSSAPP-837, services/portability.py).

    GET  /v1/projects/{project_id}/export        the project's bundle (secrets as placeholders)
    POST /v1/projects/{project_id}/import        mode=dry_run: the diff + validation, an import id
                                                 mode=apply + import_id: apply, resumable
    GET  /v1/projects/{project_id}/imports/{id}  one import's progress and report
    GET  /v1/orgs/{org_id}/export                every project of the org, and its model settings
    POST /v1/orgs/{org_id}/import                the same two steps, matched to projects by slug

An apply always follows a dry run of the same bundle: the dry run records the import, and apply
names it. Applying an import that stopped part way carries on from its last finished chunk.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from boss_engine.identity import P, Scope, permit  # noqa: F401  (identity S2)
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services import portability

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(tags=["portability"])


class ImportRequest(BaseModel):
    actor: str
    mode: Literal["dry_run", "apply"] = "dry_run"
    bundle: Optional[dict[str, Any]] = None
    import_id: Optional[str] = None
    max_chunks: Optional[int] = Field(default=None, ge=1)


def _require_actor(connection: Any, project_id: str, actor: str, *, writing: bool) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "workflow_manage")
    if writing:
        require_project_actor(connection, project_id, actor, "pack_manage")


def _export(scope: str, target_id: str, actor: str) -> dict[str, Any]:
    try:
        with begin(get_engine()) as connection:
            if scope == "project":
                _require_actor(connection, target_id, actor, writing=False)
            return portability.export_bundle(connection, get_settings(), scope=scope,
                                             target_id=target_id, actor=actor)
    except portability.PortabilityError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _import(scope: str, target_id: str, payload: ImportRequest) -> dict[str, Any]:
    engine, settings = get_engine(), get_settings()
    if scope == "project":
        with begin(engine) as connection:
            _require_actor(connection, target_id, payload.actor, writing=True)
    try:
        if payload.mode == "dry_run":
            if payload.bundle is None:
                raise HTTPException(status_code=422, detail="a dry run needs the bundle")
            return portability.plan_import(engine, settings, scope=scope, target_id=target_id,
                                           bundle=payload.bundle, actor=payload.actor)
        if not payload.import_id:
            raise HTTPException(status_code=422,
                                detail="apply names the import_id its dry run returned")
        with begin(engine) as connection:
            found = portability.get_import(connection, payload.import_id)
        if found is None or found["scope"] != scope or found["target_id"] != target_id:
            raise HTTPException(status_code=404, detail="no such import for this target")
        return portability.apply_import(engine, settings, import_id=payload.import_id,
                                        actor=payload.actor, max_chunks=payload.max_chunks)
    except portability.PortabilityError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/v1/projects/{project_id}/export")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def export_project(project_id: str, actor: str = Query(...)) -> dict[str, Any]:
    """The project's bundle. Never a secret value: each is a typed empty placeholder."""
    return _export("project", project_id, actor)


@router.post("/v1/projects/{project_id}/import")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def import_project(project_id: str, payload: ImportRequest) -> dict[str, Any]:
    """Dry run (diff + validation, nothing written) or apply (by import_id, resumable)."""
    return _import("project", project_id, payload)


@router.get("/v1/projects/{project_id}/imports/{import_id}")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def get_project_import(project_id: str, import_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        found = portability.get_import(connection, import_id)
    if found is None or found["scope"] != "project" or found["target_id"] != project_id:
        raise HTTPException(status_code=404, detail="no such import for this project")
    return found


@router.get("/v1/orgs/{org_id}/export")
@permit(P.ORG_UPDATE, scope=Scope.org())
def export_org(org_id: str, actor: str = Query(...)) -> dict[str, Any]:
    """Every project of the org, and the org's model settings."""
    return _export("org", org_id, actor)


@router.post("/v1/orgs/{org_id}/import")
@permit(P.ORG_UPDATE, scope=Scope.org())
def import_org(org_id: str, payload: ImportRequest) -> dict[str, Any]:
    """Projects are matched by slug; a project the target org lacks is reported, not created."""
    return _import("org", org_id, payload)
