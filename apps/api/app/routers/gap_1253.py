"""A project's four surfaces, published without dropping one another (BOSSAPP-1253).

GET  /v1/projects/{project_id}/suite
PUT  /v1/projects/{project_id}/suite/surfaces/{kind}
POST /v1/projects/{project_id}/suite/publish
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, database_is_reachable, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.project_suite import SuiteError, hold_surface, publish, read_suite

router = APIRouter(tags=["project-suite"])
_settings = get_settings()
_engine = get_engine(_settings)


class HoldBody(BaseModel):
    title: str = Field(default="", max_length=400)
    body: str = Field(default="", max_length=8000)


class PublishBody(BaseModel):
    mode: str = Field(default="", max_length=40)
    kind: Optional[str] = Field(default=None, max_length=40)
    dedupe_key: str = Field(default="", max_length=200)


def _unavailable() -> JSONResponse:
    return JSONResponse({"error": "database", "message": "Postgres is not reachable."},
                        status_code=503)


def _failure(error: SuiteError) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


@router.get("/v1/projects/{project_id}/suite")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def get_suite(project_id: str) -> Any:
    """The website, mobile app, slides, and data view this project holds."""
    if not database_is_reachable(_engine):
        return _unavailable()
    with begin(_engine) as connection:
        try:
            return read_suite(connection, project_id)
        except SuiteError as error:
            return _failure(error)


@router.put("/v1/projects/{project_id}/suite/surfaces/{kind}")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def put_surface(project_id: str, kind: str, payload: HoldBody) -> Any:
    """Hold one surface. The other surfaces stay as they are."""
    if not database_is_reachable(_engine):
        return _unavailable()
    with begin(_engine) as connection:
        try:
            return hold_surface(connection, project_id, kind=kind, title=payload.title,
                                body=payload.body)
        except SuiteError as error:
            return _failure(error)


@router.post("/v1/projects/{project_id}/suite/publish")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def post_publish(project_id: str, payload: PublishBody) -> Any:
    """Publish one surface, or all four together. The same dedupe key does not publish twice."""
    if not database_is_reachable(_engine):
        return _unavailable()
    with begin(_engine) as connection:
        try:
            return publish(connection, project_id, mode=payload.mode, kind=payload.kind,
                           dedupe_key=payload.dedupe_key)
        except SuiteError as error:
            return _failure(error)
