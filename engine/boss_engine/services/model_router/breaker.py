"""A circuit breaker per source — "never stranded" (docs/product/MODEL_ROUTER.md, Decision 14).

A call-time failure that says nothing about the work (a 5xx, a timeout, a refused connection,
a provider at capacity) marks its source failing for a short time. Selection skips a failing
source, and the step that failed retries at once on the next source in its tier: a failed call
has no session to preserve. In-process on purpose — it is about the next few seconds, and every
worker learns it from its own calls; the durable facts (limits, resets) live in headroom.

HALF-OPEN (Decision 21): an open breaker is a guess that the source is still down. When nothing
else can take the work, selection asks `claim_trial` for ONE trial call on it — at most one per
source every TRIAL_GAP_S — instead of refusing. A trial that succeeds closes the breaker
(`close`); one that fails re-trips it, and the caller waits (failover.wait_for_route).
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

#: How long a source stays out after a transient failure, unless the provider said how long.
DEFAULT_TTL_S = 90.0

#: At most one half-open trial call per source in this many seconds.
TRIAL_GAP_S = 20.0

_LOCK = threading.Lock()
_OPEN: dict[str, tuple[float, str]] = {}
_TRIALS: dict[str, float] = {}

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
    now = time.monotonic()
    with _LOCK:
        _OPEN[engine] = (now + (seconds or DEFAULT_TTL_S), str(reason)[:200])
        # A failure IS an attempt: the next half-open trial waits TRIAL_GAP_S from it.
        _TRIALS[engine] = now


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


def claim_trial(engine: str) -> bool:
    """One half-open trial call on an open breaker: True (and the trial is taken) when none was
    granted for `engine` in the last TRIAL_GAP_S."""
    now = time.monotonic()
    with _LOCK:
        if now - _TRIALS.get(engine, -1e9) < TRIAL_GAP_S:
            return False
        _TRIALS[engine] = now
        return True


def seconds_until_trial(engine: str) -> float:
    """How long until `engine` may be tried again: its next half-open trial or its breaker
    closing, whichever comes first (0 when it is not open)."""
    now = time.monotonic()
    with _LOCK:
        hit = _OPEN.get(engine)
        if not hit or now >= hit[0]:
            return 0.0
        trial = _TRIALS.get(engine, -1e9) + TRIAL_GAP_S - now
        return max(0.0, min(hit[0] - now, trial))


def close(engine: str) -> None:
    """A call on `engine` succeeded: it is not failing (a half-open trial that worked)."""
    with _LOCK:
        _OPEN.pop(engine, None)
        _TRIALS.pop(engine, None)


def reset(engine: Optional[str] = None) -> None:
    with _LOCK:
        if engine is None:
            _OPEN.clear()
            _TRIALS.clear()
        else:
            _OPEN.pop(engine, None)
            _TRIALS.pop(engine, None)
