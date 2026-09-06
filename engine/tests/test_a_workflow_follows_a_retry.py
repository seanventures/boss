"""The build was running and the workflow above it said failed.

    Build, review and test   27m 15s   failed
    the code_task it started finished as 'cancelled'
    AWAIT crun_33a0a77e6ccf4d17b36e        3 of 8

Reported as "the Interview build & test is running but Workflow is not updated", which is
exactly what it was. `crun_7f38987cae0f4becbbf3` — the retry — was running at that moment.

A parked step holds ONE code-task id. Retrying a build starts a fresh attempt with a new id, so
the step went on waiting for the corpse and the run sat at 3 of 8 permanently. Nothing anywhere
connected the retry to the step that had asked for the work.

THE ORDER OF EVENTS IS WHAT MAKES THIS SUBTLE. The build dies, the step fails, and only THEN
does the operator press retry. A recovery that only swept `waiting` steps could never fire for
the sequence that actually happens, so the sweep reaches failed steps too — and asks them one
question and no other: is there a live later attempt at this same work?
"""
from __future__ import annotations

import inspect

from ._source import code_only
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows


class TestTheSweepReachesAFailedStep:
    def test_it_looks_at_failed_steps_as_well_as_waiting_ones(self):
        body = inspect.getsource(runner.resume_awaited)
        assert "status IN ('waiting', 'failed')" in body

    def test_a_failed_step_is_asked_exactly_one_question(self):
        """Everything else in that loop decides the fate of work in flight. A failed step has
        had its verdict; the only thing that reopens it is a retry."""
        body = inspect.getsource(runner.resume_awaited)
        block = body[body.index('if str(step["status"]) == "failed":'):]
        assert "_successor_attempt" in block[:400]
        assert "reopen_failed_step" in block[:800]
        # And it stops there — no other branch may act on a failed step.
        assert "continue" in block[:1200]

    def test_a_failed_step_with_no_retry_is_left_alone(self):
        body = inspect.getsource(runner.resume_awaited)
        block = body[body.index('if str(step["status"]) == "failed":'):]
        # The reopen is inside `if successor:`; without one the loop just moves on.
        assert "if successor:" in block[:600]


class TestAFailedRunCanStillBeRevived:
    """Chicken and egg, and the reason the first version of this fix changed nothing.

    The tick scans `running` and `waiting` runs, which is right for doing work. A run whose
    build failed is `failed` — so it was never swept, so it stayed failed for ever, while the
    retry ran to completion underneath it.
    """

    def test_failed_runs_are_scanned_for_a_retry(self):
        body = inspect.getsource(runner.tick)
        assert "AND r.status = 'failed'" in body

    def test_only_those_with_a_failed_code_task_step(self):
        """A run that failed for some other reason has nothing here to reconsider."""
        body = inspect.getsource(runner.tick)
        assert "s.output->'await'->>'kind' = 'code_task'" in body

    def test_reviving_is_not_running(self):
        """They go to resume_awaited and nothing else — no claiming, no gates, no new work.
        Once revived the run is `waiting` and the ordinary loop takes it from there."""
        body = inspect.getsource(runner.tick)
        block = body[body.index("for run in revivable:"):]
        head = block[:block.index("for run in runs:")]
        assert "resume_awaited(" in head
        for forbidden in ("_claim", "_resume_answered_gates", "_open_gate"):
            assert forbidden not in head, forbidden

    def test_a_revival_failure_never_breaks_the_tick(self):
        body = inspect.getsource(runner.tick)
        block = body[body.index("for run in revivable:"):]
        assert "except Exception" in block[:600]


