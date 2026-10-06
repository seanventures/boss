"""A circuit breaker per source — "never stranded" (docs/product/MODEL_ROUTER.md, Decision 14).

A call-time failure that says nothing about the work (a 5xx, a timeout, a refused connection,
a provider at capacity) marks its source failing for a short time. Selection skips a failing
source, and the step that failed retries at once on the next source in its tier: a failed call
has no session to preserve. In-process on purpose — it is about the next few seconds, and every
worker learns it from its own calls; the durable facts (limits, resets) live in headroom.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

#: How long a source stays out after a transient failure, unless the provider said how long.
DEFAULT_TTL_S = 90.0

_LOCK = threading.Lock()
_OPEN: dict[str, tuple[float, str]] = {}

_TRANSIENT = re.compile(
    r"\b(5\d\d)\b|overloaded|timed? ?out|timeout|connection (?:refused|reset|error|aborted)|"
    r"temporarily unavailable|service unavailable|bad gateway|gateway time|upstream|"
    r"econnrefused|econnreset|name resolution|network is unreachable|remote end closed|"
    r"server disconnected|read error",
    re.IGNORECASE)


def kind_of(error: Optional[str]) -> Optional[str]:
    """'limit' | 'transient' | None for a call's error. A limit is the provider saying capacity
    will return (model_router.headroom parses when); transient is the call path failing."""
    if not error:
        return None
    from .headroom import windows_from_limit_message

    if windows_from_limit_message(error):
        return "limit"
    if _TRANSIENT.search(str(error)):
        return "transient"
    return None


def trip(engine: str, reason: str, seconds: Optional[float] = None) -> None:
    with _LOCK:
        _OPEN[engine] = (time.monotonic() + (seconds or DEFAULT_TTL_S), str(reason)[:200])


def open_reason(engine: str) -> Optional[str]:
    """Why `engine` is out right now, or None."""
    with _LOCK:
        hit = _OPEN.get(engine)
        if not hit:
            return None
        if time.monotonic() >= hit[0]:
            _OPEN.pop(engine, None)
            return None
        return hit[1]


def reset(engine: Optional[str] = None) -> None:
    with _LOCK:
        if engine is None:
            _OPEN.clear()
        else:
            _OPEN.pop(engine, None)
