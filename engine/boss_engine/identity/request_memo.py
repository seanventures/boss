"""Identity facts read once per request, not once per check (console-latency fix, 2026-10-09).

One console page render asks the API ~40 questions at once, and each of them used to re-read the
same identity rows several times: the session, then the principal, then (in the PDP) the org's
epoch and the principal's status AGAIN, then the project's org — on two separate pooled
connections. Under that concurrency the identity guard was ~40% of the API's time per render.

What this memo keeps, for the life of ONE request only (a context variable the API sets per
request; the threadpool copies the context, so every thread of the request shares the dict):

    ("status", org_id, principal_id) -> the principal's status, read live by authentication
    ("epoch", org_id)                -> the org's identity epoch, read live this request
    ("project", org_id, project_id)  -> whether the project belongs to the org

Nothing outlives the request (and nothing is trusted for more than MAX_AGE_SECONDS even inside a
long streamed one), so revocation is exactly as live as before: a revoked session, a
disabled person or a bumped epoch is seen by the NEXT request, which starts with an empty memo.
Inside a request, an identity mutation (epoch.bump) clears the memo, so a request that changes who
may do what and then checks again sees the change. Outside a request (the worker, a CLI) there is no
memo and every read goes to the database, as before.
"""
from __future__ import annotations

import time
from contextvars import ContextVar, Token
from typing import Any, Optional

MEMO: ContextVar[Optional[dict]] = ContextVar("boss_identity_request_memo", default=None)

_MISSING = object()


def start() -> Token:
    """A fresh memo for the request now starting."""
    return MEMO.set({})


def reset(token: Token) -> None:
    MEMO.reset(token)


#: A fact older than this is read again even inside its request: a streamed response (a log
#: tail, an event stream) is one request that can live for minutes, and a check it repeats as it
#: goes must see a revocation as promptly as a new request would.
MAX_AGE_SECONDS = 2.0


def get(key: tuple) -> Any:
    memo = MEMO.get()
    if memo is None:
        return _MISSING
    entry = memo.get(key)
    if entry is None or time.monotonic() - entry[1] > MAX_AGE_SECONDS:
        return _MISSING
    return entry[0]


def has(value: Any) -> bool:
    return value is not _MISSING


def put(key: tuple, value: Any) -> None:
    memo = MEMO.get()
    if memo is not None:
        memo[key] = (value, time.monotonic())


def clear() -> None:
    """An identity mutation in this request: nothing read before it may be reused after it."""
    memo = MEMO.get()
    if memo is not None:
        memo.clear()


class RequestMemoMiddleware:
    """Pure ASGI: one memo per HTTP request. NEVER a websocket: a socket lives for hours and
    re-checks as it goes, so every one of its checks must read live (no memo is set for it)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = start()
        try:
            await self.app(scope, receive, send)
        finally:
            reset(token)
