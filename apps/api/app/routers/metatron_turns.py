"""MT-10 HTTP SSE streaming turn endpoints (leaves metatron_captain.py & /v1/metatron/session intact)."""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, authenticated, permit
from boss_engine.services import metatron_turns

from .. import identity_guard
from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(tags=["metatron-v1"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class TurnCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    project_id: str = Field(min_length=1, max_length=200)
    thread_id: Optional[str] = None
    agent: Optional[str] = None
    actor: Optional[str] = None
    message: str = Field(min_length=1, max_length=200_000)
    mode: str = "ask"
    client: Optional[dict[str, Any]] = None
    context: Optional[dict[str, Any]] = None
    client_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_deadline_s: Optional[int] = None


class ToolResultRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    call_id: str = Field(min_length=1, max_length=200)
    status: str = "ok"
    output: Any = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)


def _ctx(request: Request) -> Any:
    cached = getattr(request.state, "identity", None)
    if cached is not None:
        return cached
    try:
        return identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001
        return None


def _heartbeat_seconds() -> float:
    raw = os.environ.get("BOSS_METATRON_SSE_HEARTBEAT_SECONDS")
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return metatron_turns.DEFAULT_HEARTBEAT_S


@router.post("/v1/metatron/turns")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def start_metatron_turn(payload: TurnCreateRequest, request: Request) -> StreamingResponse:
    from apps.api.app.main import request_actor_or_principal, require_project_actor

    ctx = _ctx(request)
    actor = str(payload.actor or request_actor_or_principal() or "executive_console")
    principal_id = str(ctx.principal.id) if ctx is not None else actor
    org_id = ctx.principal.org_id if ctx is not None else None
    engine = get_engine()
    settings = get_settings()

    with begin(engine) as c:
        require_project_actor(c, payload.project_id, actor, "agent_chat")

    try:
        state = metatron_turns.start_turn(
            engine,
            settings,
            project_id=payload.project_id,
            actor=actor,
            principal_id=principal_id,
            org_id=org_id,
            payload=payload.model_dump(),
            ctx=ctx,
            idempotency_key=request.headers.get("idempotency-key"),
            request_hash=getattr(request.state, "voice_intent_fingerprint", None),
        )
    except metatron_turns.MetatronTurnError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())  # type: ignore[return-value]

    metatron_turns.launch_turn_runner(engine, settings, state)

    def _mark_done() -> None:
        request.state.voice_stream_completed = True

    return StreamingResponse(
        metatron_turns.stream_turn_events(
            engine,
            state["turn_id"],
            after_seq=0,
            heartbeat_s=_heartbeat_seconds(),
            on_complete=_mark_done,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/v1/metatron/turns/{turn_id}/tool_results", status_code=202)
@authenticated()
def submit_metatron_tool_result(
    turn_id: str, payload: ToolResultRequest, request: Request
) -> JSONResponse:
    ctx = _ctx(request)
    enforced = identity_guard.identity_enforced()
    try:
        result = metatron_turns.submit_tool_result(
            get_engine(),
            get_settings(),
            turn_id=turn_id,
            call_id=payload.call_id,
            status=payload.status,
            output=payload.output,
            decision=payload.decision,
            ctx=ctx,
            enforced=enforced,
        )
    except metatron_turns.MetatronTurnError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())
    return JSONResponse(status_code=202, content=result)


@router.get("/v1/metatron/turns/{turn_id}/events")
@authenticated()
def stream_metatron_turn_events(
    turn_id: str, request: Request, after_seq: int = 0
) -> StreamingResponse:
    ctx = _ctx(request)
    enforced = identity_guard.identity_enforced()
    cursor = max(0, int(after_seq))
    last_event_id = (request.headers.get("last-event-id") or "").strip()
    if last_event_id:
        if ":" not in last_event_id:
            return JSONResponse(  # type: ignore[return-value]
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_last_event_id",
                        "message": "Last-Event-ID must be '<turn_id>:<seq>'.",
                        "retryable": False,
                    }
                },
            )
        ev_turn, _, ev_seq = last_event_id.rpartition(":")
        if ev_turn != turn_id:
            return JSONResponse(  # type: ignore[return-value]
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_last_event_id",
                        "message": "Last-Event-ID turn_id does not match requested turn.",
                        "retryable": False,
                    }
                },
            )
        try:
            cursor = max(cursor, int(ev_seq))
        except ValueError:
            return JSONResponse(  # type: ignore[return-value]
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_last_event_id",
                        "message": "Last-Event-ID sequence must be an integer.",
                        "retryable": False,
                    }
                },
            )

    engine = get_engine()
    try:
        with begin(engine) as c:
            metatron_turns.load_turn_for_caller(c, turn_id, ctx=ctx, enforced=enforced)
    except metatron_turns.MetatronTurnError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())  # type: ignore[return-value]

    return StreamingResponse(
        metatron_turns.stream_turn_events(
            engine,
            turn_id,
            after_seq=cursor,
            heartbeat_s=_heartbeat_seconds(),
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/v1/metatron/turns/{turn_id}/cancel", status_code=202)
@authenticated()
def cancel_metatron_turn(turn_id: str, request: Request) -> JSONResponse:
    ctx = _ctx(request)
    enforced = identity_guard.identity_enforced()
    try:
        out = metatron_turns.cancel_turn(
            get_engine(),
            turn_id=turn_id,
            ctx=ctx,
            enforced=enforced,
        )
    except metatron_turns.MetatronTurnError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())
    return JSONResponse(status_code=202, content=out)
