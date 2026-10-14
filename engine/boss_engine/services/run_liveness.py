"""Is this code-task attempt alive and making progress? One answer, for every automatic remedy.

BOSS's own machinery killed a live build of the HelpDesk demo (crun_1839849d, 2026-10-11). The
implement stage had been streaming agent turns for an hour and a half when:

  1. the worker's job-runtime ceiling (a fixed 5400s for the WHOLE job) stopped renewing the
     lease of a build whose single implement turn may itself legitimately run 5400s;
  2. the lease expired, another worker slot reclaimed the job and POSTed /code-tasks/step for
     the same run into the same api process, which was still executing it;
  3. that second execution crashed on entry, and its `finally` ran
     `owned_process.terminate_owner(run_id)` — SIGTERM to every process the run owned,
     including the live agent of the FIRST execution. Three times, five minutes apart; the
     third ended the build with "claude code agent exited 143".

Every one of those remedies judged liveness on its own, by a clock, and none of them asked
whether the run was doing anything. This module is the one place that question is answered:

* `executing_here` / `join_or_claim` — a run already executing in this process is never
  started a second time (and so never torn down by a second execution's cleanup);
* `stall_horizon_seconds` — how long silence must last before it can be a stall: the
  configured agent timeout plus grace, never a fixed window shorter than one agent turn;
* `progressing` — the run is in flight and has produced an agent turn or a state change
  within that horizon (or is executing right here);
* `superseded_by` — a newer attempt of the same work exists (retry lineage, a person's
  workflow-step retry, or the same work fingerprint), so the old one is history.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from ..ports import sql

#: Slack on top of the agent timeout: provisioning, a gate run, a commit between turns.
GRACE_SECONDS = 600
#: Never judge a stall on less than this, whatever the agent timeout says.
MIN_HORIZON_SECONDS = 25 * 60
DEFAULT_AGENT_TIMEOUT_SECONDS = 2700

IN_FLIGHT = ("running", "queued")


def stall_horizon_seconds(settings: Any) -> int:
    """Silence shorter than this is a long agent turn, not a stall."""
    try:
        timeout = int(getattr(settings, "code_agent_timeout_seconds", 0) or 0)
    except (TypeError, ValueError):
        timeout = 0
    timeout = timeout or DEFAULT_AGENT_TIMEOUT_SECONDS
    return max(MIN_HORIZON_SECONDS, timeout + GRACE_SECONDS)


# ------------------------------------------------------------------------------------------ #
# Executions in THIS process
# ------------------------------------------------------------------------------------------ #

class _Execution:
    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


_LOCK = threading.Lock()
_EXECUTING: dict[str, _Execution] = {}


def executing_here(run_id: str) -> bool:
    with _LOCK:
        return str(run_id) in _EXECUTING


def run_once(run_id: str, execute, *, on_join=None) -> Any:
    """Run `execute()` for `run_id` unless this process is already executing it.

    A redelivered job for a run that is still executing here JOINS the live execution: it waits
    for it and returns its result. It never starts a second pipeline, and so never reaches the
    second pipeline's cleanup, which ends every process the run owns."""
    run_id = str(run_id)
    with _LOCK:
        live = _EXECUTING.get(run_id)
        owner = live is None
        if owner:
            live = _EXECUTING[run_id] = _Execution()
    if not owner:
        if on_join is not None:
            try:
                on_join()
            except Exception:  # noqa: BLE001 — telemetry never decides the outcome
                pass
        live.done.wait()
        if live.error is not None:
            raise live.error
        return live.result
    try:
        live.result = execute()
        return live.result
    except BaseException as error:
        live.error = error
        raise
    finally:
        with _LOCK:
            if _EXECUTING.get(run_id) is live:
                _EXECUTING.pop(run_id, None)
        live.done.set()


# ------------------------------------------------------------------------------------------ #
# Progress, from the database
# ------------------------------------------------------------------------------------------ #

def seconds_since_activity(connection, run_id: str) -> Optional[float]:
    """Seconds since the run last did anything observable: an agent turn or a state change."""
    row = connection.execute(sql("""
        SELECT EXTRACT(EPOCH FROM (now() - GREATEST(
                   r.updated_at,
                   COALESCE((SELECT max(t.created_at) FROM agent_turns t
                             WHERE t.code_task_run_id = r.id), r.updated_at))))
        FROM code_task_runs r WHERE r.id = :r"""), {"r": str(run_id)}).first()
    if row is None or row[0] is None:
        return None
    return max(0.0, float(row[0]))