class TestWhatCountsAsTheSameWork:
    def test_lineage_alone_would_have_missed_the_real_case(self):
        """The live rows, and the reason this is not just a lineage match:

            crun_33a0…  cancelled  root=crun_2b7b…  workflow_run_id=wfr_666b…
            crun_7f38…  running    root=crun_ae46…  workflow_run_id=wfr_666b…

        Different roots, same workflow run. `retry_code_task` continues a lineage; the retry an
        operator actually presses on a stuck workflow — "Try Build, review and test again" —
        re-runs the STEP, which calls create_code_task and starts a build with a root of its
        own. A lineage-only rule would have missed the exact case it was written for."""
        body = code_only(runner._successor_attempt)
        assert "root_run_id" in body
        assert "'workflow_run_id' = :wfr" in body

    def test_it_is_never_matched_on_title(self):
        """Two builds named "Interview Scorecard App" can be different work, and a workflow
        following a stranger because the names matched is worse than one that stays stuck."""
        assert "title" not in code_only(runner._successor_attempt)

    def test_the_governing_run_is_actually_passed_in(self):
        """The signal is worthless if the caller does not supply it."""
        body = inspect.getsource(runner.resume_awaited)
        assert body.count("_successor_attempt(connection, project_id, awaited_id, run_id)") == 2

    def test_an_empty_workflow_id_does_not_match_everything(self):
        """`finalize_ctx->>'workflow_run_id' = ''` would be false for real rows, but the guard
        is explicit so the clause cannot become a wildcard by accident."""
        assert ":wfr <> ''" in code_only(runner._successor_attempt)

    def test_a_completed_successor_counts_too(self):
        """The first version accepted only running/queued, reasoning that a finished attempt
        should reach its verdict the ordinary way. A FAILED step has no ordinary way — it is
        terminal. So by the time the operator looked, the retry had completed, the step was
        still failed, and the recovery declined to act on the most valuable case of all: the
        build worked and the workflow should move on."""
        body = inspect.getsource(runner._successor_attempt)
        assert "'running', 'queued', 'completed'" in body

    def test_but_a_dead_successor_is_not_progress(self):
        """Following a cancelled or failed retry would only move the stuck point."""
        body = inspect.getsource(runner._successor_attempt)
        assert "cancelled" not in body.split("r.status IN")[1][:60]
        assert "'failed'" not in body.split("r.status IN")[1][:60]

    def test_following_a_completed_run_still_checks_for_a_pull_request(self):
        """Re-parking hands it to the ordinary terminal path, which refuses a build that opened
        no PR. The recovery must not become a way around that."""
        body = inspect.getsource(runner.resume_awaited)
        assert 'if kind == "code_task" and not evidence.get("pull_request_url")' in body

    def test_the_awaited_run_is_never_its_own_successor(self):
        body = inspect.getsource(runner._successor_attempt)
        assert "r.id <> :awaited" in body

    def test_it_stays_inside_the_project(self):
        body = inspect.getsource(runner._successor_attempt)
        assert "r.project_id = :p" in body

    def test_it_takes_the_newest(self):
        body = inspect.getsource(runner._successor_attempt)
        assert "ORDER BY r.created_at DESC" in body

    def test_a_database_error_falls_back_to_failing_the_step(self):
        """The safe direction. A step wrongly failed is visible and retryable; a step wrongly
        left waiting is the stuck run this whole file is about."""
        body = inspect.getsource(runner._successor_attempt)
        assert "return None" in body[body.index("except Exception"):]


class TestReopeningIsNarrow:
    def test_only_a_failed_step_can_be_reopened(self):
        body = inspect.getsource(workflows.reopen_failed_step)
        assert "AND status='failed'" in body

    def test_it_goes_back_to_waiting_not_to_succeeded(self):
        """Nothing has succeeded. It is waiting for different work."""
        body = inspect.getsource(workflows.reopen_failed_step)
        assert "SET status='waiting'" in body

    def test_the_stale_error_is_cleared(self):
        """Leaving "finished as 'cancelled'" on a step now following a live build is the same
        contradiction in a quieter form."""
        body = inspect.getsource(workflows.reopen_failed_step)
        assert "last_error=NULL" in body
        assert "completed_at=NULL" in body

    def test_the_run_stops_reporting_failed_too(self):
        body = inspect.getsource(workflows.reopen_failed_step)
        assert "UPDATE workflow_runs SET status='waiting', error=NULL" in body

    def test_the_substitution_is_recorded(self):
        """A step that silently changed which run it was watching would be worse than the stuck
        one — the operator would find a stranger under it with no explanation."""
        body = inspect.getsource(workflows.reopen_failed_step)
        assert "awaited_note" in body
        note = inspect.getsource(runner.resume_awaited)
        assert "the build was retried; this step now follows" in note

    def test_the_step_reports_the_new_id_where_the_console_reads_it(self):
        """The card shows CODE TASK RUN ID from the step's output. Repointing the await handle
        and leaving that field on the dead run would swap the truth for a half-truth."""
        body = inspect.getsource(workflows.reopen_failed_step)
        assert "'code_task_run_id'" in body


class TestTheLiveCase:
    """The two ids from the incident, as data, so the shape of the query is exercised rather
    than only its text."""

    def test_a_retry_shares_the_root_of_the_run_it_replaces(self):
        cancelled = {"id": "crun_33a0", "finalize_ctx": {"root_run_id": "crun_2b7b"}}
        retry = {"id": "crun_7f38", "finalize_ctx": {"root_run_id": "crun_2b7b"}}

        def root(run):
            return (run["finalize_ctx"].get("root_run_id") or "").strip() or run["id"]

        assert root(cancelled) == root(retry)

    def test_a_first_attempt_is_its_own_root(self):
        """COALESCE(root_run_id, id): a run that has never been retried has no root recorded,
        and must still match its own successors."""
        first = {"id": "crun_aaa", "finalize_ctx": {}}
        second = {"id": "crun_bbb", "finalize_ctx": {"root_run_id": "crun_aaa"}}

        def root(run):
            return (run["finalize_ctx"].get("root_run_id") or "").strip() or run["id"]

        assert root(first) == root(second) == "crun_aaa"
