"""A project whose builds END AT THEIR PR completes its workflow there (2026-10-31).

BOSS building its own repository opened reviewed PRs that were merged, and every run was
shown failed (preview of BOSS: no module) or deadlettered (launch readiness: refused). With
`builds_end_at_pr` on, every step past the PR is not applicable and opens no approval; with it
off, or before anything was built, nothing changes.
"""
from __future__ import annotations

from boss_engine.services import workflow_capability_runner as runner


class _Row:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _Conn:
    """Answers the one project_settings read with the stored value."""

    def __init__(self, value):
        self.value = value

    def execute(self, *_a, **_k):
        return _Row(self.value)


def _step(capability: str) -> dict:
    return {"id": capability, "binding": {"capability": capability}}


BUILT = {"input": {"adopt_code_task_run_id": "crun_1"}, "steps": {}}


def test_every_step_past_the_pr_is_not_applicable_when_the_setting_is_on():
    for key in ("preview", "plrr", "deploy", "monitor", "monitoring_pack"):
        out = runner.ends_at_the_pr(_Conn("1"), None, "p", _step(key), BUILT)
        assert out and out["status"] == "not_applicable" and out["ends_at_pr"], key


def test_a_build_started_by_the_workflow_counts_as_built():
    ctx = {"input": {}, "steps": {"build": {"code_task_run_id": "crun_2"}}}
    assert runner.ends_at_the_pr(_Conn(1), None, "p", _step("deploy"), ctx)


def test_the_build_its_review_pass_2_and_the_report_still_run():
    for key in ("build", "review", "test", "more_tests", "report", "notify", "spec_review", "meeting"):
        assert runner.ends_at_the_pr(_Conn("1"), None, "p", _step(key), BUILT) is None, key


def test_nothing_changes_with_the_setting_off_or_before_a_build():
    assert runner.ends_at_the_pr(_Conn(None), None, "p", _step("deploy"), BUILT) is None
    assert runner.ends_at_the_pr(_Conn("0"), None, "p", _step("deploy"), BUILT) is None
    assert runner.ends_at_the_pr(_Conn("1"), None, "p", _step("deploy"),
                                 {"input": {}, "steps": {}}) is None


def test_the_step_runner_records_it_and_no_deploy_approval_is_opened():
    out = runner._execute_step(_Conn("1"), None, project_id="p", actor="boss",
                               step=_step("plrr"), context=BUILT)
    assert out["status"] == "not_applicable"
    spec = {**_step("deploy"), "approval": {"mode": "manual", "approver_role": "release_manager"}}
    assert runner._gate_subject(_Conn("1"), "p", spec, BUILT) is None
