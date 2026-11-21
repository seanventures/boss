"""A parked run whose authority has ended does not repair itself.

The sentence is exact: "the authority this run was started under has ended; renew it
to continue." A recovery build is not started, and the step is not put back for another
automatic attempt. A person renews that authority and the same parked run continues.
"""

from __future__ import annotations

from boss_engine.identity import consent
from boss_engine.services import code_task_service as cts
from boss_engine.services import step_recovery
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

ENDED = "the authority this run was started under has ended; renew it to continue."


def test_an_ended_authority_is_not_retried_as_a_fresh_build():
    failure = {"error": ENDED, "headline": ENDED, "why": ENDED}
    signature, pattern = step_recovery.cause_signature("build", failure)
    assert pattern["pattern"] == "authority_ended"
    choice = step_recovery.coded_decision(
        "build", {"failure": failure}, signature=signature, pattern=pattern, last={},
        allowed=step_recovery.KINDS["build"])
    assert choice["action"] == step_recovery.ASK_PERSON
    assert choice["action"] not in (
        step_recovery.RETRY, step_recovery.FIX_APP, *step_recovery.BOSS_SIDE_ACTIONS)
    assert choice["question"] == ENDED
    assert "repair build could not be started" not in str(choice)


def test_an_authority_the_org_default_renewed_is_simply_run_again():
    """identity/fix_authority (operator 2026-11-21): when the run is live again on its person's
    fixer authority, the work that died of the ended grant is RETRIED — no person asked."""
    failure = {"error": ENDED, "headline": ENDED, "why": ENDED}
    signature, pattern = step_recovery.cause_signature("build", failure)
    choice = step_recovery.coded_decision(
        "build", {"failure": failure, "authority_renewed": True}, signature=signature,
        pattern=pattern, last={}, allowed=step_recovery.KINDS["build"])
    assert choice["action"] == step_recovery.RETRY


def test_a_recovery_build_is_not_started_when_the_authority_has_ended(monkeypatch):
    monkeypatch.setattr(consent, "recorded_authority", lambda *_a, **_k: object())
    monkeypatch.setattr(consent, "acting_for_workflow_run", lambda *_a, **_k: None)

    def _refuse_repair(*_a, **_k):
        raise AssertionError("retry_code_task started a repair")

    monkeypatch.setattr(cts, "retry_code_task", _refuse_repair)
    new_id, why = runner._start_recovery_build(
        object(), object(), project_id="p", run_id="wfr_parked", step_id="build",
        build_run_id="crun_old", note="repair the app")
    assert new_id == ""
    assert why == ENDED


class _Conn:
    def execute(self, *_a, **_k):
        return self


def test_a_person_renews_the_authority_and_the_same_parked_run_continues(monkeypatch):
    repairs: list[str] = []
    continued: list[dict] = []

    def _refuse_repair(*_a, **_k):
        repairs.append("repair")
        return {}

    def _requeue(_connection, *, project_id, step_run_id, output, event, data=None):
        continued.append({"project_id": project_id, "step_run_id": step_run_id,
                          "event": event, "data": data, "output": output})
        return {"id": step_run_id, "status": "ready"}

    monkeypatch.setattr(cts, "retry_code_task", _refuse_repair)
    monkeypatch.setattr(workflows, "requeue_waiting_step", _requeue)
    monkeypatch.setattr(consent, "recorded_authority", lambda *_a, **_k: object())
    monkeypatch.setattr(consent, "acting_for_workflow_run", lambda *_a, **_k: None)
    step = {"id": "wstep_parked", "step_id": "build"}

    stayed = runner.follow_authority_park(
        _Conn(), project_id="p", run_id="wfr_parked", step=step)
    assert stayed["outcome"] == "parked"
    assert stayed["run_id"] == "wfr_parked"
    assert stayed["error"] == ENDED
    assert continued == [] and repairs == []

    monkeypatch.setattr(consent, "acting_for_workflow_run", lambda *_a, **_k: object())
    went = runner.follow_authority_park(
        _Conn(), project_id="p", run_id="wfr_parked", step=step)
    assert went["outcome"] == "continued"
    assert went["run_id"] == "wfr_parked"
    assert repairs == []
    assert continued == [{
        "project_id": "p",
        "step_run_id": "wstep_parked",
        "event": "workflow.authority_renewed",
        "data": {"run_id": "wfr_parked"},
        "output": {"note": ENDED},
    }]


def test_the_ended_authority_is_not_queued_for_another_attempt():
    said, retryable = workflows.retryable_failure(
        f"the repair build could not be started ({ENDED})", True)
    assert said == ENDED
    assert retryable is False
