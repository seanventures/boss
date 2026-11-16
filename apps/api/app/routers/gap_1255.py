"""Where a built app may run (BOSSAPP-1255, services/residency_pin.py).

    GET    /v1/projects/{project_id}/residency-pins/{app_key}
    PUT    /v1/projects/{project_id}/residency-pins/{app_key}
    DELETE /v1/projects/{project_id}/residency-pins/{app_key}
    POST   /v1/projects/{project_id}/residency-pins/{app_key}/admit

A mismatch is a 409 whose body is the refusal. The control-plane host on the
row is never replaced. No handler contacts a cloud region.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from boss_engine.identity import P, Scope, permit
from boss_engine.services.residency_pin import (
    ResidencyRefused,
    admit_deploy,
    clear_pin,
    read_pin,
    set_pin,
)

from ..db import begin

router = APIRouter(tags=["residency-pin"])


class PinBody(BaseModel):
    actor: str
    geography: str
    control_plane_host: str


class ClearBody(BaseModel):
    actor: str


class AdmitBody(BaseModel):
    target_geography: str
    app_path: str
    control_plane_host: str


def _refused(error: ResidencyRefused) -> HTTPException:
    status = 403 if error.code == "not_admin" else 409
    return HTTPException(status_code=status, detail={"error": str(error), "code": error.code})


@router.get("/v1/projects/{project_id}/residency-pins/{app_key}")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def read_residency_pin(
    project_id: str, app_key: str, control_plane_host: str = Query("")
) -> dict[str, Any]:
    with begin() as connection:
        return read_pin(
            connection,
            project_id=project_id,
            app_key=app_key,
            control_plane_host=control_plane_host,
        )


@router.put("/v1/projects/{project_id}/residency-pins/{app_key}")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def put_residency_pin(project_id: str, app_key: str, payload: PinBody) -> dict[str, Any]:
    try:
        with begin() as connection:
            return set_pin(
                connection,
                project_id=project_id,
                app_key=app_key,
                geography=payload.geography,
                actor=payload.actor,
                control_plane_host=payload.control_plane_host,
            )
    except ResidencyRefused as error:
        raise _refused(error) from error


@router.delete("/v1/projects/{project_id}/residency-pins/{app_key}")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def delete_residency_pin(project_id: str, app_key: str, payload: ClearBody) -> dict[str, Any]:
    try:
        with begin() as connection:
            return clear_pin(
                connection, project_id=project_id, app_key=app_key, actor=payload.actor
            )
    except ResidencyRefused as error:
        raise _refused(error) from error


@router.post("/v1/projects/{project_id}/residency-pins/{app_key}/admit")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"), readonly_post=True)
def admit_residency_deploy(project_id: str, app_key: str, payload: AdmitBody) -> dict[str, Any]:
    with begin() as connection:
        result = admit_deploy(
            connection,
            project_id=project_id,
            app_key=app_key,
            target_geography=payload.target_geography,
            app_path=payload.app_path,
            control_plane_host=payload.control_plane_host,
        )
    if not result["allowed"]:
        raise HTTPException(status_code=409, detail=result)
    return result
