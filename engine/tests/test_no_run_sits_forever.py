"""A run that cannot move must not claim to be working.

One symptom, three separate appearances in a single afternoon:

  * a preview button stuck on "Starting preview…" through five failures
  * a convene button stuck on "Convening…" after the request died
  * a meeting card reading "PANEL SITTING" two hours after its job was gone

Each was fixed in the UI where it surfaced. That is treating the symptom: the UI was
faithfully rendering a row that said `running`, and the row was wrong. A run's execution IS
its job — when the job is cancelled, deadlettered, or lost with the process, nothing remains
that can advance the run.

The platform-layer fix is to stop producing the state, not to add a branch for it in each
component that renders it. No UI needs a case for a status the data no longer emits.
"""

import inspect

from boss_engine.services import shepherd_service
from boss_engine.services.shepherd_service import _RUN_TABLES


def test_every_run_table_that_is_driven_by_a_job_is_covered():
    """Fixing code_task_runs and not meeting_runs is exactly what produced the third case."""
    covered = {t for t, _, _ in _RUN_TABLES}
    assert {"code_task_runs", "meeting_runs", "discovery_runs"} <= covered


def test_the_sweeper_is_generic_not_written_for_code_tasks():
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert "_RUN_TABLES" in src
    body = src.split('"""')[2]
    assert "code_task_runs" not in body, (
        "naming one table in the body is how the next table gets missed")


def test_a_run_is_only_abandoned_when_no_job_could_advance_it():
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert "NOT EXISTS" in src
    assert "'queued','running','leased'" in src, (
        "a job in any live state means the run is genuinely in progress")


def test_there_is_a_grace_period_so_a_new_run_is_never_mistaken_for_an_orphan():
    """A run is created moments before its job; without a grace window this would kill it."""
    assert shepherd_service.ORPHAN_AFTER_MINUTES >= 5
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert "ORPHAN_AFTER_MINUTES" in src
    assert "updated_at <" in src


def test_it_never_reopens_a_finished_run():
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    for _table, _id, working in _RUN_TABLES:
        assert "completed" not in working
        assert "failed" not in working
        assert "cancelled" not in working
    assert "status = ANY(:working)" in src


def test_one_missing_table_does_not_stop_the_others():
    """Editions differ; a table absent in one must not block the sweep everywhere else."""
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert "except Exception" in src
    assert "must not stop the rest" in src


def test_the_autopilot_runs_it_so_nothing_depends_on_someone_noticing():
    src = inspect.getsource(shepherd_service.autopilot)
    assert "sweep_abandoned_runs" in src
    assert '"abandoned": abandoned' in src


def test_cancelling_a_job_closes_both_kinds_of_run():
    """The narrower half of the same rule, applied at the moment of cancellation."""
    from boss_engine.services import jobs
    src = inspect.getsource(jobs.JobLeaseService.cancel_queued_job)
    assert "UPDATE code_task_runs" in src
    assert "UPDATE meeting_runs" in src
