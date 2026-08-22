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


def test_a_finished_run_is_closed_as_completed_not_cancelled():
    """Eight runs sat at status='running' with phase='done'.

    They had finished every stage and nothing moved the status. Closing those as `cancelled`
    would replace one wrong answer with another and erase work that actually succeeded.
    """
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert 'str(r["phase"] or "") == "done"' in src
    assert "SET status = 'completed'" in src
    assert "SET status = 'cancelled'" in src
    # the phase must be selected, or the distinction cannot be made
    assert "r.phase AS phase" in src


def test_previews_have_their_own_rule_because_they_have_no_job():
    """The same miss in miniature: sweep_abandoned_runs enumerated the tables I was looking at.

    A preview is built and booted INLINE by start_preview — there is no job. So "no live job"
    is trivially true for every preview, and applying the no-job rule would have failed all of
    them. Time is the only signal a preview has, which is why it needs a separate sweeper.
    """
    src = inspect.getsource(shepherd_service.sweep_stale_previews)
    assert "app_previews" in src
    assert "status = 'starting'" in src
    assert "NOT EXISTS" not in src, "previews have no job to be absent"
    assert shepherd_service.PREVIEW_STARTING_STALE_MINUTES >= 15, (
        "a cold npm install plus a build legitimately takes minutes")


def test_the_autopilot_runs_the_preview_sweep_too():
    src = inspect.getsource(shepherd_service.autopilot)
    assert "sweep_stale_previews" in src
    assert '"stale_previews": stale_previews' in src


def test_every_table_that_can_strand_a_run_is_swept_by_something():
    """The whole-picture check: which tables can hold a row that claims to be in progress?

    code_task_runs / meeting_runs / discovery_runs -> sweep_abandoned_runs (no live job)
    app_previews                                   -> sweep_stale_previews (time)
    deployment_runs                                -> fail_hung_deployments, on the
                                                      deployment-health tick

    If a new table joins that list, it needs a rule here or it will strand rows silently.
    """
    from boss_engine.services import delivery_pipeline

    covered = {t for t, _, _ in shepherd_service._RUN_TABLES}
    assert {"code_task_runs", "meeting_runs", "discovery_runs"} <= covered
    assert callable(shepherd_service.sweep_stale_previews)
    assert callable(delivery_pipeline.fail_hung_deployments)


def test_consistency_report_covers_each_of_those_tables():
    """The report must ask about every table that can strand a row, or it reads clean while
    something is stuck — which is exactly how today went."""
    from boss_engine.tools import builtin

    src = inspect.getsource(builtin._register_operations_tools)
    for table in ("code_task_runs", "meeting_runs", "discovery_runs", "app_previews", "jobs",
                  "decisions"):
        assert table in src, f"consistency_report never asks about {table}"


def test_a_blocked_run_whose_job_is_gone_is_abandoned_too():
    """'Blocked' is only a legitimate resting state while a job exists to resume.

    A run blocked on a decision whose job has been cancelled can never proceed — approving the
    decision resumes nothing — but it renders as a live BLOCKED card. One sat beside a running
    build of the same app and made the running one look blocked.
    """
    covered = {t: working for t, _id, working in _RUN_TABLES}
    assert "blocked" in covered["code_task_runs"]


def test_a_blocked_run_that_still_has_a_job_is_left_alone():
    """The no-live-job rule is the whole safety of including 'blocked'."""
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert "NOT EXISTS" in src
    assert "'queued','running','leased'" in src


def test_a_blocked_run_is_closed_as_cancelled_not_completed():
    """Its phase is awaiting_*, never 'done', so the phase rule already handles it —
    but assert it, because closing abandoned work as 'completed' would fake a success."""
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert 'str(r["phase"] or "") == "done"' in src
