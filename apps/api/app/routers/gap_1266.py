"""Data page for a built app (BOSSAPP-1266).

GET  /v1/apps/data                         apps in the project that have drawers
GET  /v1/apps/{app_key}/data               tables, the open table, and the trash
POST /v1/apps/{app_key}/data/import        add rows from a CSV. Never deletes.
POST /v1/apps/{app_key}/data/rows/{row_id}/trash
POST /v1/apps/{app_key}/data/rows/{row_id}/restore
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from boss_engine.identity import P, Scope, permit
from boss_engine.services.app_data import AppDataError

router = APIRouter(tags=["app-data"])


class ImportBody(BaseModel):
    project_id: str
    table: str
    csv: str = Field(default="")


class RowBody(BaseModel):
    project_id: str


def _engine():
    from boss_engine.db import get_engine
    return get_engine()


def _call(fn) -> Any:
    from boss_engine.db import begin

    try:
        with begin(_engine()) as connection:
            return fn(connection)
    except AppDataError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=404, detail="No such project.") from error


@router.get("/v1/apps/data")
@permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
def app_data_index(project_id: str) -> dict[str, Any]:
    from boss_engine.services.app_data import list_apps

    return _call(lambda connection: list_apps(connection, project_id=project_id))


@router.get("/v1/apps/{app_key}/data")
@permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
def app_data_page(app_key: str, project_id: str, table: str = "", field: str = "",
                  value: str = "", mode: str = "is") -> dict[str, Any]:
    from boss_engine.services.app_data import page

    return _call(lambda connection: page(
        connection, project_id=project_id, app_key=app_key, table=table, field=field,
        value=value, mode=mode))


@router.post("/v1/apps/{app_key}/data/import")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def app_data_import(app_key: str, payload: ImportBody) -> dict[str, Any]:
    from boss_engine.services.app_data import import_csv

    return _call(lambda connection: import_csv(
        connection, project_id=payload.project_id, app_key=app_key, table=payload.table,
        csv_text=payload.csv))


@router.post("/v1/apps/{app_key}/data/rows/{row_id}/trash")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def app_data_trash(app_key: str, row_id: str, payload: RowBody) -> dict[str, Any]:
    from boss_engine.services.app_data import soft_delete

    return _call(lambda connection: soft_delete(
        connection, project_id=payload.project_id, app_key=app_key, row_id=row_id))


@router.post("/v1/apps/{app_key}/data/rows/{row_id}/restore")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def app_data_restore(app_key: str, row_id: str, payload: RowBody) -> dict[str, Any]:
    from boss_engine.services.app_data import restore

    return _call(lambda connection: restore(
        connection, project_id=payload.project_id, app_key=app_key, row_id=row_id))
