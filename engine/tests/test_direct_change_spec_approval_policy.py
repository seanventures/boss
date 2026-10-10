"""BOSS-482: direct-change eligibility cannot override the operator's PRD gate.

These exercise the real settings reader, gate planner and execution branch. No database,
model or provider effect is needed to decide whether an unanswered gate may run.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.config import get_settings
from boss_engine.services import workflow_capability_runner as runner


DIRECT = {"input": {"adopt_code_task_run_id": "owned_direct_run"}}
STEP = {"id": "review", "binding": {"capability": "spec_review"}}


class NoDatabase:
    def execute(self, *args, **kwargs):
        raise AssertionError("an unadopted direct-change gate should not read the database")


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    monkeypatch.delenv("BOSS_MEETING_PRD_AUTO_APPROVE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def assert_manual_gate():
    gate = runner._gate_subject(NoDatabase(), "owned_project", STEP, DIRECT)
    assert gate["key"] == "spec_review"
    assert gate["completes_step"] is True
    assert gate["risk_tier"] >= 2
    with pytest.raises(runner.CapabilityExecutionError, match="waits for a person"):
        runner._execute_step(NoDatabase(), None, project_id="owned_project", actor="owner",
                             step=STEP, context=DIRECT)


@pytest.mark.parametrize("mode,setting,automatic", [
    ("enforce", None, False),
    ("shadow", None, True),
    ("off", None, True),
    ("enforce", "false", False),
    ("enforce", "true", True),
    ("shadow", "false", False),
])
def test_settings_control_gate_planning_and_execution(monkeypatch, mode, setting, automatic):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", mode)
    if setting is not None:
        monkeypatch.setenv("BOSS_MEETING_PRD_AUTO_APPROVE", setting)
    assert get_settings().meeting_prd_auto_approve is automatic
    if automatic:
        assert runner._gate_subject(NoDatabase(), "owned_project", STEP, DIRECT) is None
        outcome = runner._execute_step(NoDatabase(), None, project_id="owned_project",
                                       actor="owner", step=STEP, context=DIRECT)
        assert outcome["status"] == "not_applicable"
        assert "No PRD to approve" in outcome["note"]
    else:
        assert_manual_gate()


def test_settings_read_error_retains_manual_gate(monkeypatch, caplog):
    def unavailable():
        raise RuntimeError("owned settings reader unavailable")

    monkeypatch.setattr(runner, "get_settings", unavailable, raising=False)
    assert_manual_gate()
    assert "retaining manual spec review" in caplog.text
    assert "owned settings reader unavailable" not in caplog.text


@pytest.mark.parametrize("setting", [None, "false", "true", 1])
def test_missing_or_untyped_setting_cannot_approve(monkeypatch, setting):
    settings = SimpleNamespace() if setting is None else SimpleNamespace(
        meeting_prd_auto_approve=setting)
    monkeypatch.setattr(runner, "get_settings", lambda: settings, raising=False)
    assert_manual_gate()


@pytest.mark.parametrize("key,context", [
    ("approve_release", DIRECT),
    ("spec_review", None),
    ("spec_review", []),
    ("spec_review", {}),
    ("spec_review", {"input": "not a run input"}),
    ("spec_review", {"input": {"adopt_code_task_run_id": ""}}),
    ("spec_review", {"input": {"adopt_code_task_run_id": "owned_direct_run",
                               "adopt_meeting_run_id": "owned_meeting"}}),
    ("spec_review", {"input": {"adopt_code_task_run_id": "owned_direct_run",
                               "adopt_decision_id": "owned_decision"}}),
])
def test_other_gates_and_origins_never_take_direct_change_shortcut(monkeypatch, key, context):
    def must_not_read_settings():
        raise AssertionError("the direct-change setting does not govern this origin")

    monkeypatch.setattr(runner, "get_settings", must_not_read_settings, raising=False)
    assert runner._no_prd_to_approve(key, context) is False
