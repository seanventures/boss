"""One budget for one chat turn — every model call in it, first pass and every re-ask.

THE INCIDENT (boss-1, 2026-10-02). Asked why a Preview step had failed, one turn chained a
first pass (up to 14 model round-trips), a role-grounding re-ask (4), a continuation (10) and
could have added an action-claim reconcile (4): up to 32 model calls, each bounded only by the
CLI's own 240-second timeout. One step alone took 157 seconds. The operator watched "Still
working — this turn is running long" for 276 seconds and then got an answer about something
else.

Each pass had a step limit; the TURN had none. So the limit lives here, on the turn, and every
`agent.chat` call in it draws from the same pool. When it is spent the agent's next round-trip
is the answer step — no tools — and it is told to say what it could not check. A re-ask that
finds the budget spent does not start.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TurnBudget:
    #: Wall-clock seconds the turn may spend before it must answer.
    seconds: float = 180.0
    #: Model round-trips across the whole turn.
    model_calls: int = 16
    clock: Callable[[], float] = time.monotonic
    started: float = field(default=-1.0)
    used: int = 0
    #: Seconds held back for the answer step itself: starting another tool round-trip with less
    #: than this left is how a turn overruns.
    answer_reserve: float = 20.0

    def __post_init__(self) -> None:
        if self.started < 0:
            self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    @property
    def calls_left(self) -> int:
        return max(0, self.model_calls - self.used)

    def spend_call(self) -> None:
        self.used += 1

    def must_answer_now(self) -> bool:
        """True when the NEXT model call has to be the answer: one call left, or no time."""
        return self.calls_left <= 1 or self.elapsed >= self.seconds - self.answer_reserve

    def spent(self) -> bool:
        """Nothing left for another pass — a re-ask must not start."""
        return self.calls_left <= 1 or self.elapsed >= self.seconds - self.answer_reserve

    def steps_for(self, requested: int) -> int:
        """A pass's step limit, cut to what the turn has left."""
        return max(1, min(int(requested), self.calls_left))

    def describe(self) -> str:
        return (f"{self.used} of {self.model_calls} model calls, "
                f"{int(self.elapsed)}s of {int(self.seconds)}s")
