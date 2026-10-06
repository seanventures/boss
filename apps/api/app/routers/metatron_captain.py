"""Metatron Captain HTTP surface — session, goals, watch, drive, overwatch."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.services.metatron_captain import MetatronCaptain

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["metatron"])


class SessionTurnRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    message: str
    thread_id: Optional[str] = None


class GoalCreateRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    title: str
    kind: str = "custom"
    description: str = ""
    auto_drive: Optional[bool] = None


class WatchRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"


class DriveRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    auto: bool = Field(
        default=False,
        description="Apply only moves that pass validate_move. Decisions/DLQ stay human.",
    )


def _captain() -> MetatronCaptain:
    return MetatronCaptain(get_settings())


def _require(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "agent_chat")


@router.post("/v1/metatron/session")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_session_turn(payload: SessionTurnRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _captain().session_turn(
            connection,
            project_id=payload.project_id,
            actor=payload.actor,
            message=payload.message,
            thread_id=payload.thread_id,
        )


@router.get("/v1/metatron/session")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def captain_session_get(project_id: str, actor: str = "executive_console") -> dict[str, Any]:
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, project_id, actor)
        return _captain().load_session(connection, project_id=project_id, actor=actor)


@router.get("/v1/metatron/goals")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def captain_goals_list(project_id: str) -> dict[str, Any]:
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        return {"project_id": project_id, "goals": _captain().list_goals(connection, project_id)}


@router.post("/v1/metatron/goals", status_code=201)
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_goals_create(payload: GoalCreateRequest) -> dict[str, Any]:
    if not payload.project_id.strip() or not payload.title.strip():
        raise HTTPException(status_code=422, detail="project_id and title are required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        goal = _captain().set_goal(
            connection,
            project_id=payload.project_id,
            actor=payload.actor,
            title=payload.title,
            kind=payload.kind,
            description=payload.description,
            auto_drive=payload.auto_drive,
        )
        return goal


@router.post("/v1/metatron/watch")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_watch(payload: WatchRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _captain().watch_once(
            connection, project_id=payload.project_id, actor=payload.actor
        )


@router.get("/v1/metatron/watch/latest")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def captain_watch_latest(project_id: str) -> dict[str, Any]:
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        tick = _captain().latest_watch(connection, project_id)
        return {"project_id": project_id, "watch": tick}


@router.post("/v1/metatron/drive")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_drive(payload: DriveRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _captain().drive(
            connection,
            project_id=payload.project_id,
            actor=payload.actor,
            auto=payload.auto,
        )


@router.post("/v1/metatron/overwatch")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_overwatch(payload: WatchRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _captain().overwatch_once(
            connection, project_id=payload.project_id, actor=payload.actor
        )


class TheaterRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    auto: bool = Field(
        default=True,
        description="Apply bounded command powers (reprioritize/staff/loop). Never a second factory job.",
    )


@router.get("/v1/metatron/theater")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def captain_theater(project_id: str, actor: str = "executive_console") -> dict[str, Any]:
    """Fleet view of official work, captains, and proposed command powers."""
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, project_id, actor)
        return _captain().theater(connection, project_id=project_id, actor=actor)


@router.post("/v1/metatron/theater")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_theater_enact(payload: TheaterRequest) -> dict[str, Any]:
    """Observe the fleet, then apply validated command powers when auto=true."""
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _captain().command_once(
            connection,
            project_id=payload.project_id,
            actor=payload.actor,
            auto=payload.auto,
        )
