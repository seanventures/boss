"""Run a build's review seats side by side, each under its OWN deadline.

THE ONE PLACE a panel of seats is run: the first review panel and every fix round's re-review
both come here, so neither can be left without a deadline or run a seat it said it skipped.

- Only the seats passed are run (a skipped seat is never handed in), and the number at once is
  bounded by `max_workers`, which callers size from the seats actually run.
- A seat's deadline starts when THAT seat starts, not when the panel does: a seat queued
  behind a slow one is not charged for the wait (review_code alone took 336-348 s on
  HelpDesk, so a single 420 s budget for the whole panel starved whoever went last).
- Nor is a seat charged for the time its model calls wait for a slot on the provider's lane
  (BOSSAPP-1450): on a one-slot host the seats take turns at the model, and that queue is not
  the seat's work. Each seat runs under its own stage_clock.WaitMeter; its deadline counts
  elapsed time minus what the meter saw it wait. The hard cap moves out by the same waits.
- A seat past its deadline has its owned processes ended (owned_process.terminate_owner on
  the seat's owner id) and its slot freed, so the seats queued behind it still run. Its
  thread is abandoned, never waited on.
- A hard cap (every seat's deadline, end to end, plus a minute) ends the wait even if a
  seat's thread ignores the kill.
"""

from __future__ import annotations

import contextvars
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

SEAT_OWNER_KIND = "review_seat"


@dataclass
class SeatRun:
    """What happened to each seat: its return value, its error, or its deadline."""

    results: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)
    killed: int = 0
    started: dict[str, float] = field(default_factory=dict)
    #: Seconds each seat waited for a model slot (not counted against its deadline).
    slot_waits: dict[str, float] = field(default_factory=dict)

    def finished(self, label: str) -> bool:
        return label in self.results and label not in self.timed_out


def run_seats(seats: list[tuple[str, Callable[[], Any]]], *, max_workers: int,
              seat_deadline_s: float, owner_id: Callable[[str], str],
              kill_strays: Optional[Callable[[], int]] = None,
              poll_s: float = 0.5, clock: Callable[[], float] = time.monotonic) -> SeatRun:
    """Run `seats` (label, fn) with at most `max_workers` at once, each under its own deadline.

    `owner_id(label)` names the seat's process owner: everything the seat spawns belongs to it,
    and that is what a deadline ends. `kill_strays`, when given, runs once after the panel when a
    seat timed out, at a moment no other seat is running any more."""
    from . import owned_process as _op
    from . import stage_clock

    out = SeatRun()
    if not seats:
        return out
    slots = threading.Semaphore(max(1, int(max_workers or 1)))
    lock = threading.Lock()
    state = {label: {"start": None, "done": False, "released": False, "abandoned": False}
             for label, _ in seats}
    meters = {label: stage_clock.WaitMeter(clock) for label, _ in seats}

    def _free(st):
        # Called with `lock` held. Idempotent: a seat's slot is given back exactly once,
        # by the seat when it ends or by the deadline when it is abandoned.
        if not st["released"]:
            st["released"] = True
            slots.release()

    def _body(label, fn):
        slots.acquire()
        st = state[label]
        with lock:
            if st["abandoned"]:  # the panel moved on before this seat got a slot
                st["done"] = True
                _free(st)
                return
            st["start"] = clock()
            out.started[label] = st["start"]
        try:
            with _op.owner_scope(SEAT_OWNER_KIND, owner_id(label)), \
                    stage_clock.metering(meters[label]):
                value = fn()
            with lock:
                if not st["abandoned"]:
                    out.results[label] = value
        except Exception as exc:  # noqa: BLE001 — a failed seat must not sink the panel
            with lock:
                if not st["abandoned"]:
                    out.errors[label] = f"{label}: {exc.__class__.__name__}: {exc}"[:200]
        finally:
            with lock:
                st["done"] = True
                _free(st)

    threads = []
    for label, fn in seats:
        # A thread starts with an EMPTY context: each gets its own copy of the caller's, so a
        # seat runs inside the build's owner scope and LLM context instead of belonging to nobody.
        t = threading.Thread(target=contextvars.copy_context().run, args=(_body, label, fn),
                             name=f"review-seat-{label}", daemon=True)
        threads.append(t)
        t.start()

    hard_cap = clock() + float(seat_deadline_s) * len(seats) + 60.0
    # The hard cap moves out by the seats' slot waits, but never past a second panel's length:
    # it is the backstop for a seat that ignores its kill, not a deadline of its own.
    most_extra = float(seat_deadline_s) * len(seats)
    while True:
        to_kill: list[str] = []
        with lock:
            now = clock()
            pending = [lb for lb, st in state.items() if not st["done"] and not st["abandoned"]]
            if not pending:
                break
            waited = {lb: meters[lb].waited(now) for lb in state}
            cap = hard_cap + min(most_extra, sum(waited.values()))
            for lb in pending:
                st = state[lb]
                overdue = (st["start"] is not None
                           and now - st["start"] - waited[lb] > seat_deadline_s)
                if overdue or now > cap:
                    st["abandoned"] = True
                    out.timed_out.append(lb)
                    if st["start"] is not None:
                        to_kill.append(lb)
                        _free(st)
        for lb in to_kill:
            try:
                out.killed += int(_op.terminate_owner(owner_id(lb), reason="deadline")
                                  .get("ended") or 0)
            except Exception:  # noqa: BLE001 — a failed kill never sinks the panel
                pass
        time.sleep(poll_s)
    out.slot_waits = {lb: round(m.waited(), 1) for lb, m in meters.items() if m.waited() > 0}
    if out.timed_out and kill_strays is not None:
        try:
            out.killed += int(kill_strays() or 0)
        except Exception:  # noqa: BLE001
            pass
    return out
