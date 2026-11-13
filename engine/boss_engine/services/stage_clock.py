"""THE STAGE CLOCK STOPS WHILE A BUILD AGENT WAITS FOR A PERSON (BOSSAPP-814).

WHY. A build stage has a wall-time budget (governed `build_implement_budget_minutes`, carried as
the llm context's `stage_deadline`), a CLI session has a timeout cut to it (code_agent
`_cap_to_deadline`) and an idle killer (a session silent for `_IDLE_TIMEOUT` is ended as
stalled). With the MCP gateway (mcp_gateway.py) an agent's tool call can PARK while a person
answers a decision. That wait is the person's time, not the agent's: counted against the stage,
twenty minutes in the Approvals queue failed a build that was working, and the silent session
looked exactly like a stall and was killed.

So the gateway marks a run PARKED for as long as one of its calls waits (`parked`), and every
place that meters the stage reads the paused time back:

  * `extended(run_id, deadline, since=...)` — a deadline moved out by the time the run spent
    parked since the deadline was set (code_agent._stage_deadline, the governed implement
    budget's one seam);
  * `paused_between(run_id, t0)` — parked seconds since `t0` (a session's own timeout);
  * `is_parked(run_id)` — a session that is silent because it is parked is not stalled.

Process-local and in memory, like the model proxy's grants: the gateway runs in the process that
started the session, which is the process that meters it. Several parked calls of one run (two
parallel asks) are one pause, not two: the intervals are merged before they are summed.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Iterator, Optional

_LOCK = threading.Lock()
#: run_id -> [[start, end_or_None], ...] (wall-clock seconds).
_PARKS: dict[str, list[list[Optional[float]]]] = {}
#: (run_id, deadline) -> when this deadline was first read: the start of its stage's window when
#: the caller cannot say (`extended(since=None)`).
_FIRST_SEEN: dict[tuple[str, float], float] = {}
_MAX_INTERVALS = 2_000
_MAX_ANCHORS = 4_096


@contextlib.contextmanager
def parked(run_id: Optional[str]) -> Iterator[None]:
    """The run is waiting on a person for the duration of the block."""
    if not run_id:
        yield
        return
    interval: list[Optional[float]] = [time.time(), None]
    with _LOCK:
        spans = _PARKS.setdefault(str(run_id), [])
        spans.append(interval)
        if len(spans) > _MAX_INTERVALS:
            del spans[: len(spans) - _MAX_INTERVALS]
    try:
        yield
    finally:
        with _LOCK:
            interval[1] = time.time()


def is_parked(run_id: Optional[str]) -> bool:
    if not run_id:
        return False
    with _LOCK:
        return any(end is None for _start, end in _PARKS.get(str(run_id), ()))


def paused_between(run_id: Optional[str], t0: float, t1: Optional[float] = None) -> float:
    """Seconds the run was parked inside [t0, t1] (t1: now), overlapping parks counted once."""
    if not run_id:
        return 0.0
    now = time.time()
    end_at = now if t1 is None else float(t1)
    with _LOCK:
        spans = [(float(s), float(e if e is not None else now))
                 for s, e in _PARKS.get(str(run_id), ())]
    clipped = sorted((max(s, t0), min(e, end_at)) for s, e in spans
                     if min(e, end_at) > max(s, t0))
    total, cur_s, cur_e = 0.0, None, None
    for s, e in clipped:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def extended(run_id: Optional[str], deadline: Optional[float], *,
             since: Optional[float] = None) -> Optional[float]:
    """`deadline` moved out by the time `run_id` spent parked after `since` (the moment the
    deadline was set). Without `since`, the first time this deadline was read for the run is
    used: a stage reads its deadline when its first session starts, before anything could park.
    None stays None; a run that never parked gets its deadline back unchanged."""
    if deadline is None or not run_id:
        return deadline
    key = (str(run_id), float(deadline))
    if since is None:
        with _LOCK:
            if key not in _FIRST_SEEN and len(_FIRST_SEEN) >= _MAX_ANCHORS:
                _FIRST_SEEN.clear()
            since = _FIRST_SEEN.setdefault(key, time.time())
    with _LOCK:
        if str(run_id) not in _PARKS:
            return deadline
    return float(deadline) + paused_between(run_id, float(since))


def forget(run_id: Optional[str]) -> None:
    """The run is over: drop what was kept for it."""
    if not run_id:
        return
    with _LOCK:
        _PARKS.pop(str(run_id), None)
        for key in [k for k in _FIRST_SEEN if k[0] == str(run_id)]:
            del _FIRST_SEEN[key]


__all__ = ["extended", "forget", "is_parked", "parked", "paused_between"]
