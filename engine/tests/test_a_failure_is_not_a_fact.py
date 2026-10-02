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
    """REWRITTEN to the one-retry rule. These asserted recheck_run's own copy of the re-queue
    UPDATE and of the run revival. That copy recorded no `workflow.step_retried` event and
    revived the run whatever state it was in; a cleared step is now put back through
    `workflows.retry_failed_step`, the same retry the console and the agent use."""

    @pytest.fixture()
    def retried(self, monkeypatch):
        from boss_engine.services import workflows

        from boss_engine.services import workflow_run_actions

        calls = []
        monkeypatch.setattr(workflows, "retry_failed_step",
                            lambda connection, **kw: calls.append(kw) or {"status": "ready"})
        # The fake connection answers only the step query. Whether the run's "recheck" action
        # is offered is decided and tested in workflow_run_actions; see the tests below for
        # the refusal itself.
        monkeypatch.setattr(workflow_run_actions, "require",
                            lambda connection, project_id, run_id, key, **kw:
                            {"key": key, "enabled": True})
        return calls

    def test_a_cleared_step_is_put_back_through_the_one_retry(self, retried):
        rows = _Rows(
            targets=[{"id": "deptgt_1", "app_name": "Lead-Triage Inbox",
                      "environment": "staging", "domains": ["192.0.2.202:3014"]}],
            steps=[DEPLOY_REFUSED])
        out = recheck_run(rows, project_id="p", run_id="wfr_1", actor="ceo")
        assert out["requeued"] == ["deploy"]
        assert retried == [{"project_id": "p", "run_id": "wfr_1", "step_id": "deploy",
                            "actor": "ceo"}]

    def test_it_keeps_no_copy_of_the_retry(self):
        """No second re-queue UPDATE and no second run revival: one implementation."""
        source = inspect.getsource(recheck_run)
        assert "status='ready'" not in source
        assert "UPDATE workflow_runs" not in source
        assert "retry_failed_step(" in source

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

    def test_a_stopped_run_is_not_put_back_by_a_re_check(self, monkeypatch):
        """A re-check revived steps of a run somebody had STOPPED: the console stopped offering
        Re-check there, but the endpoint and the agent's tool still re-queued. The run's
        "recheck" action is asked before the first step goes back, and its refusal stands."""
        from boss_engine.services import workflow_run_actions, workflows

        asked, retried = [], []
        monkeypatch.setattr(workflows, "retry_failed_step",
                            lambda connection, **kw: retried.append(kw))

        def refuse(connection, project_id, run_id, key, **kw):
            asked.append(key)
            raise workflow_run_actions.RunActionRefused(key, "this run is cancelled")

        monkeypatch.setattr(workflow_run_actions, "require", refuse)
        rows = _Rows(
            targets=[{"id": "deptgt_1", "app_name": "Lead-Triage Inbox",
                      "environment": "staging", "domains": []}],
            steps=[DEPLOY_REFUSED])
        with pytest.raises(workflows.WorkflowConflictError, match="cancelled"):
            recheck_run(rows, project_id="p", run_id="wfr_1")
        assert asked == ["recheck"] and retried == []

    def test_but_answering_whether_it_is_still_true_is_never_refused(self, monkeypatch):
        """`requeue=False` changes nothing, so it asks nobody's permission."""
        from boss_engine.services import workflow_run_actions

        monkeypatch.setattr(workflow_run_actions, "require",
                            lambda *a, **k: pytest.fail("a report-only re-check was gated"))
        rows = _Rows(
            targets=[{"id": "deptgt_1", "app_name": "Lead-Triage Inbox",
                      "environment": "staging", "domains": []}],
            steps=[DEPLOY_REFUSED])
        assert recheck_run(rows, project_id="p", run_id="wfr_1", requeue=False)["ok"] is True


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
