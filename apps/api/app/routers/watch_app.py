"""WATCH THIS APP: the HTTP surface (engine services/watch_app.py; docs/product/wireframes/
agentic-factory/2-watch-an-app.html). It lives on the app's own page, Project › Health.

POST /v1/projects/{id}/watch/read            the one input read: which app, how it talks, what
                                             matters, the watches with a 7-day backtest, the
                                             open question, the boxes. Creates nothing.
POST /v1/projects/{id}/watch                 Start watching: the app's alert rules, the outside
                                             channel (sent its code), routing, quiet hours.
GET  /v1/projects/{id}/watch/{target_id}     the landing: diagram, Now line, the one gate.
POST /v1/projects/{id}/watch/{target_id}/verify   enter the channel's code (Talking to the outside)
POST /v1/projects/{id}/watch/{target_id}/resend   send the code again
POST /v1/projects/{id}/watch/{target_id}/change   a later edit of one watch, with its why

WHO MAY DO WHAT. Reading creates nothing: `observability:read`. Every write is
`observability:manage` (Owner/Admin), the same permission as Observability's rules and contact
points, whose engine functions these call. Respond automatically on a non-prod app asks for
the person's standing grant the first time (409 consent_required; the console asks once).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["observability"])
_settings = get_settings()
_engine = get_engine(_settings)


class ReadBody(BaseModel):
    text: str = Field("", max_length=4000)
    kind: Optional[str] = Field(None, description="project | url | elsewhere: the kind pill, when picked.")
    target_id: Optional[str] = Field(None, description="The app, when the person picked one.")


class StartBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    actor: Optional[str] = None
    target_id: str
    words: str = Field("", max_length=4000)
    #: [{kind, params?, for_s?, tone: wake|tell, enabled?, words?}]
    watches: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    night: str = "wake"
    alone: bool = True
    #: {kind: slack_webhook|mattermost|webhook|email, name, url?|address?}; {} tells only you.
    tell: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "UTC"
    reason: str = Field("", max_length=500)


class CodeBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    code: str = ""


class ProjectBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class ChangeBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    rule_id: str
    reason: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    for_s: Optional[int] = None
    tone: Optional[str] = None
    remove: bool = False


def _refused(error: Exception) -> JSONResponse:
    status = int(getattr(error, "status", 422) or 422)
    return JSONResponse({"error": "watch_refused", "message": str(error), "detail": str(error)},
                        status_code=status)


def _write_actor(connection, project_id: str, claimed: Optional[str]) -> str:
    from apps.api.app import identity_guard
    from apps.api.app import main

    enforced = identity_guard.request_actor()
    if enforced:
        return enforced
    return main.require_project_write_actor(connection, project_id, claimed, "providerops_manage")


def _project(connection, project_id: str) -> None:
    from boss_engine.services.project_experience.common import require_project

    try:
        require_project(connection, project_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None


@router.post("/v1/projects/{project_id}/watch/read")
@permit(P.OBSERVABILITY_READ, scope=Scope.project(path="project_id"), readonly_post=True)
def read_watch_words(project_id: str, payload: ReadBody) -> Any:
    from boss_engine.services import watch_app

    with begin(_engine) as connection:
        _project(connection, project_id)
        try:
            return watch_app.understand(connection, _settings, project_id, payload.text,
                                        kind=payload.kind, target_id=payload.target_id)
        except watch_app.WatchRefused as refused:
            return _refused(refused)


@router.post("/v1/projects/{project_id}/watch")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(path="project_id"))
def start_watching(project_id: str, payload: StartBody) -> Any:
    from boss_engine.services import watch_app

    if payload.project_id != project_id:
        raise HTTPException(status_code=422, detail="project_id differs from the path")
    # A refusal raised INSIDE the transaction rolls every write back (a rule edited before the
    # channel was refused stays as it was): nothing is caught until the block has unwound.
    try:
        with begin(_engine) as connection:
            _project(connection, project_id)
            actor = _write_actor(connection, project_id, payload.actor)
            out = watch_app.start(connection, _settings, project_id, actor=actor,
                                  spec=payload.model_dump())
    except watch_app.WatchRefused as refused:
        return _refused(refused)
    return {**out, "href": f"?project_id={project_id}&tab=health"}


@router.get("/v1/projects/{project_id}/watch/{target_id}")
@permit(P.OBSERVABILITY_READ, scope=Scope.project(path="project_id"))
def watch_landing(project_id: str, target_id: str) -> Any:
    from boss_engine.services import watch_app

    with begin(_engine) as connection:
        try:
            return watch_app.landing(connection, _settings, project_id, target_id)
        except watch_app.WatchRefused as refused:
            return _refused(refused)


@router.post("/v1/projects/{project_id}/watch/{target_id}/verify")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(path="project_id"))
def verify_channel(project_id: str, target_id: str, payload: CodeBody) -> Any:
    from boss_engine.services import watch_app

    try:
        with begin(_engine) as connection:
            actor = _write_actor(connection, project_id, payload.actor)
            return watch_app.verify_tell(connection, project_id, target_id, payload.code,
                                         actor=actor)
    except watch_app.WatchRefused as refused:
        return _refused(refused)


@router.post("/v1/projects/{project_id}/watch/{target_id}/resend")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(path="project_id"))
def resend_code(project_id: str, target_id: str, payload: ProjectBody) -> Any:
    from boss_engine.services import watch_app

    try:
        with begin(_engine) as connection:
            actor = _write_actor(connection, project_id, payload.actor)
            return watch_app.resend(connection, project_id, target_id, actor=actor)
    except watch_app.WatchRefused as refused:
        return _refused(refused)


@router.post("/v1/projects/{project_id}/watch/{target_id}/change")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(path="project_id"))
def change_watch(project_id: str, target_id: str, payload: ChangeBody) -> Any:
    from boss_engine.services import watch_app

    try:
        with begin(_engine) as connection:
            actor = _write_actor(connection, project_id, payload.actor)
            return watch_app.change(connection, project_id, target_id, payload.rule_id,
                                    actor=actor, reason=payload.reason,
                                    edit=payload.model_dump())
    except watch_app.WatchRefused as refused:
        return _refused(refused)
