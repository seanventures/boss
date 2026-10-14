"""A template build whose app does not start gets a warm fix round, not a cold child run.

Pre-mortem finding 3, verified by calling the functions directly: `template_tests` was not
convergeable, and it fails AS A CONSEQUENCE whenever the app does not start or the build's own
tests fail (templates/gate.py sets `because`). So warm_fix_rounds returned 0 for every such
template build, and a blocker finding then returned "repair" before the template repair budget
was looked at: a cold ~20-minute child run even at template_repair_max=0.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import governed_code_task_service as g


def _ev(because):
    return {"template_tests": {"error": "x", "because": because}}


@pytest.mark.parametrize("failing,because,repairs", [
    (["app_serves", "template_tests"], "app_serves", 0),
    (["app_serves", "template_tests"], "app_serves", 1),
    (["written_tests_pass", "template_tests"], "tests", 0),
    (["code_review_passed", "app_serves", "template_tests"], "app_serves", 1),
])
def test_a_consequence_template_failure_does_not_cost_the_warm_round(failing, because, repairs):
    assert g.warm_fix_rounds(failing, configured=2, repair_attempts=repairs, use_microvm=False,
                             template_evidence=_ev(because)) >= 1
    # Without the `because` it is the template's own failure: the legacy paths judge it.
    assert g.warm_fix_rounds(failing, configured=2, repair_attempts=repairs,
                             use_microvm=False) == 0


def test_a_template_failure_whose_cause_passed_is_its_own():
    # `because: app_serves` while app_serves passes: stale or not a consequence, never excused.
    assert g.unconvergeable_failing(["template_tests"], template_evidence=_ev("app_serves")) == {
        "template_tests"}
    assert g.warm_fix_rounds(["template_tests", "code_review_passed"], configured=2,
                             repair_attempts=0, use_microvm=False,
                             template_evidence=_ev("app_serves")) == 0


def test_a_secret_the_coder_wrote_goes_to_the_warm_round():
    assert g.warm_fix_rounds(["no_committed_secrets"], configured=2, repair_attempts=0,
                             use_microvm=False, coder_owned_secrets=True) >= 1
    assert g.warm_fix_rounds(["no_committed_secrets"], configured=2, repair_attempts=0,
                             use_microvm=False, coder_owned_secrets=False) == 0


@pytest.mark.parametrize("budget,expected", [(0, "block"), (1, "repair")])
def test_a_blocker_on_a_template_build_respects_its_repair_budget(budget, expected):
    action = g.resolve_gate_failure_action(
        failing=["code_review_passed", "template_tests"], advisory_gate=True, rounds_used=0,
        max_repair_rounds=2, template_repair_max=budget, blockers=["code_review_passed"],
        fix_round_ran=False)
    assert action == expected


def test_a_blocker_without_template_gates_keeps_its_cold_repair():
    assert g.resolve_gate_failure_action(
        failing=["code_review_passed"], advisory_gate=True, rounds_used=0, max_repair_rounds=2,
        template_repair_max=0, blockers=["code_review_passed"], fix_round_ran=False) == "repair"


def test_the_warm_round_feeds_the_secret_and_re_judges_the_template_gates():
    src = inspect.getsource(g._run_governed_code_task)
    loop = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    assert "template_evidence=_tpl_evidence()," in loop
    assert "coder_owned_secrets=_coder_owned_secrets())" in loop
    assert '"COMMITTED SECRET LITERALS' in loop
    # Re-judged on the code as it now stands, with the app-start proof of this round.
    rejudge = loop[loop.index("THE TEMPLATE GATES ARE RE-JUDGED"):]
    assert "_template_record = _template_gates(" in rejudge[:1200]
    assert "_tpl_gates = dict(gates)" in rejudge[:1600]
    # The loop's own exit test uses the same rule as its entry.
    assert "set(failing) - _convergeable" not in loop
    assert "unconvergeable_failing(" in loop
