"""Waiting for BOSS's own work without spending a model call per look (the `wait_for` tool).

THE EXAM (Metatron, 2026-10-24). Asked to make sure three code-graph index jobs (about a minute
each) had finished and then answer a question about the graph, the agent read code_graph_status
eighteen times in one turn, ran out of round-trips and handed back "Next action: poll
code_graph_status again". Every look was a model round-trip; the job did not move any faster.
It had no way to wait, so it polled, and polling is the most expensive way to wait there is.

THE MECHANISM. One read-safe tool call that blocks on the server until a record reaches a state
where waiting longer would not change the answer — finished, failed, cancelled, or waiting for a
person — or until a bounded time passes, and returns the final state. No model is called while
it waits; the state is re-read from the database on an interval. Each kind of record is read by
the readers BOSS already has (repo_index.status for the code graph, the jobs, code_task_runs and
workflow_runs rows) and settled by the terminal sets those modules already define, so the tool
cannot disagree with the console about whether something is done.

THE BOUND. Never longer than `agent_wait_max_seconds` (default 600), and inside a chat turn never
past the turn's own tool time (services/call_deadline.py) less a margin for the answer — so the
console's stall line still holds. Cut short, it says so: what is still running, and how to resume
(`resume`, which the turn offers as a one-press "Keep waiting" next step).

`watch` is the other half and stays separate: it re-runs a model check on an interval in the
conversation, for things that take longer than a turn or live outside BOSS (a pull request's
review, an external CI). This is for BOSS's own records that settle within minutes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text

from . import call_deadline

#: The kinds of record a wait can be on: what each is, and the id prefix it uses.
KINDS: dict[str, str] = {
    "job": "a queued job (job_...)",
    "code_task": "a code task run, a build (crun_...)",
    "workflow_run": "a workflow run (wfr_...)",
    "code_graph": "the project's code graph: every registered repository's index (or the one named)",
}
ID_PREFIX = {"job": "job_", "code_task": "crun_", "workflow_run": "wfr_"}

#: The longest one wait may block, when the install sets nothing (BOSS_AGENT_WAIT_MAX_SECONDS).
DEFAULT_MAX_SECONDS = 600
#: Asked for nothing: how long a wait blocks.
DEFAULT_SECONDS = 300
#: Inside a turn, the seconds left for the answer after the wait returns.
TURN_MARGIN_SECONDS = 10.0
#: Re-read interval: starts short (a job about to finish is reported promptly) and grows.
FIRST_POLL_SECONDS = 2.0
MAX_POLL_SECONDS = 10.0

# Seams for tests: the clock and the sleep.
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


class WaitRefused(ValueError):
    """The wait cannot start: an unknown kind, a missing or foreign id."""


@dataclass
class Look:
    """One read of the record: its state, and whether waiting longer could change it."""
    state: dict[str, Any]
    settled: bool
    #: Why it is settled, in words: "succeeded", "failed", "needs a person".
    outcome: str = ""


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _job(connection, project_id: str, ref: str, _repository: str) -> Look:
    from .shepherd_watch import TERMINAL_JOB_STATUSES

    row = connection.execute(text(
        "SELECT id, kind, status, attempt, max_attempts, last_error, run_after, updated_at "
        "FROM jobs WHERE id = :id AND project_id = :p"), {"id": ref, "p": project_id}
    ).mappings().first()
    if row is None:
        raise WaitRefused(f"no job {ref} in this project")
    status = str(row["status"])
    return Look(_jsonable(dict(row)), status in TERMINAL_JOB_STATUSES, status)


def _code_task(connection, project_id: str, ref: str, _repository: str) -> Look:
    from .code_task_service import TERMINAL_RUN_STATUSES

    row = connection.execute(text(
        "SELECT id, status, phase, title, error, updated_at FROM code_task_runs "
        "WHERE id = :id AND project_id = :p"), {"id": ref, "p": project_id}).mappings().first()
    if row is None:
        raise WaitRefused(f"no code task run {ref} in this project")
    status = str(row["status"])
    # blocked and deadlettered do not move again without someone acting on them.
    settled = status in TERMINAL_RUN_STATUSES or status in ("blocked", "deadlettered")
    return Look(_jsonable(dict(row)), settled, status)


def _workflow_run(connection, project_id: str, ref: str, _repository: str) -> Look:
    from .workflows import TERMINAL_RUN_STATES

    row = connection.execute(text(
        "SELECT id, status, error, blocked_reason, created_at, completed_at FROM workflow_runs "
        "WHERE id = :id AND project_id = :p"), {"id": ref, "p": project_id}).mappings().first()
    if row is None:
        raise WaitRefused(f"no workflow run {ref} in this project")
    status = str(row["status"])
    state = _jsonable(dict(row))
    if status in TERMINAL_RUN_STATES or status in ("paused", "blocked"):
        return Look(state, True, status)
    # A step held on a person's decision does not move by waiting: say so now. Candidates are
    # selected in SQL (decision_actions.NEEDS_ACTION_CANDIDATE_STATUSES: never this module's own
    # list, which would disagree with the Approvals inbox about what still needs a person — see
    # engine/tests/test_every_surface_agrees_on_what_needs_the_operator.py) and classified by
    # `attention` here, same as every other reader.
    from sqlalchemy import bindparam

    from .decision_actions import NEEDS_ACTION_CANDIDATE_STATUSES, attention

    candidates = connection.execute(text(
        "SELECT s.step_id, d.status, d.deadline_at, d.default_action, d.recommendation "
        "FROM workflow_step_runs s JOIN decisions d ON d.id = s.decision_id "
        "WHERE s.workflow_run_id = :r AND s.status = 'waiting' AND d.status IN :statuses"
    ).bindparams(bindparam("statuses", expanding=True)),
        {"r": ref, "statuses": list(NEEDS_ACTION_CANDIDATE_STATUSES)}).mappings().all()
    asks = [str(row["step_id"]) for row in candidates if attention(row) == "needs_action"]
    if asks:
        state["waiting_on_a_person"] = asks
        return Look(state, True, "needs a person")
    return Look(state, False, status)


def _code_graph(connection, project_id: str, _ref: str, repository: str) -> Look:
    from .knowledge import repo_index

    rows = repo_index.status(connection, project_id)["repositories"]
    if repository:
        rows = [r for r in rows if repository in (r.get("repo"), r.get("binding_id"))]
        if not rows:
            raise WaitRefused(f"no registered repository {repository} in this project")
    if not rows:
        raise WaitRefused("no repository is registered in this project, so nothing is indexing")
    # Settled per repository: not queued, running or never yet queued — or queued behind a job
    # that is no longer live (it will not move until it is asked again).
    open_ = [r for r in rows
             if r["status"] == "pending"
             or (r["status"] in ("queued", "running") and repo_index._in_flight(connection, r))]
    state = {"repositories": [{k: r.get(k) for k in ("repo", "status", "job_id", "error",
                                                       "indexed_at", "graph", "summary")}
                              for r in rows]}
    outcome = ", ".join(f"{r['repo']}: {r['status']}" for r in rows)
    return Look(state, not open_, outcome)


READERS: dict[str, Callable[..., Look]] = {
    "job": _job, "code_task": _code_task, "workflow_run": _workflow_run,
    "code_graph": _code_graph,
}


def kind_of(ref: str) -> Optional[str]:
    """The kind an id's prefix names, or None."""
    for kind, prefix in ID_PREFIX.items():
        if ref.startswith(prefix):
            return kind
    return None


