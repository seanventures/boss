"""An agent step that did nothing fails, and no gate opens over what it did not produce.

THE RUN (BOSSAPP-1365, wfr_eb7b35a1, an API-onboarding workflow). Validate's agent could not run
the gateway's linter, made 0 tool calls and produced no report, no diff and no files — and
declared "completed". The step went green and the run opened the fix-PR approval with input {}:
a person was asked to approve a pull request made of nothing.

Two rules, one each side of the hand-off:
  * `agent_steps.verdict`: a step that promised outputs, made no tool call and produced none of
    them failed, whatever the agent said;
  * `unfed_required_inputs`: a required input wired to an earlier step that produced nothing
    for it is never put in front of a person and never run.
"""
from __future__ import annotations

import pytest

from boss_engine.services import agent_steps
from boss_engine.services import workflow_capability_runner as runner

PROMISED = ["valid", "report", "suggested_fix"]


def _validate_output(**over):
    """What the agent step recorded on the real run, shape for shape."""
    out = {"agent": "backend_engineer", "skills_offered": ["repo.read"], "skills_used": [],
           "status": "completed", "tool_calls": 0,
           "summary": "I could not run gwlint here: no executor is registered for the sandbox "
                      "validation skill.\n{\"outcome\": \"completed\", \"reason\": \"done\"}",
           "valid": "", "report": "", "suggested_fix": ""}
    out["agent_outcome"] = agent_steps.declared_outcome(out["summary"])
    out.update(over)
    return out


def test_the_replayed_step_fails_though_its_agent_said_completed():
    judged = agent_steps.verdict(_validate_output(), step_name="Validate", promised=PROMISED)
    assert judged["outcome"] == agent_steps.FAILED
    assert "did nothing" in judged["error"] and "report" in judged["error"]


def test_work_that_happened_still_passes():
    # A tool call made, or one promised output filled: the agent worked, and may legitimately
    # leave a value it could not find empty (it is told to).
    assert agent_steps.verdict(_validate_output(tool_calls=3), promised=PROMISED)[
        "outcome"] == agent_steps.COMPLETED
    assert agent_steps.verdict(_validate_output(report="3 rules failed"), promised=PROMISED)[
        "outcome"] == agent_steps.COMPLETED
    # A step that promised nothing (it answers from what it was given) is not judged by this.
    assert agent_steps.verdict(_validate_output(), promised=[])["outcome"] == agent_steps.COMPLETED


def test_evidence_without_a_tool_count_is_not_read_as_zero():
    """A step resumed after an approval carries no count; it is judged on its products only."""
    out = _validate_output()
    out.pop("tool_calls")
    assert agent_steps.verdict(out, promised=PROMISED)["outcome"] == agent_steps.COMPLETED


FIX_PR = {"id": "fix_pr", "title": "Open a fix PR",
          "binding": {"capability": "fix_pr", "inputs": {
              "title": {"from_step": "validate", "field": "suggested_title"},
              "report": {"from_step": "validate", "field": "report"}}}}


def _context(validate: dict) -> dict:
    return {"input": {"config_path": "examples/lumen-pay.bad.yaml"},
            "steps": {"validate": validate}, "skipped": {}}


def test_a_gate_is_not_opened_on_an_empty_required_input():
    unfed = runner.unfed_required_inputs(None, "p", FIX_PR, _context({"report": ""}))
    # `title` is required by fix_pr; `report` is optional and may be empty.
    assert unfed == ["title (from validate.suggested_title)"]
    assert runner.unfed_required_inputs(
        None, "p", FIX_PR, _context({"suggested_title": "Fix lumen-pay rate limits"})) == []


def test_the_run_refuses_the_same_step_before_any_work(monkeypatch):
    called = []
    monkeypatch.setattr(runner, "_execute_step", lambda *a, **k: called.append(1) or {})
    monkeypatch.setattr(runner.step_agents, "with_run_agent", lambda c, p, step, i: step)
    with pytest.raises(runner.SettledCapabilityError, match="title .from validate"):
        runner.execute_step(None, object(), project_id="p", actor="workflow", step=FIX_PR,
                            context=_context({}))
    assert not called


def test_an_input_the_run_overrides_or_a_skipped_source_is_not_this_checks():
    step = {"id": "fix_pr", "binding": {"capability": "fix_pr", "inputs": {
        "title": {"from_step": "validate", "field": "t", "override_from_input": "title"}}}}
    ctx = {**_context({}), "input": {"title": "Given at start"}}
    assert runner.unfed_required_inputs(None, "p", step, ctx) == []
    skipped = {**_context({}), "skipped": {"validate": {"reason": "operator"}}}
    assert runner.unfed_required_inputs(None, "p", FIX_PR, skipped) == []


def test_an_empty_list_is_an_answer_not_a_missing_input():
    """`findings: []` is "nothing found", which a later step may need to be told."""
    step = {"id": "file", "binding": {"capability": "fix_pr", "inputs": {
        "title": {"from_step": "validate", "field": "findings"}}}}
    assert runner.unfed_required_inputs(None, "p", step, _context({"findings": []})) == []
    assert runner.unfed_required_inputs(None, "p", step, _context({"findings": {}})) == []
    assert runner.unfed_required_inputs(None, "p", step, _context({"findings": None})) == [
        "title (from validate.findings)"]
