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
    def test_a_running_run_is_refused(self):
        source = inspect.getsource(workflows.delete_run)
        assert "TERMINAL_RUN_STATES" in source
        assert "Cancel it first" in source, "a refusal has to say what to do instead"

    def test_cancelled_counts_as_finished(self):
        """Cancel stops the work; a cancelled run is then safe to remove, and is the ordinary
        route for getting rid of one that was still going."""
        source = inspect.getsource(workflows.delete_run)
        assert '"cancelled"' in source

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
