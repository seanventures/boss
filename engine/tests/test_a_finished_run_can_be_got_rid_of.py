"""There was no way to delete a run, so the console filled up with them.

Cancelling stops the work and leaves the row. A deadlettered run is already terminal, so
cancelling one does nothing at all. Building one workflow left eighteen runs on the screen with
no way to clear any of them: "lots of workflows shows error and waiting.. please clean up the
mess please."

The one thing this must never become is a tidier that stops the bookkeeping while the work
carries on — that is the 2026-09-02 incident, from the other direction. A run still going is
refused, and told to cancel first.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import workflows


class TestItWillNotDeleteWorkThatIsStillGoing:
    """REWRITTEN to the one-decision rule (2026-10-01). These read `delete_run`'s source for its
    own status check. That check was a second copy of the console's "Remove this run" rule, and
    the two had disagreed (Remove was offered on a `blocked` run this refused). Both now read the
    run's "remove" action, so the behaviour is asserted on that decision and on `delete_run`
    asking for it."""

    @staticmethod
    def _remove(status):
        from boss_engine.services import workflow_run_actions

        return workflow_run_actions.decide({"status": status, "input": {}}, [], {}, "remove")

    @pytest.mark.parametrize("status", ["running", "waiting", "queued", "paused", "blocked"])
    def test_a_running_run_is_refused(self, status):
        verdict = self._remove(status)
        assert verdict["state"] == "absent"
        assert "Cancel it first" in verdict["reason"], "a refusal has to say what to do instead"

    def test_delete_run_asks_that_decision_before_it_touches_anything(self):
        source = inspect.getsource(workflows.delete_run)
        assert 'require(connection, project_id, run_id, "remove")' in source
        assert source.index('"remove")') < source.index("SET {column} = NULL")

    def test_cancelled_counts_as_finished(self):
        """Cancel stops the work; a cancelled run is then safe to remove, and is the ordinary
        route for getting rid of one that was still going."""
        assert self._remove("cancelled")["state"] == "enabled"

    def test_deadlettered_is_already_terminal_which_is_why_cancel_did_nothing(self):
        assert "deadlettered" in workflows.TERMINAL_RUN_STATES
        assert "completed" in workflows.TERMINAL_RUN_STATES
        assert "failed" in workflows.TERMINAL_RUN_STATES


class TestItRemovesWhatTheRunOwnsAndNothingElse:
    def test_every_reference_that_does_not_cascade_is_listed(self):
        """The first anyone hears of a missed one is a foreign key violation in front of an
        operator. This is the list, checked against the schema rather than remembered."""
        from pathlib import Path

        schema = Path("db/schema.sql").read_text(encoding="utf-8")
        lines = schema.split("\n")

        uncascaded: set[tuple[str, str]] = set()
        table = ""
        for line in lines:
            if line.startswith("CREATE TABLE"):
                table = line.split()[-2].strip("(") if line.strip().endswith("(") \
                    else line.split()[-1].strip("(")
            if "REFERENCES workflow_runs(id)" in line and "ON DELETE" not in line:
                uncascaded.add((table, line.strip().split()[0]))

        listed = set(workflows.POINTS_AT_A_RUN)
        assert uncascaded <= listed, (
            f"these point at a run and are not handled, so the delete will fail: "
            f"{uncascaded - listed}")

    def test_somebody_elses_row_is_kept_and_its_reference_cleared(self):
        """A panel that mentions this run is not this run's to delete. Tidying up must not
        destroy more than it was asked to."""
        source = inspect.getsource(workflows.delete_run)
        assert "SET {column} = NULL" in source
        assert "DELETE FROM panel_runs" not in source
        assert "DELETE FROM agent_studio_runs" not in source

    def test_only_the_run_row_is_deleted_and_the_rest_cascades(self):
        source = inspect.getsource(workflows.delete_run)
        assert source.count("DELETE FROM") == 1
        assert "DELETE FROM workflow_runs WHERE id = :id" in source

    def test_the_removal_is_recorded_before_the_row_goes(self):
        """After the delete there is nothing left to describe."""
        source = inspect.getsource(workflows.delete_run)
        assert source.index("emit_event") < source.index("DELETE FROM workflow_runs")

    def test_it_belongs_to_the_project_that_asked(self):
        source = inspect.getsource(workflows.delete_run)
        assert 'run["project_id"] != project_id' in source


class TestItWillNotStrandAParentThatIsStillWaiting:
    """A finished child is still the row a waiting subworkflow step resumes from.

    Advance copies the child's output only while `child_workflow_run_id` is set. Deleting the
    child nulls that pointer, the next advance skips the step, and the parent waits forever.
    The console lists the child as its own run, and the button this feature added is exactly
    how an operator clears the "error" row while the parent still says "waiting".
    """

    @pytest.fixture(autouse=True)
    def _remove_is_offered(self, monkeypatch):
        """These fake connections answer only the waiter query. Whether the run's "remove"
        action is offered is decided — and tested — in workflow_run_actions; here it is, so
        what is under test is the waiter guard that runs after it."""
        from boss_engine.services import workflow_run_actions

        asked = []
        monkeypatch.setattr(workflow_run_actions, "require",
                            lambda connection, project_id, run_id, key, **kw:
                            asked.append(key) or {"key": key, "enabled": True})
        return asked

    def _connection(self, waiter):
        class _Rows:
            def __init__(self, row):
                self._row = row

            def mappings(self):
                return self

            def first(self):
                return self._row

        class _Conn:
            def __init__(self):
                self.sql: list[str] = []

            def execute(self, statement, params=None):
                sql = str(statement)
                self.sql.append(sql)
                if "FROM workflow_step_runs" in sql:
                    return _Rows(waiter)
                return _Rows({
                    "id": "wfr_child",
                    "project_id": "proj",
                    "status": "completed",
                    "workflow_definition_id": "wfd_1",
                })

        return _Conn()

    def test_a_live_waiter_is_refused_before_any_pointer_is_cleared(self):
        from boss_engine.services.workflows import WorkflowError, delete_run

        connection = self._connection({
            "run_id": "wfr_parent",
            "run_status": "waiting",
            "step_id": "build_child",
        })
        with pytest.raises(WorkflowError, match="waiting forever"):
            delete_run(connection, "proj", "wfr_child", "operator")
        assert not any("SET " in sql for sql in connection.sql)
        assert not any("DELETE FROM" in sql for sql in connection.sql)

    def test_a_finished_child_whose_parent_already_moved_on_is_removed(self):
        """Once the parent step has succeeded, the child's output lives on that step.
        Nothing is waiting, so the tidy-up the operator asked for still happens."""
        from boss_engine.services.workflows import delete_run

        connection = self._connection(None)
        assert delete_run(connection, "proj", "wfr_child", "operator")["deleted"] is True
        assert any("DELETE FROM workflow_runs" in sql for sql in connection.sql)

    def test_the_guard_runs_before_references_are_cleared(self):
        source = inspect.getsource(workflows.delete_run)
        assert source.index("child_workflow_run_id = :run_id") < source.index(
            "SET {column} = NULL")
        assert "TERMINAL_STEP_STATES" in source
        # REWRITTEN (2026-10-02): the parent's states are CLOSED_RUN_STATES now, not
        # TERMINAL_RUN_STATES. A parent stopped at a failed step is waiting for its operator,
        # not over, and its subworkflow step resumes from this child after a retry or skip.
        assert "CLOSED_RUN_STATES" in source


class TestTheEndpointIsReachable:
    def test_a_run_can_be_deleted_over_http(self):
        from pathlib import Path

        main = Path("apps/api/app/main.py").read_text(encoding="utf-8")
        assert '@app.delete("/v1/delivery-workflows/runs/{run_id}")' in main
        assert "workflow_runtime.delete_run(" in main

    def test_a_refusal_is_a_conflict_not_a_server_error(self):
        from pathlib import Path

        main = Path("apps/api/app/main.py").read_text(encoding="utf-8")
        block = main[main.index('@app.delete("/v1/delivery-workflows/runs/{run_id}")'):]
        block = block[:block.index("\n@app.")]
        assert "status_code=409" in block
        assert "status_code=404" in block
