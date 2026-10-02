"""The time a chat turn has left, carried to every model call made inside it.

THE INCIDENT (the owner, 2026-10-02: "it takes so long to find the issue … it really times out
after spending 240s there"). turn_budget.py gave the turn a 180-second budget, but the budget was
only CONSULTED between model calls — the call itself ran on the CLI's own 240-second timeout.
So one model call started at 170 s ran to 410 s. Worse, the timeout then multiplied:

* the CLI raised "claude CLI timed out after 240s";
* FallingBackAdapter read "timed out after" as "this backend is unavailable" and asked the
  fallback brain the same question from the start;
* `_with_transient_retry` read "timed out" as a provider hiccup and ran the whole call again,
  three times.

One hung call could hold a turn for well over ten minutes while the console, at 240 s, said
"Still working". The console's 240-second stall line was crossed by every slow turn by
construction.

THE RULE. A turn sets its deadline here once (`until`). Every model adapter asks
`bounded_timeout` for its timeout instead of reading its own setting, so no call outlives the
turn. When the turn's time — not the backend — is what ran out, the adapter raises
`TurnDeadlineExceeded`, which is neither "unavailable" (no fallback) nor "transient" (no retry):
the agent answers from what it has already gathered and says what to do next.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

#: Monotonic time by which the current turn's model calls must have returned; None outside a turn.
_DEADLINE: ContextVar[Optional[float]] = ContextVar("boss_model_call_deadline", default=None)

#: Below this many seconds a model call cannot usefully start: it would be cut off mid-thought.
MIN_CALL_SECONDS = 5.0


class TurnDeadlineExceeded(RuntimeError):
    """The TURN's time ran out during, or before, a model call.

    Its message deliberately avoids the words FallingBackAdapter and `_with_transient_retry`
    match on ("timed out", "unavailable"): the backend did nothing wrong, so neither asking
    another backend nor asking again would help — both only spend more of a person's wait.
    """


@contextmanager
def until(deadline: float) -> Iterator[None]:
    """Bound every model call in this block to end by `deadline` (time.monotonic seconds).

    Nested blocks can only tighten the bound, never loosen it: an inner pass of a turn may not
    give itself more time than the turn has.
    """
    current = _DEADLINE.get()
    token = _DEADLINE.set(deadline if current is None else min(current, deadline))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def enter(deadline: float):
    """`until` for code whose block does not fit a `with`: returns the token `leave` takes."""
    current = _DEADLINE.get()
    return _DEADLINE.set(deadline if current is None else min(current, deadline))


def leave(token) -> None:
    _DEADLINE.reset(token)


def remaining() -> Optional[float]:
    """Seconds left before the deadline, or None when no turn has set one."""
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


def bounded_timeout(default: float) -> float:
    """The timeout a model call should use: its own setting, cut to what the turn has left.

    Raises TurnDeadlineExceeded when too little is left to start a call at all.
    """
    left = remaining()
    if left is None:
        return float(default)
    if left < MIN_CALL_SECONDS:
        raise TurnDeadlineExceeded(
            f"this turn has {max(0, int(left))}s left, too little to ask the model again")
    return float(min(float(default), left))


def deadline_cut(timeout: float, default: float) -> bool:
    """Was `timeout` set by the turn's deadline rather than the adapter's own setting?"""
    return float(timeout) < float(default)
