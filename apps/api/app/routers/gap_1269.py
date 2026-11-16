"""Builder freezes on files and data tables (BOSSAPP-1269).

GET  /v1/freeze-paths            the paths a builder has frozen
POST /v1/freeze-paths            freeze one file or data table
POST /v1/freeze-paths/thaw       let that path change again
POST /v1/freeze-paths/write      an agent write. A frozen path is 409 and the
                                 bytes stay as they were.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from boss_engine.identity import P, Scope, permit
from boss_engine.services import freeze_paths
from boss_engine.services.freeze_paths import FreezeError, FrozenPath

from ..db import begin, get_engine

router = APIRouter(tags=["freeze-paths"])


class FreezeBody(BaseModel):
    project_id: str
    kind: str
    path: str


class WriteBody(BaseModel):
    project_id: str
    kind: str
    path: str
    content: str = ""


def _run(fn) -> Any:
    try:
        with begin(get_engine()) as connection:
            return fn(connection)
    except FrozenPath as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FreezeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/freeze-paths")
@permit(P.SETTINGS_READ, scope=Scope.project(query="project_id"))
def list_freeze_paths(project_id: str = Query(...)) -> dict[str, Any]:
    paths = _run(lambda connection: freeze_paths.list_freezes(connection, project_id))
    return {"project_id": project_id, "paths": paths}


@router.post("/v1/freeze-paths")
@permit(P.SETTINGS_UPDATE, scope=Scope.project(body="project_id"))
def freeze_path(body: FreezeBody) -> dict[str, Any]:
    return _run(lambda connection: freeze_paths.freeze(
        connection, body.project_id, body.kind, body.path, actor="builder"))


@router.post("/v1/freeze-paths/thaw")
@permit(P.SETTINGS_UPDATE, scope=Scope.project(body="project_id"))
def thaw_path(body: FreezeBody) -> dict[str, Any]:
    return _run(lambda connection: freeze_paths.thaw(
        connection, body.project_id, body.kind, body.path))


@router.post("/v1/freeze-paths/write")
@permit(P.SETTINGS_UPDATE, scope=Scope.project(body="project_id"))
def write_path(body: WriteBody) -> dict[str, Any]:
    def go(connection):
        kind = body.kind.strip().lower()
        if kind == "file":
            return freeze_paths.write_file(
                connection, body.project_id, freeze_paths.file_root(body.project_id),
                body.path, body.content)
        if kind == "table":
            return freeze_paths.write_table(
                connection, body.project_id, body.path, body.content)
        raise FreezeError("kind is file or table")

    return _run(go)
