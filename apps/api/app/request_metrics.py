"""What one request cost: its SQL statements, their time, and how much of it was identity.

A request carries a `RequestMetrics` in a context variable (set by `RequestMetricsMiddleware`, the
outermost layer). SQLAlchemy's cursor events count every statement executed while it is current —
including those run in the threadpool, which copies the context and so shares the same object.

Two readers:
  * `Server-Timing` on every response when BOSS_SERVER_TIMING is on (off by default: the counts say
    nothing secret, but an install has no reason to publish them). `total`, `db` (with the number
    of statements) and `auth` (the identity guard's share). Browser devtools and `curl -i` show it.
  * the performance budget tests (apps/api/tests/test_request_budget.py), which read
    `last_metrics()` in process: a regression that adds a query per request fails there, not on
    an operator's install.
"""
from __future__ import annotations

import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine


@dataclass
class RequestMetrics:
    started: float = field(default_factory=time.perf_counter)
    queries: int = 0
    writes: int = 0
    db_ms: float = 0.0
    auth_ms: float = 0.0
    auth_queries: int = 0
    statements: list[str] = field(default_factory=list)
    keep_statements: bool = False
    _auth_depth: int = 0

    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0

    def header(self) -> str:
        return (f'total;dur={self.total_ms():.1f}, db;dur={self.db_ms:.1f};desc="{self.queries} queries", '
                f'auth;dur={self.auth_ms:.1f};desc="{self.auth_queries} queries"')


CURRENT: ContextVar[Optional[RequestMetrics]] = ContextVar("boss_request_metrics", default=None)
_LAST: list[RequestMetrics] = []
_LAST_LOCK = threading.Lock()
_INSTALLED = False


def current() -> Optional[RequestMetrics]:
    return CURRENT.get()


def last_metrics() -> Optional[RequestMetrics]:
    """The most recently finished request's metrics (tests)."""
    with _LAST_LOCK:
        return _LAST[-1] if _LAST else None


def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    m = CURRENT.get()
    if m is not None:
        context._boss_t0 = time.perf_counter()


def _after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    m = CURRENT.get()
    if m is None:
        return
    t0 = getattr(context, "_boss_t0", None)
    m.queries += 1
    if t0 is not None:
        m.db_ms += (time.perf_counter() - t0) * 1000.0
    write = statement.lstrip()[:7].upper().startswith(("INSERT", "UPDATE", "DELETE"))
    if write:
        m.writes += 1
    if m.keep_statements:
        m.statements.append(("W " if write else "") + " ".join(statement.split())[:200])


class _AuthSpan:
    """Charge the enclosed time and statements to identity (nested spans count once)."""

    def __enter__(self) -> "_AuthSpan":
        self.m = CURRENT.get()
        if self.m is not None:
            self.m._auth_depth += 1
            if self.m._auth_depth == 1:
                self.t0, self.q0 = time.perf_counter(), self.m.queries
        return self

    def __exit__(self, *exc: Any) -> None:
        m = self.m
        if m is None:
            return
        m._auth_depth -= 1
        if m._auth_depth == 0:
            m.auth_ms += (time.perf_counter() - self.t0) * 1000.0
            m.auth_queries += m.queries - self.q0


def auth_span() -> _AuthSpan:
    return _AuthSpan()


def install_sql_counters() -> None:
    """Count every statement on every engine of this process (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    event.listen(Engine, "before_cursor_execute", _before)
    event.listen(Engine, "after_cursor_execute", _after)


def server_timing_enabled() -> bool:
    return (os.environ.get("BOSS_SERVER_TIMING") or "").strip().lower() in {"1", "true", "yes", "on"}


def _log_line(scope: dict, status: int, m: RequestMetrics) -> None:
    """BOSS_METRICS_LOG=<file>: one JSON line per request (the e2e performance walk reads it to
    count what a page render asked the API for). Off unless set; never carries a header or body."""
    path = os.environ.get("BOSS_METRICS_LOG")
    if not path:
        return
    import json

    headers = dict(scope.get("headers") or [])
    cred = ("session" if b"boss_session" in headers.get(b"cookie", b"") else
            "bearer" if headers.get(b"authorization") or headers.get(b"x-boss-api-key") else "none")
    line = json.dumps({"t": time.time(), "method": scope.get("method"), "path": scope.get("path"),
                       "cred": cred,
                       "query": (scope.get("query_string") or b"").decode("latin-1")[:300],
                       "status": status, "ms": round(m.total_ms(), 2), "queries": m.queries,
                       "writes": m.writes, **({"sql": m.statements} if m.keep_statements else {}),
                       "db_ms": round(m.db_ms, 2), "auth_ms": round(m.auth_ms, 2),
                       "auth_queries": m.auth_queries})
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class RequestMetricsMiddleware:
    """Pure ASGI, outermost: one RequestMetrics per HTTP request."""

    def __init__(self, app: Any) -> None:
        self.app = app
        install_sql_counters()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        metrics = RequestMetrics(keep_statements=bool(os.environ.get("BOSS_METRICS_KEEP_SQL")))
        token = CURRENT.set(metrics)
        emit = server_timing_enabled()

        async def send_with_timing(message: dict) -> None:
            if emit and message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"server-timing", metrics.header().encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        status: list[int] = [0]

        async def send_recording(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status[0] = int(message.get("status") or 0)
            await send_with_timing(message)

        try:
            await self.app(scope, receive, send_recording)
        finally:
            CURRENT.reset(token)
            _log_line(scope, status[0], metrics)
            with _LAST_LOCK:
                _LAST.append(metrics)
                del _LAST[:-50]
