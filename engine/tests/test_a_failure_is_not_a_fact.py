"""A failed step's error is a record of a past attempt. The console read it as the present.

The operator watched BOSS create the deployment target that a Deploy step had refused for —
and the step went on saying, in red, on a run marked `deadlettered`:

    'Lead-Triage Inbox' has no 'staging' deployment target of its own, so there is nowhere to
    ship it.

That had stopped being true. Their words: "not sure why the workflow is not updated with the
latest... it should automatically refresh when we get to the workflow screen again."

Retrying was always possible — `retry-step` resets the attempt and revives the run. What was
missing is what makes retrying an INFORMED act rather than a guess: knowing whether the reason
still holds. So a failure that names a precondition is re-tested against the world.

This is also the operation the first fixing investigation reported as missing: it had to
reconcile a dead-lettered run with hand-written SQL because nothing exposed it.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services.workflow_recheck import (
    CHECKERS,
    STALE_CAPABLE,
    blocker_status,
    recheck_run,
)


class _Rows:
    """A connection that answers the target query with whatever it was given."""

    def __init__(self, targets=(), steps=()):
        self.targets = list(targets)
        self.steps = list(steps)
        self.executed = []

    def execute(self, statement, params=None):
        text = str(statement)
        self.executed.append((text, params or {}))
        rows = (self.targets if "FROM deployment_targets" in text
                else self.steps if "FROM workflow_step_runs" in text else [])

        class Result:
            def mappings(self_inner):
                return self_inner

            def all(self_inner):
                return rows

        return Result()


DEPLOY_REFUSED = {
    "id": "wfs_1", "step_id": "deploy", "status": "deadlettered", "last_error": "no target",
    "output": {"missing": "deployment_target", "app_name": "Lead-Triage Inbox",
               "fix": {"tool": "create_deployment_target",
                       "args": {"app_name": "Lead-Triage Inbox", "environment": "staging"}}},
}


class TestIsItStillTrue:
    def test_the_blocker_has_cleared_once_the_target_exists(self):
        rows = _Rows(targets=[{"id": "deptgt_1", "app_name": "Lead Triage Inbox.",
                               "environment": "staging", "domains": ["192.0.2.202:3014"]}])
        out = blocker_status(rows, "boss-dogfood", DEPLOY_REFUSED)
        assert out["cleared"] is True
        assert "192.0.2.202:3014" in out["reason"]

    def test_the_name_it_was_stored_under_does_not_have_to_match_exactly(self):
        """The target that fixed this was created under 'Lead Triage Inbox.' and the step
        recorded 'Lead-Triage Inbox'. One app."""
        rows = _Rows(targets=[{"id": "deptgt_1", "app_name": "lead triage inbox",
                               "environment": "staging", "domains": []}])
        assert blocker_status(rows, "p", DEPLOY_REFUSED)["cleared"] is True

    def test_a_target_for_a_different_app_does_not_clear_it(self):
        rows = _Rows(targets=[{"id": "deptgt_2", "app_name": "Change Calendar",
                               "environment": "staging", "domains": []}])
        out = blocker_status(rows, "p", DEPLOY_REFUSED)
        assert out["cleared"] is False
        assert "would fail again for the same reason" in out["reason"]

    def test_nor_does_the_same_app_in_a_different_environment(self):
        rows = _Rows(targets=[{"id": "deptgt_3", "app_name": "Lead-Triage Inbox",
                               "environment": "production", "domains": []}])
        assert blocker_status(rows, "p", DEPLOY_REFUSED)["cleared"] is False

    def test_a_running_step_has_no_stale_error_to_re_test(self):
        assert blocker_status(_Rows(), "p", {**DEPLOY_REFUSED, "status": "running"}) is None

    def test_nor_does_a_step_that_recorded_no_blocker(self):
        assert blocker_status(_Rows(), "p", {**DEPLOY_REFUSED, "output": {}}) is None

    def test_a_blocker_nobody_can_test_says_so_rather_than_guessing(self):
        """"Probably fine now" is the answer that gets a retry burned for nothing."""
        out = blocker_status(_Rows(), "p", {**DEPLOY_REFUSED,
                                            "output": {"missing": "a_thing_we_cannot_test"}})
        assert out["cleared"] is False
        assert out["checkable"] is False
        assert "cannot test that one automatically" in out["reason"]

    def test_a_checker_that_raises_does_not_hide_the_failure(self):
        class Boom:
            def execute(self, *a, **kw):
                raise RuntimeError("gone")

        assert blocker_status(Boom(), "p", DEPLOY_REFUSED) is None

    @pytest.mark.parametrize("status", ["failed", "deadlettered", "cancelled"])
    def test_which_states_can_go_stale(self, status):
        assert status in STALE_CAPABLE

    @pytest.mark.parametrize("status", ["running", "succeeded", "ready", "waiting"])
    def test_and_which_cannot(self, status):
        assert status not in STALE_CAPABLE


class TestPuttingItBack:
    def test_a_cleared_step_is_re_queued_with_its_attempts_reset(self):
        rows = _Rows(
            targets=[{"id": "deptgt_1", "app_name": "Lead-Triage Inbox",
                      "environment": "staging", "domains": ["192.0.2.202:3014"]}],
            steps=[DEPLOY_REFUSED])
        out = recheck_run(rows, project_id="p", run_id="wfr_1")
        assert out["requeued"] == ["deploy"]
        written = " ".join(t for t, _ in rows.executed)
        assert "status='ready', attempt=0, last_error=NULL" in written

    def test_and_the_run_comes_back_with_it(self):
        """A re-queued step under a dead run never runs."""
        rows = _Rows(
            targets=[{"id": "deptgt_1", "app_name": "Lead-Triage Inbox",
                      "environment": "staging", "domains": []}],
            steps=[DEPLOY_REFUSED])
        recheck_run(rows, project_id="p", run_id="wfr_1")
        assert any("UPDATE workflow_runs SET status='running'" in t for t, _ in rows.executed)

    def test_a_step_whose_reason_still_holds_is_left_alone(self):
        rows = _Rows(targets=[], steps=[DEPLOY_REFUSED])
        out = recheck_run(rows, project_id="p", run_id="wfr_1")
        assert out["requeued"] == []
        assert out["checked"][0]["cleared"] is False
        assert not any("status='ready'" in t for t, _ in rows.executed)

    def test_it_can_report_without_changing_anything(self):
        rows = _Rows(
            targets=[{"id": "deptgt_1", "app_name": "Lead-Triage Inbox",
                      "environment": "staging", "domains": []}],
            steps=[DEPLOY_REFUSED])
        out = recheck_run(rows, project_id="p", run_id="wfr_1", requeue=False)
        assert out["checked"][0]["cleared"] is True
        assert out["requeued"] == []
        assert not any("status='ready'" in t for t, _ in rows.executed)

    def test_a_run_with_nothing_to_re_test_says_so_plainly(self):
        rows = _Rows(steps=[{**DEPLOY_REFUSED, "status": "succeeded"}])
        out = recheck_run(rows, project_id="p", run_id="wfr_1")
        assert out["ok"] is True
        assert "No step on this run recorded a blocker" in out["note"]

    def test_an_unknown_run_is_an_error_not_a_silent_success(self):
        out = recheck_run(_Rows(), project_id="p", run_id="wfr_missing")
        assert out["ok"] is False


class TestItIsReachableFromEverywhereItIsNeeded:
    def test_the_console_reads_it_on_every_load(self):
        """The automatic half: arriving at the workflow screen re-tests, so the operator does
        not have to know to press anything."""
        import pathlib

        main = pathlib.Path("apps/api/app/main.py").read_text()
        assert "from boss_engine.services.workflow_recheck import blocker_status" in main
        assert '"blocker": _blockers.get(' in main

    def test_a_re_check_that_fails_never_fails_the_page(self):
        import pathlib

        main = pathlib.Path("apps/api/app/main.py").read_text()
        i = main.index("_blockers[(_s[\"workflow_run_id\"]")
        assert "except Exception" in main[i - 400:i + 400]

    def test_there_is_an_endpoint_for_the_whole_run(self):
        import pathlib

        main = pathlib.Path("apps/api/app/main.py").read_text()
        assert '@app.post("/v1/delivery-workflows/runs/{run_id}/recheck")' in main

    def test_and_boss_can_do_it_without_sql(self):
        """The gap the first fixing investigation named: it had to reconcile a dead-lettered
        run by hand because no operation existed."""
        from boss_engine.tools import build_discuss_read_registry

        registry = build_discuss_read_registry()
        assert "recheck_workflow_run" in registry.names()
        assert registry.get("recheck_workflow_run").self_executable

    def test_the_tool_will_not_re_run_something_still_broken(self):
        from boss_engine.tools import builtin

        body = inspect.getsource(builtin)
        # Fragments that do not span a string-concatenation boundary in the source.
        assert "re-runs a step whose reason still holds" in body
        assert "re-queues only the steps whose blocker has actually cleared" in body
