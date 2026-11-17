"""BOSSAPP-1346 (AR-32): a gate whose check did not run is SKIPPED with its reason.

The lean profile does not convene the security and consistency seats. The gate record said
`security_clean` and `consistent` PASSED while the run's retrospective called the same seats a
blocker. One function (`seat_policy.skip_reason`) decides; the gate record, the PR and the
retrospective read what it decided. The acceptance check (BOSSAPP-1211) and the code reviewer
are never skippable.
"""
from __future__ import annotations

import json

from boss_engine.services import governed_code_task_service as svc
from boss_engine.services import pr_description, run_retrospective, seat_policy
from boss_engine.services.pipeline_profiles import resolve_profile

LEAN = resolve_profile("lean")
REASON = "not convened: profile 'lean' does not include this seat"
STATE = {"tests_passed": True, "adversarial_verdict": "survived", "security_verdict": "not_run",
         "consistency_verdict": "not_run", "security_check_skipped": REASON,
         "consistency_check_skipped": REASON,
         "gates_skipped": {"security_clean": REASON, "consistent": REASON}}


def test_a_profile_may_skip_security_and_consistency_but_never_review_or_acceptance():
    seats = {"review_code", "regression"}
    assert seat_policy.skip_reason("security_check", seats, LEAN) == REASON
    assert seat_policy.skip_reason("consistency_check", seats, LEAN) == REASON
    assert seat_policy.skip_reason("security_check", seats | {"security_check"}, LEAN) is None
    assert seat_policy.skip_reason("review_code", set(), LEAN) is None
    assert seat_policy.skip_reason("acceptance_met", set(), LEAN) is None
    assert "acceptance_met" not in seat_policy.skipped_gates(
        {"acceptance_met_skipped": "x", "gates_skipped": {}})


def test_the_gate_record_keeps_a_skipped_gate_out_of_passed(monkeypatch):
    calls = []

    class Conn:
        def execute(self, statement, params=None):
            calls.append(params)

    monkeypatch.setattr(svc, "emit_event", lambda *a, **k: calls.append(("event", a, k)))
    gates = {"code_review_passed": True, "security_clean": True, "consistent": True,
             "written_tests_pass": True}
    _, status = svc._record_quality_gate(Conn(), run_id="r", project_id="p", task_id="t",
                                         job_id="j", evidence=dict(STATE), gates=gates)
    row = next(c for c in calls if isinstance(c, dict))
    assert json.loads(row["gp"]) == ["code_review_passed", "written_tests_pass"]
    assert json.loads(row["gf"]) == []
    assert json.loads(row["ev"])["gates_skipped"] == STATE["gates_skipped"]
    assert status == "pass"            # a skipped seat is not a failure either
    event = next(c for c in calls if isinstance(c, tuple))
    assert event[2] or event[1][-1]["gates_skipped"] == STATE["gates_skipped"]


def test_a_gate_that_did_run_stays_passed(monkeypatch):
    calls = []

    class Conn:
        def execute(self, statement, params=None):
            calls.append(params)

    monkeypatch.setattr(svc, "emit_event", lambda *a, **k: None)
    svc._record_quality_gate(Conn(), run_id="r", project_id="p", task_id="t", job_id="j",
                             evidence={"security_verdict": "clean"},
                             gates={"security_clean": True, "consistent": False})
    row = calls[0]
    assert json.loads(row["gp"]) == ["security_clean"]
    assert json.loads(row["gf"]) == ["consistent"]


def test_the_retrospective_does_not_call_a_skipped_seat_a_blocker():
    got = run_retrospective.score_delivery(STATE, pr_number=7, review_comments_posted=True)
    assert got["verdicts"]["security"] == "skipped"
    assert not any("never produced a verdict" in p for p in got["problems"])
    assert got["grade"] == "good"


def test_a_seat_that_should_have_run_and_did_not_is_still_a_blocker():
    state = {**STATE, "security_check_skipped": "", "gates_skipped": {}}
    got = run_retrospective.score_delivery(state, pr_number=7, review_comments_posted=True)
    assert any("never produced a verdict: " in p and "security" in p for p in got["problems"])


def test_the_pr_lists_a_skipped_gate_with_why_not_as_passed():
    gates = {"code_review_passed": True, "security_clean": True, "consistent": True}
    body = pr_description.render(title="t", evidence=dict(STATE), gates=gates)
    assert "1 passed, 0 failed, 2 skipped." in body
    assert f"{pr_description._gate('security_clean')} — skipped: {REASON}" in body
    assert "✅ " + pr_description._gate("security_clean") not in body


def test_the_pipeline_decides_skips_through_seat_policy_only():
    import inspect

    source = inspect.getsource(svc)
    assert "_seat_policy.skip_reason(" in source
    assert "does not include this seat" not in source
