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
When the request ends its memo is CLOSED, not just unset: a task or thread it started that still
holds the dict (asyncio.create_task copies the context) reads live from then on.
Inside a request, an identity mutation TURNS THE MEMO OFF for the rest of that request: an explicit
epoch.bump, and any write to a table the epoch trigger watches (change_feed.WATCHED), which moves
the epoch in the database without passing through bump(). Off, not cleared: a read made between
the write and its commit, on a fresh connection, would see the OLD committed state and memoise it
for after the commit (rc6.1 security verification). Outside a request (the worker, a CLI) there is no
memo and every read goes to the database, as before.
"""
from __future__ import annotations

import re
import time
from contextvars import ContextVar, Token
from typing import Any, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

MEMO: ContextVar[Optional[dict]] = ContextVar("boss_identity_request_memo", default=None)

_MISSING = object()

#: The memo's own off switch (a key no identity fact uses: facts are tuples).
_OFF = "off"


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
    if memo is None or memo.get(_OFF):
        return _MISSING
    entry = memo.get(key)
    if entry is None or time.monotonic() - entry[1] > MAX_AGE_SECONDS:
        return _MISSING
    return entry[0]


def has(value: Any) -> bool:
    return value is not _MISSING


def put(key: tuple, value: Any) -> None:
    memo = MEMO.get()
    if memo is not None and not memo.get(_OFF):
        memo[key] = (value, time.monotonic())


def active() -> bool:
    """Is there a memo this request may still use?"""
    memo = MEMO.get()
    return memo is not None and not memo.get(_OFF)


def clear() -> None:
    """An identity mutation in this request: nothing read before it may be reused after it, and
    nothing read from here on is memoised (it may predate the mutation's commit)."""
    memo = MEMO.get()
    if memo is not None:
        _close(memo)


def _close(memo: dict) -> None:
    memo.clear()
    memo[_OFF] = True


#: A write statement to an identity table (the epoch trigger fires on the watched ones).
_IDENTITY_WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(identity_\w+)", re.I)


@event.listens_for(Engine, "before_cursor_execute")
def _identity_write_turns_the_memo_off(conn: Any, cursor: Any, statement: str, parameters: Any,
                                       context: Any, executemany: bool) -> None:
    memo = MEMO.get()
    if memo is None or memo.get(_OFF):
        return
    from .change_feed import WATCHED

    if any(table.lower() in WATCHED for table in _IDENTITY_WRITE.findall(statement or "")):
        _close(memo)


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
        memo = MEMO.get()
        try:
            await self.app(scope, receive, send)
        finally:
            _close(memo)    # anything the request started that still holds it reads live now
            reset(token)
