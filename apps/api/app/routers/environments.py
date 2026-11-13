"""Execution environments API (BOSSAPP-824 phase 1).

CRUD over /v1/environments plus POST /v1/environments/{id}/probe (the console's
Test button). An environment row says WHERE a governed build's sandbox runs —
local, a machine over SSH, the micro-VM lane, or a phase-2 driver.

Secrets never ride this API: `secret_refs` holds secret_refs.id REFERENCES
(the kubeconfig / service_account / private-key kinds secrets_service already
knows), and the service's public_view scrubs secret-shaped config keys, so no
response ever contains a secret value.
"""
from __future__ import annotations

from boss_engine.identity import P, Scope, permit  # noqa: E402  (identity S2)
from ..identity_guard import writing_get  # noqa: E402  (identity int-b)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services import execution_environments as envs

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/environments", tags=["environments"])


class CreateRequest(BaseModel):
    project_id: str
    actor: str
    name: str
    driver: str
    config: dict = Field(default_factory=dict)
    secret_refs: dict = Field(default_factory=dict)


class UpdateRequest(BaseModel):
    project_id: str
    actor: str
    name: Optional[str] = None
    config: Optional[dict] = None
    secret_refs: Optional[dict] = None
    status: Optional[str] = None


class ActorRequest(BaseModel):
    project_id: str
    actor: str


def _require_operator(connection: Any, project_id: str, actor: str,
                      *, mutating: bool = False) -> None:
    # Same binding the preview-sandbox routes use: the claimed actor is bound to
    # the transport principal and the project before anything is read or, for a
    # mutation (which can reach a machine over SSH), changed.
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "providerops_manage")
    if mutating:
        require_project_actor(connection, project_id, actor, "workflow_execute")


def _own_row(connection, environment_id: str, project_id: str) -> dict:
    """The row, only when this project may see it: its own, or an org-wide one."""
    try:
        row = envs.get_environment(connection, environment_id)
    except envs.EnvironmentNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if row.get("project_id") not in (None, project_id):
        raise HTTPException(status_code=404,
                            detail=f"no execution environment {environment_id!r}.")
    return row


@writing_get(router, "")
@writing_get(router, "/")
@permit(P.HOST_READ, scope=Scope.project(query="project_id"), writes_on_get=True)
def list_environments(
    project_id: str = Query(...),
    actor: str = Query(...),
    include_org_wide: bool = Query(True),
) -> dict[str, Any]:
    """The project's execution environments, plus the org-wide rows it may use."""
    with begin(get_engine()) as connection:
        _require_operator(connection, project_id, actor)
        items = [envs.public_view(r) for r in envs.list_environments(
            connection, project_id, include_org_wide=include_org_wide)]
    return {"project_id": project_id, "items": items, "count": len(items)}


@router.post("")
@router.post("/")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def create_environment(payload: CreateRequest) -> dict[str, Any]:
    try:
        with begin(get_engine()) as connection:
            _require_operator(connection, payload.project_id, payload.actor,
                              mutating=True)
            row = envs.create_environment(
                connection,
                project_id=payload.project_id,
                name=payload.name,
                driver=payload.driver,
                config=payload.config,
                secret_refs=payload.secret_refs,
                actor=payload.actor,
            )
            return envs.public_view(row)
    except envs.EnvironmentUnusable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/{environment_id}")
@router.patch("/{environment_id}")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def update_environment(environment_id: str, payload: UpdateRequest) -> dict[str, Any]:
    try:
        with begin(get_engine()) as connection:
            _require_operator(connection, payload.project_id, payload.actor,
                              mutating=True)
            _own_row(connection, environment_id, payload.project_id)
            row = envs.update_environment(
                connection,
                environment_id,
                name=payload.name,
                config=payload.config,
                secret_refs=payload.secret_refs,
                status=payload.status,
            )
            return envs.public_view(row)
    except envs.EnvironmentUnusable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/{environment_id}")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def delete_environment(environment_id: str, payload: ActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require_operator(connection, payload.project_id, payload.actor,
                          mutating=True)
        row = _own_row(connection, environment_id, payload.project_id)
        if row.get("project_id") is None:
            # An org-wide row is shared; a project may not delete it from here.
            raise HTTPException(
                status_code=409,
                detail="this environment is org-wide; retire it instead "
                       "(PATCH status=retired) or delete it at the org level.")
        envs.delete_environment(connection, environment_id)
    return {"id": environment_id, "deleted": True}


@router.post("/{environment_id}/probe")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def probe_environment(environment_id: str, payload: ActorRequest) -> dict[str, Any]:
    """The Test button: actually reach the environment and say what a build
    would find — never a checkbox that cannot fail. Records verified/degraded."""
    with begin(get_engine()) as connection:
        _require_operator(connection, payload.project_id, payload.actor,
                          mutating=True)
        row = _own_row(connection, environment_id, payload.project_id)
        result = envs.probe_environment(connection, get_settings(), row)
        envs.record_probe(connection, environment_id, result)
    return {"id": environment_id, "probe": result}
