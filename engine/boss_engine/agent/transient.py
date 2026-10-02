"""Retry a provider hiccup without retrying work that already happened.

Anthropic's 529 is `overloaded_error` — capacity, not a bad request. The coding pipeline
has always treated that as transient. These helpers are the shared definition of "try
again" so the chat path and the coding path cannot drift into disagreeing.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

#: Kept in the same words as code_agent's list so the two cannot drift.
_TRANSIENT_MARKERS = (
    "529", "503", "overloaded", "capacity", "bad gateway", "gateway timeout",
    "connection reset", "connection closed", "connection error", "timed out",
)


def _looks_transient(error: BaseException) -> bool:
    from ..services.call_deadline import TurnDeadlineExceeded

    # THE TURN'S CLOCK IS NOT A HICCUP. Retrying a call the turn's deadline cut off spent the
    # same wait again, three times (call_deadline.py).
    if isinstance(error, TurnDeadlineExceeded):
        return False
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _with_transient_retry(call, attempts: int = 3, backoff: float = 2.0):
    """Run `call`, retrying a provider hiccup a couple of times before giving up.

    Short and bounded on purpose: a person is waiting on this turn, so two extra attempts a few
    seconds apart is the difference between "the provider was busy for a moment" and a dead end
    the operator has to notice and work around. Anything that does not look transient is raised
    immediately — retrying a bad request just spends the wait twice.

    `call` must be safe to run again. A model completion is. A chat turn that has already
    executed redeploy / start_preview / write_db is not — retry that completion, not the turn.
    """
    last: BaseException
    for attempt in range(attempts):
        try:
            return call()
        except BaseException as exc:  # noqa: BLE001 — re-raised below unless transient
            last = exc
            if attempt == attempts - 1 or not _looks_transient(exc):
                raise
            # A retry that cannot finish inside the turn is not worth starting.
            from ..services.call_deadline import MIN_CALL_SECONDS, remaining

            left = remaining()
            if left is not None and left < backoff * (attempt + 1) + MIN_CALL_SECONDS:
                raise
            _log.warning(
                "answer attempt %d/%d hit a transient provider error (%s) — retrying in %.0fs",
                attempt + 1, attempts, type(exc).__name__, backoff * (attempt + 1),
            )
            time.sleep(backoff * (attempt + 1))
    raise last
