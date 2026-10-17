"""Open review findings go to the step's own agent first, a person only after that.

THE CASE (HelpDesk AI recording run, 2026-10-16). A build parked for a waiver
(awaiting_gate_override): a security finding was still open after the reviewers' fix rounds.
It went straight to a person to waive the gates. The build step's recovery agent
(step_recovery) never saw it, because a build waiting on a person "has not failed".

The class: a parked-for-waiver build is the agent's to answer first. It gets the failing gates
and the reviewers' findings verbatim, starts a repair build (fix_app), and the step follows it.
Only when the same findings come back (or the repair cannot start, or the budget is spent) is a
person asked to waive — and then the step keeps waiting on the build's waiver card, never failed.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.services import step_recovery as sr
from boss_engine.services import workflow_capability_runner as runner

from .test_a_failed_step_stops_the_run import (  # noqa: F401 - fixtures used by name
    _insert_awaited,
    _steps,
    _tick_through_retries,
    _two_step,
    eng,
    project,
)
from .test_workflow_capability_runner import Ctx, RecordingRegistry, RecordingTool

SECURITY = ("## Findings\n- HIGH: /api/tickets returns every customer's tickets without "
            "checking the session (IDOR) in server/routes/tickets.ts:41")
FAILURE = {"build_run_id": "crun_x", "phase": sr.GATE_WAIT_PHASE, "status": "blocked",
           "error": "QualityGate FAIL (qg_1); failing gates: ['security_clean']. "
                    "bind for 0.0.0.0:8000 failed: port is already allocated",
           "failing_gates": ["security_clean"], "findings": "[security]\n" + SECURITY}


def test_open_findings_have_their_own_signature_whatever_the_error_quotes():
    """A finding can quote a port or a secret; it is still the reviewers' finding."""
    signature, pattern = sr.cause_signature(sr.FINDINGS_KIND, FAILURE)
    assert signature == "build:open_findings:security_clean"
    assert pattern == {"pattern": "open_findings", "gates": ["security_clean"]}


def test_the_agent_fixes_first_and_asks_to_waive_only_after_that():
    allowed = sr.KINDS[sr.FINDINGS_KIND]
    signature, pattern = sr.cause_signature(sr.FINDINGS_KIND, FAILURE)
    first = sr.coded_decision(sr.FINDINGS_KIND, {"failure": FAILURE}, signature=signature,
                              pattern=pattern, last={}, allowed=allowed)
    assert first["action"] == sr.FIX_APP
    assert "IDOR" in first["note"] and "security_clean" in first["note"]
    again = sr.coded_decision(sr.FINDINGS_KIND, {"failure": FAILURE}, signature=signature,
                              pattern=pattern, last={"signature": signature,
                                                     "action": sr.FIX_APP},
                              allowed=allowed)
    assert again["action"] == sr.ASK_PERSON
    assert "Waive" in again["question"] and "security_clean" in again["question"]


def test_a_retry_or_a_boss_wait_is_not_an_answer_to_open_findings():
    """Rebuilding the same code reproduces the same findings; a BOSS update closes none."""
    assert set(sr.KINDS[sr.FINDINGS_KIND]) == {sr.FIX_APP, sr.ASK_PERSON}
    _, why = sr.validate({"action": "retry", "evidence": ["security_clean"]},
                         allowed=sr.KINDS[sr.FINDINGS_KIND], evidence={}, corpus="security_clean",
                         last={}, signature="build:open_findings:security_clean")
    assert "not allowed" in why


def test_the_findings_are_read_from_the_run_and_security_comes_first(eng, project):
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        _insert_awaited(c, project, "code_task_runs", run_id, "blocked")
        c.execute(text("UPDATE code_task_runs SET phase = :ph, state = CAST(:s AS JSONB), "
                       "finalize_ctx = CAST(:f AS JSONB) WHERE id = :i"),
                  {"ph": sr.GATE_WAIT_PHASE, "i": run_id,
                   "s": json.dumps({"review_summary": "## Findings\n- naming nit",
                                    "security_summary": SECURITY}),
                   "f": json.dumps({"failing_gates": ["security_clean", "code_review_passed"]})})
        got = sr.open_findings(c, run_id, "an error with no gate list")
        c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": run_id})
    assert got["failing_gates"] == ["security_clean", "code_review_passed"]
    assert got["findings"].index("[security]") < got["findings"].index("[review]")
    assert "IDOR" in got["findings"]


class _Worker(Ctx):
    """A worker that can start builds (the recovery loop acts only with one)."""

    def __init__(self, engine):
        super().__init__(engine)
        self.engine = engine
        self.jobs = object()
        self.settings = None


