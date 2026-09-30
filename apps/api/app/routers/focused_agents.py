"""Focused always-on agents — builder + duty loop HTTP surface."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.services.focused_agents import FocusedAgentService

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["focused-agents"])


class CreateFocusedAgentRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    kind: str = "custom"
    name: str = ""
    focus: str = ""
    mode: Optional[str] = None
    interval_seconds: Optional[int] = None
    allowed_actions: Optional[list[str]] = None
    wake_on: Optional[list[str]] = None
    start: bool = Field(default=True, description="Activate immediately.")


class FocusedAgentActorRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    reason: str = "manual"


class TickAllRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"


def _service() -> FocusedAgentService:
    return FocusedAgentService(get_settings())


def _require(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "agent_chat")


@router.get("/v1/focused-agents")
def list_focused_agents(project_id: str) -> dict[str, Any]:
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        return {
            "project_id": project_id,
            "agents": _service().list_agents(connection, project_id),
        }


@router.post("/v1/focused-agents", status_code=201)
def create_focused_agent(payload: CreateFocusedAgentRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().create(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                kind=payload.kind,
                name=payload.name,
                focus=payload.focus,
                mode=payload.mode,
                interval_seconds=payload.interval_seconds,
                allowed_actions=payload.allowed_actions,
                wake_on=payload.wake_on,
                start=payload.start,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/focused-agents/{agent_id}")
def get_focused_agent(agent_id: str, project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        try:
            return _service().get(connection, project_id, agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/focused-agents/{agent_id}/wake")
def wake_focused_agent(agent_id: str, payload: FocusedAgentActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().wake(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                agent_id=agent_id,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/focused-agents/{agent_id}/pause")
def pause_focused_agent(agent_id: str, payload: FocusedAgentActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().set_status(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                agent_id=agent_id,
                status="paused",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/focused-agents/{agent_id}/activate")
def activate_focused_agent(agent_id: str, payload: FocusedAgentActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().set_status(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                agent_id=agent_id,
                status="active",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/focused-agents/tick-all")
def tick_all_focused_agents(payload: TickAllRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _service().tick_due(
            connection, project_id=payload.project_id, actor=payload.actor
        )