def is_progressing(*, status: Optional[str], seconds_since_activity: Optional[float],
                   horizon_seconds: float, executing: bool = False) -> bool:
    """Pure: an in-flight run that acted within the horizon (or is executing here) is alive."""
    if executing:
        return True
    if str(status or "") not in IN_FLIGHT:
        return False
    return seconds_since_activity is not None and seconds_since_activity < horizon_seconds


def progressing(connection, settings: Any, run_id: str) -> bool:
    row = connection.execute(sql("SELECT status FROM code_task_runs WHERE id = :r"),
                             {"r": str(run_id)}).first()
    status = row[0] if row is not None else None
    return is_progressing(status=status,
                          seconds_since_activity=seconds_since_activity(connection, run_id),
                          horizon_seconds=stall_horizon_seconds(settings),
                          executing=executing_here(run_id))


# ------------------------------------------------------------------------------------------ #
# Supersession: a newer attempt of the same work exists
# ------------------------------------------------------------------------------------------ #

#: SQL predicate, `r` the run judged and `n` a candidate newer run: same lineage (retry_code_task),
#: same workflow step (a person pressed Retry on the step), or the same work fingerprint.
SUPERSEDES_SQL = """
    n.project_id = r.project_id AND n.id <> r.id AND n.created_at > r.created_at
    AND (
        COALESCE(n.finalize_ctx->>'root_run_id', n.id)
            = COALESCE(r.finalize_ctx->>'root_run_id', r.id)
        OR n.finalize_ctx->>'parent_run_id' = r.id
        OR (r.finalize_ctx->'origin'->>'step_run_id' IS NOT NULL
            AND n.finalize_ctx->'origin'->>'step_run_id'
                = r.finalize_ctx->'origin'->>'step_run_id')
        OR (r.spec->>'work_fingerprint' IS NOT NULL
            AND n.spec->>'work_fingerprint' = r.spec->>'work_fingerprint')
    )
"""


def superseded_by(connection, run_id: str) -> Optional[str]:
    """The newest later attempt of this run's work, or None.

    Two questions, one answer. A later attempt in the run's lineage (SUPERSEDES_SQL), OR an
    in-flight run doing the SAME WORK by the rule start_code_task dedupes on
    (code_task_service.in_flight_same_work: the work fingerprint, then the same app in the same
    repo). With only the first, a failed build of an older workflow run (crun_37f4ccef) was not
    "superseded" by the live crun_247435e1 — different fingerprint, same app — so the shepherd
    retried it every two minutes and every retry was deduped onto the live run (2026-10-13).
    A run still in flight is superseded only by NEWER same work, never by an older keeper.
    """
    row = connection.execute(sql(f"""
        SELECT n.id FROM code_task_runs r JOIN code_task_runs n ON {SUPERSEDES_SQL}
        WHERE r.id = :r ORDER BY n.created_at DESC LIMIT 1"""), {"r": str(run_id)}).first()
    if row is not None:
        return str(row[0])
    return same_work_in_flight(connection, run_id)


def same_work_in_flight(connection, run_id: str) -> Optional[str]:
    """The in-flight run start_code_task would dedupe this run's work onto, other than itself."""
    from .code_task_service import in_flight_same_work

    me = connection.execute(sql(
        "SELECT project_id, status, title, created_at, spec->>'work_fingerprint' AS fp, "
        "spec->>'repo' AS repo FROM code_task_runs WHERE id = :r"), {"r": str(run_id)}).mappings().first()
    if me is None:
        return None
    other = in_flight_same_work(connection, me["project_id"], fingerprint=me["fp"],
                                repo=me["repo"], title=me["title"], exclude=str(run_id))
    if not other:
        return None
    if str(me["status"] or "") in IN_FLIGHT:
        newer = connection.execute(sql(
            "SELECT 1 FROM code_task_runs WHERE id = :o AND created_at > :t"),
            {"o": other, "t": me["created_at"]}).first()
        if newer is None:
            return None
    return str(other)
