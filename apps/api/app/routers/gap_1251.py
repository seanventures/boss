"""HTTP for a named preview share (BOSSAPP-1251).

The password is a request field and nothing else: it is not logged and it is
not returned. Opening a link still checks the password, the expiry and the
stop. An admin stops one link or stops new links.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services.preview_share import (
    ShareRefused,
    create_share,
    list_shares,
    open_share,
    stop_new_links,
    stop_share,
)

router = APIRouter(tags=["preview-share"])
_ENGINE: Any = None


def bind_engine(engine: Any) -> None:
    """Tests point the routes at their database. None uses the process engine."""
    global _ENGINE
    _ENGINE = engine


def _engine():
    if _ENGINE is not None:
        return _ENGINE
    from boss_engine.db import get_engine
    return get_engine()


def _refused(error: ShareRefused) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.code)


class CreateBody(BaseModel):
    project_id: str
    app_key: str
    name: str = Field(max_length=80)
    password: str = Field(max_length=128)
    expires_at: datetime
    actor: str


class OpenBody(BaseModel):
    project_id: str
    password: str = Field(max_length=128)


class ActorBody(BaseModel):
    project_id: str
    actor: str


@router.post("/v1/preview-shares", status_code=201)
@permit(P.PREVIEW_MANAGE, scope=Scope.project(body="project_id"))
def create_preview_share(payload: CreateBody) -> dict[str, Any]:
    try:
        return create_share(
            _engine(),
            project_id=payload.project_id,
            app_key=payload.app_key,
            name=payload.name,
            password=payload.password,
            expires_at=payload.expires_at,
            actor=payload.actor,
        )
    except ShareRefused as error:
        raise _refused(error) from None


@router.get("/v1/preview-shares")
@permit(P.PREVIEW_READ, scope=Scope.project(query="project_id"))
def get_preview_shares(project_id: str) -> dict[str, Any]:
    return {"shares": list_shares(_engine(), project_id=project_id)}


@router.post("/v1/preview-shares/{slug}/open")
@permit(P.PREVIEW_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def open_preview_share(slug: str, payload: OpenBody) -> dict[str, Any]:
    try:
        return open_share(
            _engine(), slug=slug, password=payload.password, project_id=payload.project_id)
    except ShareRefused as error:
        raise _refused(error) from None


@router.post("/v1/preview-shares/{share_id}/stop")
@permit(P.PREVIEW_MANAGE, scope=Scope.project(body="project_id"))
def stop_preview_share(share_id: str, payload: ActorBody) -> dict[str, Any]:
    try:
        return stop_share(
            _engine(), project_id=payload.project_id, share_id=share_id, actor=payload.actor)
    except ShareRefused as error:
        raise _refused(error) from None


@router.post("/v1/preview-shares/stop-new")
@permit(P.PREVIEW_MANAGE, scope=Scope.project(body="project_id"))
def stop_new_preview_shares(payload: ActorBody) -> dict[str, Any]:
    try:
        return stop_new_links(_engine(), project_id=payload.project_id, actor=payload.actor)
    except ShareRefused as error:
        raise _refused(error) from None
