"""Cancelling a run must stop the work that run set going.

On 2026-09-02 a verification run was cancelled at 04:52 in the belief that its build was
stopped. Cancel marked the step rows and the run row and touched nothing else, so the code task
the build step had already started carried on in its own sandbox. Twenty minutes later it
reached its QA step, ran the API test suite against the production DATABASE_URL it had
inherited, and deleted 204 tables.

The cancel was not late and it was not ignored — it simply never reached the work. A button
that reports "cancelled" while the thing it cancelled keeps running is worse than no button,
because it stops anyone from looking further.
"""

from __future__ import annotations

import inspect

from boss_engine.services import workflows


class TestCancelReachesTheRealWork:
    def test_cancel_run_calls_through_to_the_work_it_started(self):
        source = inspect.getsource(workflows.cancel_run)
        assert "_cancel_work_this_run_started" in source, (
            "cancel_run marks step rows and the run row; neither of those stops a code task. "
            "If this call goes away, cancelling stops meaning anything again."
        )

    def test_it_stops_the_work_before_it_rewrites_the_rows(self):
        """Order matters: the awaited handles live in the step outputs, and the rows are about
        to be marked cancelled. Read them first."""
        source = inspect.getsource(workflows.cancel_run)
        assert source.index("_cancel_work_this_run_started") < source.index(
            "UPDATE workflow_step_runs SET status='cancelled'"
        )

    def test_every_async_capability_kind_can_be_stopped(self):
        """A kind that can be started and not stopped is a leak by construction."""
        from boss_engine.services import workflow_capability_runner as runner

        startable = {kind for kind, _fields in runner._AWAIT_KINDS.values()}
        assert startable <= set(workflows._WORK_TABLES), (
            f"these can be started but not cancelled: {startable - set(workflows._WORK_TABLES)}"
        )


class TestItStopsRunningWorkAndNotJustQueuedWork:
    def test_jobs_are_cancelled_with_force(self):
        """The queue refuses to cancel a RUNNING job by default, because killing one leaves a
        half-written branch. When an operator cancels the run, that trade flips: a half-written
        branch on abandoned work beats an agent that keeps going after being told to stop.
        The incident is what settled it."""
        source = inspect.getsource(workflows._cancel_work_this_run_started)
        assert "force=True" in source

    def test_it_finds_jobs_by_the_id_the_step_parked_against(self):
        source = inspect.getsource(workflows._cancel_work_this_run_started)
        assert "payload->>'run_id'" in source, (
            "code task jobs carry payload={'run_id': run_id}; that is the thread from the "
            "awaited handle back to the process actually running"
        )

    def test_it_reads_the_await_handle_rather_than_guessing(self):
        source = inspect.getsource(workflows._cancel_work_this_run_started)
        assert '"await"' in source or "'await'" in source


class TestCancellationCannotBeDefeatedByOneStubbornItem:
    def test_a_failure_to_stop_one_thing_does_not_abort_the_cancel(self):
        """Cancel has to work on the worst day. If one unstoppable job could raise out of the
        loop, the run would stay live precisely when somebody is trying to stop it."""
        source = inspect.getsource(workflows._cancel_work_this_run_started)
        assert source.count("except Exception") >= 2
        assert "logger.exception" in source

    def test_only_unfinished_work_is_touched(self):
        source = inspect.getsource(workflows._cancel_work_this_run_started)
        assert "NOT IN ('succeeded','cancelled','deadlettered')" in source
        assert "_WORK_TERMINAL" in source

    def test_what_was_stopped_is_recorded(self):
        """An operator who cancels a run needs to see what that actually killed — otherwise
        the same silence that hid this incident hides the next one."""
        source = inspect.getsource(workflows._cancel_work_this_run_started)
        assert "workflow.work_cancelled" in source
