"""What a build records when its status turns terminal — whichever path wrote that status.

A build's profile (`code_task.build_profile`, services/build_profile.py) and its final run
scorecard (services/scorecards.py) used to be recorded only from the build runner's `finally`
(agent_acts.settle_code_task). The run's status is written well before that: the PR path marks
the run `completed` and then starts its auto-preview and spawns follow-ups, and the `finally`
only runs once those return. On crun_16c5e245 (rc10.8) the status was `completed` at 19:31:42
while the build job was still running minutes later, so the release gate read a finished run
with no profile and failed "build profile recorded". Any slow step after the terminal write —
or any path that writes the status outside a build runner (shepherd, cancel, retry delivery) —
had the same hole.

So the record hangs on the TRANSITION, not on the runner:

  * `run_fencing.guarded_status` — the state machine every governed terminal write goes
    through — calls `on_status_written` in the transaction that writes the status;
  * every raw `UPDATE code_task_runs SET status=...` calls `on_status_written` right after it
    (test_a_terminal_build_is_profiled_whichever_path_ends_it holds every write site to that);
  * `core.emit_event` calls `on_event` for every `code_task.*` event on a run, the net under a
    writer that records its outcome as an event;
  * `agent_acts.settle_code_task` still runs both from the `finally`, as the backstop.

Each records at most once per terminal status (the profile's own dedupe: same status, same
transcript → nothing), inside a savepoint, and never raises: a diagnostic that can fail or undo
the status it describes is worse than none.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Events this hook writes itself, or that cannot change a run's status: never re-entered.
_SKIP_EVENTS = frozenset({"code_task.build_profile", "code_task.swallowed_error"})


def _is_postgres(connection: Any) -> bool:
    """A Connection carries its dialect; a Session reaches it through its bind."""
    dialect = getattr(connection, "dialect", None) or getattr(
        getattr(connection, "bind", None), "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def on_status_written(connection: Any, run_id: str) -> None:
    """A run's status was just written in `connection`'s transaction. If it is terminal and not
    yet recorded at that status: its run scorecard and its build profile, now. Never raises."""
    if not run_id or not _is_postgres(connection):
        return
    try:
        from sqlalchemy import text

        from .build_profile import TERMINAL_STATUSES, emit_build_profile_in, profiled_at

        with connection.begin_nested():
            status = connection.execute(text(
                "SELECT status FROM code_task_runs WHERE id = :r"), {"r": run_id}).scalar()
            if status not in TERMINAL_STATUSES or profiled_at(connection, run_id) == status:
                return  # the common case: still running, or already recorded at this status
        try:
            from .scorecards import record_scorecard

            with connection.begin_nested():
                record_scorecard(connection, "build", run_id)
        except Exception:  # noqa: BLE001 — a scorecard never undoes what it records
            log.warning("run terminal: scorecard not recorded for %s", run_id, exc_info=True)
        emit_build_profile_in(connection, run_id)
    except Exception:  # noqa: BLE001
        log.warning("run terminal: could not settle %s", run_id, exc_info=True)


def on_event(connection: Any, event_type: str, entity_type: str, entity_id: str) -> None:
    """core.emit_event's hook: an event on a build may follow a status write made by any path."""
    if entity_type != "code_task_run" or event_type in _SKIP_EVENTS:
        return
    on_status_written(connection, entity_id)
