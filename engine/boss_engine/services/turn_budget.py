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
    #: Seconds the ANSWER call may run past `seconds`. A tool round-trip must end by
    #: `seconds`; the answer that follows gets this window and no more, so a turn ends by
    #: `seconds + answer_seconds` whatever the model does — inside the console's 240 s stall
    #: line (tests/test_a_turn_ends_before_the_console_gives_up.py holds the two together).
    answer_seconds: float = 40.0
    #: The longest gap seen between two model calls in this turn — one round-trip, tools
    #: included. See `_time_needed`.
    longest_round_trip: float = 0.0
    _last_call_at: float = field(default=-1.0, repr=False)

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
        now = self.clock()
        if self._last_call_at >= 0:
            self.longest_round_trip = max(self.longest_round_trip, now - self._last_call_at)
        self._last_call_at = now
        self.used += 1

    def _time_needed(self) -> float:
        """What another round-trip and then the answer will need: at least the reserve, and at
        least the longest round-trip this turn has already taken.

        A FIXED 20 s RESERVE AGAINST 157 s CALLS. boss-1 measured one model round-trip at 157 s;
        with a 20 s reserve a tool round-trip could start at 159 s of a 180 s turn and the
        forced answer only after it — two full round-trips past the clock (review of 374ba07).
        Measured from the turn itself, the answer starts in time to end within one round-trip
        of the budget."""
        # The call just made counts as soon as it has come back: asked before the next call,
        # the time since the last one started IS that round-trip.
        current = self.clock() - self._last_call_at if self._last_call_at >= 0 else 0.0
        return max(self.answer_reserve, self.longest_round_trip, current)

    def must_answer_now(self) -> bool:
        """True when the NEXT model call has to be the answer: one call left, or no time."""
        return self.calls_left <= 1 or self.elapsed >= self.seconds - self._time_needed()

    def spent(self) -> bool:
        """Nothing left for another pass — a re-ask must not start."""
        return self.calls_left <= 1 or self.elapsed >= self.seconds - self._time_needed()

    def deadline(self, *, answering: bool) -> float:
        """When the next model call must have returned, on time.monotonic()'s clock.

        What services/call_deadline.py hands every adapter, so the budget is enforced DURING a
        call and not only between calls (the owner, 2026-10-02: "it really times out after
        spending 240s there").
        """
        left = self.seconds + (self.answer_seconds if answering else 0.0) - self.elapsed
        return time.monotonic() + left

    def steps_for(self, requested: int) -> int:
        """A pass's step limit, cut to what the turn has left."""
        return max(1, min(int(requested), self.calls_left))

    def describe(self) -> str:
        return (f"{self.used} of {self.model_calls} model calls, "
                f"{int(self.elapsed)}s of {int(self.seconds)}s")


# ── How big a turn's budget is ──────────────────────────────────────────────────────────────────
#
# ONE CONSTANT FOR EVERY TURN WAS TOO SMALL FOR A REAL ONE AND TOO BIG FOR A PLAIN QUESTION
# (Metatron, 2026-10-24): "make sure the three code indexes finished, then query the graph" ran
# out of its 14 round-trips in every turn it was asked, and handed back "poll it again". The
# turn's decision (agent_decision, in route_turn) already says what kind of turn it is, so the
# budget is sized from that — never from the words — under one hard ceiling. The time is the
# same for every turn: it is what the console's stall line allows.

LIGHT = "light"
DEEP = "deep"
#: The decided actions that look into or act on the work, and so need a chain of reads.
DEEP_ACTIONS = frozenset({"investigate", "operate", "new_code_change", "create_project"})


def turn_depth(action: str | None, depth: str | None) -> str:
    """LIGHT for a question (or a turn nothing decided); DEEP for a decision to investigate or
    act, and for a pressed "Investigate further" / "Continue" (depth=investigate)."""
    if depth == "investigate" or (action or "") in DEEP_ACTIONS:
        return DEEP
    return LIGHT


def _int(settings, name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def for_turn(settings, *, action: str | None, depth: str | None) -> tuple[TurnBudget, int, str]:
    """(the turn's budget, the first pass's step limit, LIGHT|DEEP) for a decided turn.

    Model calls and steps are both clamped to `chat_turn_model_calls_ceiling`: a setting can
    make a turn smaller, never bigger than the ceiling."""
    kind = turn_depth(action, depth)
    ceiling = _int(settings, "chat_turn_model_calls_ceiling", 40)
    if kind == DEEP:
        calls = _int(settings, "chat_deep_turn_model_calls", 32)
        steps = _int(settings, "chat_deep_max_steps", 28)
    else:
        calls = _int(settings, "chat_turn_model_calls", 16)
        steps = _int(settings, "chat_max_steps", 14)
    calls = min(calls, ceiling)
    budget = TurnBudget(seconds=float(_int(settings, "chat_turn_seconds", 180)),
                        model_calls=calls)
    return budget, budget.steps_for(min(steps, ceiling)), kind


# ── What a turn that stopped short offers next ──────────────────────────────────────────────────
#
# "I'm out of tool budget … Next action: poll code_graph_status again" asked the person to type
# the request again. A turn that ran out, or a wait that was cut short, ends with ONE press that
# resumes it instead: a next step (RouterTurn.next_steps), which the console and Metatron already
# render as a button / numbered choice and send as a deep turn.

#: The resume for a turn that ran out of its budget while it still had work to do.
CONTINUE_STEP: dict[str, str] = {
    "id": "continue", "label": "Continue", "depth": "investigate",
    "message": ("Continue where your last reply stopped: read the current state again first, "
                "then do what it listed as left."),
}


def continue_steps(passes, *, budget_ran_out: bool = False) -> list[dict[str, str]]:
    """The next steps for a turn: "Keep waiting" when its last wait on some record was cut short
    (a tool result carrying `resume`), else "Continue" when a pass was made to answer early or a
    re-ask could not start for want of budget; nothing when the turn finished on its own."""
    latest: dict[str, dict] = {}
    exhausted = budget_ran_out
    for one in passes or ():
        if one is None:
            continue
        exhausted = exhausted or bool(getattr(one, "exhausted", ""))
        for step in getattr(one, "trace", None) or []:
            result = step.get("result")
            if not isinstance(result, dict) or not ("resume" in result or "done" in result):
                continue
            args = {k: v for k, v in (step.get("args") or {}).items() if k != "timeout_seconds"}
            key = f"{step.get('tool')}:{sorted(args.items(), key=lambda kv: kv[0])!r}"
            latest[key] = result          # a later wait on the same record supersedes it
    resumes = [r["resume"] for r in latest.values()
               if isinstance(r.get("resume"), dict) and r["resume"].get("message")]
    if resumes:
        said = list(dict.fromkeys(str(r["message"]) for r in resumes))
        return [{"id": "keep_waiting", "label": str(resumes[-1].get("label") or "Keep waiting"),
                 "depth": "investigate", "message": " ".join(said)}]
    return [dict(CONTINUE_STEP)] if exhausted else []