def _park(c, project_id: str, run_id: str, gates: list[str]) -> None:
    c.execute(text("UPDATE code_task_runs SET status = 'blocked', phase = :ph, error = :e, "
                   "state = CAST(:s AS JSONB), finalize_ctx = COALESCE(finalize_ctx, "
                   "'{}'::jsonb) || CAST(:f AS JSONB), created_at = now() - interval '1 hour' "
                   "WHERE id = :i"),
              {"ph": sr.GATE_WAIT_PHASE, "i": run_id,
               "e": f"QualityGate FAIL (qg_1); failing gates: {gates}.",
               "s": json.dumps({"security_summary": SECURITY}),
               "f": json.dumps({"failing_gates": gates})})


def test_a_build_parked_on_open_findings_is_repaired_by_its_agent_then_waived(
        eng, project, monkeypatch):
    awaited_id = f"crun_{uuid.uuid4().hex[:10]}"
    repair_id = f"crun_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(
        [RecordingTool("create_code_task", {"code_task_run_id": awaited_id})]))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)

    def no_model(*_a, **_k):
        raise RuntimeError("no model connected")  # BOSS's coded triage answers

    monkeypatch.setattr(sr, "routed_adapter", no_model)
    started: list[dict] = []

    def start(connection, ctx, *, project_id, run_id, step_id, build_run_id, note):
        started.append({"from": build_run_id, "note": note})
        _insert_awaited(connection, project_id, "code_task_runs", repair_id, "running")
        connection.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = CAST(:f AS JSONB) WHERE id = :i"),
            {"i": repair_id, "f": json.dumps({"root_run_id": build_run_id,
                                              "parent_run_id": build_run_id, "attempt": 2})})
        return repair_id, ""

    monkeypatch.setattr(runner, "_start_recovery_build", start)
    with eng.begin() as c:
        run = _two_step(c, project, "build")
        _tick_through_retries(eng, c, project, run["id"], times=2)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        _insert_awaited(c, project, "code_task_runs", awaited_id, "blocked")
        _park(c, project, awaited_id, ["security_clean"])

        # 1. The agent answers first: a repair build told the finding, and the step follows it.
        runner.tick(c, _Worker(eng), project_id=project)
        step = _steps(c, run["id"])["s"]
        assert step["status"] == "waiting"
        assert len(started) == 1 and started[0]["from"] == awaited_id
        assert "IDOR" in started[0]["note"] and "security_clean" in started[0]["note"]
        assert (step["output"].get("await") or {}).get("id") == repair_id
        decision = sr.last_decision(step["output"][sr.STATE_KEY])
        assert decision["action"] == sr.FIX_APP

        # 2. The same findings after the fix: NOW a person is asked to waive — and the step
        #    keeps waiting on the build's waiver card instead of failing.
        _park(c, project, repair_id, ["security_clean"])
        runner.tick(c, _Worker(eng), project_id=project)
        step = _steps(c, run["id"])["s"]
        assert step["status"] == "waiting", step
        assert (step["output"].get("await") or {}).get("id") == repair_id
        assert step["output"].get(runner.FINDINGS_ANSWERED_KEY) == repair_id
        assert "Waive" in str(step["output"].get("recovery_question"))
        assert sr.last_decision(step["output"][sr.STATE_KEY])["action"] == sr.ASK_PERSON
        assert len(started) == 1, "no second repair for the same findings"

        # 3. Asked once: later sweeps do not decide again.
        decisions = len(step["output"][sr.STATE_KEY]["decisions"])
        runner.tick(c, _Worker(eng), project_id=project)
        step = _steps(c, run["id"])["s"]
        assert len(step["output"][sr.STATE_KEY]["decisions"]) == decisions
        c.execute(text("DELETE FROM code_task_runs WHERE id = ANY(:i)"),
                  {"i": [awaited_id, repair_id]})


def test_a_plan_to_approve_stays_a_persons(eng, project, monkeypatch):
    """Only open findings are the agent's: a plan approval or a budget is a person's wait."""
    awaited_id = f"crun_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(
        [RecordingTool("create_code_task", {"code_task_run_id": awaited_id})]))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_recover_awaited",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("decided")))
    with eng.begin() as c:
        run = _two_step(c, project, "build")
        _tick_through_retries(eng, c, project, run["id"], times=2)
        _insert_awaited(c, project, "code_task_runs", awaited_id, "blocked")
        c.execute(text("UPDATE code_task_runs SET phase = 'awaiting_plan_approval' "
                       "WHERE id = :i"), {"i": awaited_id})
        runner.tick(c, _Worker(eng), project_id=project)
        step = _steps(c, run["id"])["s"]
        assert step["status"] == "waiting"
        assert sr.STATE_KEY not in step["output"]
        c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": awaited_id})