def max_seconds(settings: Any) -> float:
    try:
        value = float(getattr(settings, "agent_wait_max_seconds", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else float(DEFAULT_MAX_SECONDS)


def wait(begin: Callable[[], Any], settings: Any, *, project_id: str, what: str, ref: str = "",
         repository: str = "", timeout_seconds: Optional[float] = None) -> dict[str, Any]:
    """Block until the record settles or the bound passes; return its last state.

    `begin` opens a short transaction per read (ctx.begin), so each look sees what the worker
    has committed since the last. Raises WaitRefused when the wait cannot start."""
    what = (what or "").strip() or (kind_of(ref) or "")
    if what not in READERS:
        raise WaitRefused(f"what must be one of {sorted(READERS)}")
    if what != "code_graph":
        if not ref:
            raise WaitRefused(f"give the id of the {what} to wait for")
        named = kind_of(ref)
        if named and named != what:
            raise WaitRefused(f"{ref} is a {named}, not a {what}")
    asked = float(timeout_seconds) if timeout_seconds not in (None, "") else DEFAULT_SECONDS
    bound = max(0.0, min(asked, max_seconds(settings)))
    left = call_deadline.remaining()
    cut_by_turn = False
    if left is not None and left - TURN_MARGIN_SECONDS < bound:
        bound, cut_by_turn = max(0.0, left - TURN_MARGIN_SECONDS), True
    reader = READERS[what]
    started = _clock()
    poll = FIRST_POLL_SECONDS
    looks = 0
    while True:
        with begin() as connection:
            look = reader(connection, project_id, ref, repository)
        looks += 1
        waited = _clock() - started
        if look.settled:
            return {"ok": True, "done": True, "what": what, "id": ref or None,
                    "outcome": look.outcome, "waited_seconds": round(waited, 1),
                    "reads": looks, "state": look.state}
        remaining = bound - waited
        if remaining <= 0:
            break
        _sleep(min(poll, remaining))
        poll = min(MAX_POLL_SECONDS, poll * 1.5)
    subject = f"{what} {ref}".strip() if ref else (
        f"the code graph of {repository}" if repository else "the project's code graph")
    why = ("this turn's time ran out" if cut_by_turn
           else f"the {int(bound)} s wait passed")
    return {
        "ok": True, "done": False, "what": what, "id": ref or None,
        "outcome": f"still {look.outcome}" if look.outcome else "still running",
        "waited_seconds": round(_clock() - started, 1), "reads": looks, "state": look.state,
        "note": (f"Not settled yet: {why}. It is still running; nothing was lost by stopping "
                 "the wait. Say what is still open and do not poll it again this turn."),
        # The one-press resume (intent_router turns it into a next step).
        "resume": {"label": "Keep waiting",
                   "message": (f"Keep waiting for {subject} with wait_for, then carry on with "
                               "what is left of my request.")},
    }
