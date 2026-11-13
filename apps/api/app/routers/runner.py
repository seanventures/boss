"""The API's side of the boss-runner outbox (BOSSAPP-828).

    POST /v1/runner/sessions/{session_id}/events

The runner pushes the events of a turn it owns after the last ack (an empty list is its
heartbeat), as the RUN: `Authorization: DPoP <run token bound to the runner's key>` plus a fresh
DPoP proof, verified by the ordinary run-token path (authn._from_run_token: signature, live grant
and person, jti, the proof's key == cnf.jkt, method and URL, replay). The token must name the run
the session was handed over for (`runner_sessions.identity_run_id`); any other session is 404.

The answer is the CUMULATIVE ack `{"ack": n}` (runner_registry.record_push): everything up to n is
held; the runner trims its outbox to it and re-sends the rest. After an API restart the stream
resumes from exactly that seq. Thin: every rule lives in boss_engine.services.runner_registry.
"""
from __future__ import annotations

from typing import Any

from boss_engine.identity import authenticated
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["runner"])

MAX_EVENTS = 500


def _engine():
    from boss_engine.db import get_engine

    return get_engine()


def _err(code: str, status: int) -> JSONResponse:
    headers = {"WWW-Authenticate": 'DPoP error="invalid_token"'} if status == 401 else None
    return JSONResponse(status_code=status, content={"error": code}, headers=headers)


@router.post("/v1/runner/sessions/{session_id}/events")
@authenticated(note="boss-runner: a DPoP run token for the session's own run; checked in the handler")
async def runner_events(session_id: str, request: Request):
    from starlette.concurrency import run_in_threadpool

    from ..identity_guard import request_auth

    try:
        ctx = getattr(request.state, "identity", None) or request_auth(request)
    except Exception:  # noqa: BLE001 — a refused credential is a 401, never a stack trace
        return _err("unauthenticated", 401)
    rt = getattr(ctx, "run_token", None)
    if rt is None:
        return _err("run_token_required", 403)
    try:
        body: Any = await request.json()
    except ValueError:
        return _err("bad_json", 400)
    if not isinstance(body, dict) or not isinstance(body.get("events", []), list):
        return _err("bad_body", 400)
    events = [e for e in body.get("events", [])[:MAX_EVENTS] if isinstance(e, dict)]

    def record():
        from boss_engine.services import runner_registry

        with _engine().begin() as c:
            return runner_registry.record_push(
                c, session_id=session_id, identity_run_id=rt.task.run_id,
                runner_id=str(body.get("runner_id") or ""), state=str(body.get("state") or ""),
                alive=bool(body.get("alive")), events=events)

    answer = await run_in_threadpool(record)
    if answer is None:
        return _err("unknown_session", 404)
    return JSONResponse({"ack": answer["ack"], "state": answer["state"]}, headers={"Cache-Control": "no-store"})
