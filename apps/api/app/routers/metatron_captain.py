"""Metatron HTTP surface. A sentence is an Ask BOSS turn (`agent_turn`); a captain command
(/watch, /overwatch, /drive, goals, theater) is a press of the BOSS agent's own action
(`agent_action`). Neither has a decision path of its own (BOSSAPP-759)."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from ..identity_guard import writing_get  # noqa: E402  (identity int-b)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from boss_engine.schemas import AgentActionRunRequest, AgentChatRequest, AgentChatResponse
from boss_engine.services.metatron_captain import MetatronCaptain

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["metatron"])


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


def _press(request: Request, project_id: str, actor: str, tool: str,
           args: dict[str, Any]) -> dict[str, Any]:
    """A TYPED CAPTAIN COMMAND IS A PRESS OF THE AGENT'S BUTTON (BOSSAPP-759).

    /watch, /overwatch, /drive and the rest are the BOSS agent's actions (tools/captain_tools.py),
    run through `agent_action` — the console's button path — with the arguments the person
    typed: the tool's permission, the identity gate and the transcript are the same as a press
    in Ask BOSS. The reply is the tool's result, the shape these routes always returned."""
    from apps.api.app.main import agent_action

    pressed = agent_action(AgentActionRunRequest(project_id=project_id, actor=actor, tool=tool,
                                                 args=args, label=f"/{tool.split('_', 1)[1]}"),
                           request)
    if not pressed.ok:
        result = pressed.result or {}
        forbidden = result.get("status") in {"forbidden", "denied"} or result.get("permission")
        raise HTTPException(status_code=403 if forbidden else 409,
                            detail=result.get("message") or pressed.error or "refused")
    return dict(pressed.result or {})


@router.post("/v1/metatron/session", response_model=AgentChatResponse)
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def captain_session_turn(payload: AgentChatRequest, request: Request) -> AgentChatResponse:
    """A Metatron terminal turn IS an Ask BOSS turn (BOSSAPP-759).

    A thin adapter, nothing more: the same request, the same function (`agent_turn`), the same
    reply. There was a second engine here — MetatronCaptain.session_turn, its own keyword reading
    and its own model decision — and asked to "set up three projects, tell me what you can't do"
    it ran overwatch moves and proposed a factory job instead of answering. Captain powers are
    explicit commands (/watch, /overwatch, /drive), never something plain text sets off."""
    from apps.api.app.main import agent_turn

    return agent_turn(payload, request, endpoint="/v1/metatron/session")


@writing_get(router, "/v1/metatron/session")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"), writes_on_get=True)
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
def captain_goals_create(payload: GoalCreateRequest, request: Request) -> dict[str, Any]:
    if not payload.project_id.strip() or not payload.title.strip():
        raise HTTPException(status_code=422, detail="project_id and title are required")
    args: dict[str, Any] = {"title": payload.title, "kind": payload.kind,
                            "description": payload.description}
    if payload.auto_drive is not None:
        args["auto_drive"] = payload.auto_drive
    return _press(request, payload.project_id, payload.actor, "captain_set_goal", args)


@router.post("/v1/metatron/watch")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_watch(payload: WatchRequest, request: Request) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    return _press(request, payload.project_id, payload.actor, "captain_watch", {})


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
def captain_drive(payload: DriveRequest, request: Request) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    return _press(request, payload.project_id, payload.actor, "captain_drive",
                  {"auto": bool(payload.auto)})


@router.post("/v1/metatron/overwatch")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_overwatch(payload: WatchRequest, request: Request) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    return _press(request, payload.project_id, payload.actor, "captain_overwatch", {})


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
def captain_theater_enact(payload: TheaterRequest, request: Request) -> dict[str, Any]:
    """Observe the fleet, then apply validated command powers when auto=true."""
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    return _press(request, payload.project_id, payload.actor, "captain_theater",
                  {"auto": bool(payload.auto)})
