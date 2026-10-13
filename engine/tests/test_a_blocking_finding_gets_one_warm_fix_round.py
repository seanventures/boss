"""A blocking finding from any seat gets one warm fix round; a blocker still open after it blocks.

Gate B (crun_2d097414, PR paramg/boss-sandbox#227, a templates/{id}/apply build on the `lean`
profile) failed code_review_passed, security_clean, tests_strong_enough and consistent. The
in-run loop ran only when EVERY failing gate was convergeable, and tests_strong_enough was not,
so no seat got a fix turn: the PR says "Round 1 of 1 — first review", and the profile's
advisory_gate shipped a reviewer BLOCKER and a HIGH security finding as advisory.
"""
from __future__ import annotations

import inspect
import itertools

import pytest

from boss_engine.services import governed_code_task_service as g

GATE_B = ["code_review_passed", "security_clean", "tests_strong_enough", "consistent"]
JUDGMENT = sorted(g._JUDGMENT_GATES)

#: The reviewer's words on crun_2d097414 (trimmed).
BLOCKER_REVIEW = (
    "FINDING: BLOCKER onboarding-path-builder/package.json:9 — the two template scripts are "
    "missing from `scripts`.\nFINDING: MAJOR views/dashboard.mjs:23 — no path editor.\n"
    "VERDICT: REFUTED")
MAJOR_REVIEW = "FINDING: MAJOR views/dashboard.mjs:23 — no path editor.\nVERDICT: REFUTED"


def test_gate_bs_findings_now_get_their_round():
    assert g.warm_fix_rounds(GATE_B, configured=2, repair_attempts=1, use_microvm=False) >= 1


# Entry paths differ, to this decision, only by what they carry into it: the profile's Review
# rounds (an experiment arm may set 1), a repair attempt already spent (a retry or a test fix
# round), and whether a failing test rides along. Composer New app, Add a feature,
# templates/{id}/apply and a retry all reach it through _run_governed_code_task.
@pytest.mark.parametrize("gate,configured,repair_attempts,with_failing_tests", list(
    itertools.product(JUDGMENT, (1, 2, 5), (0, 1), (False, True))))
def test_every_judgment_finding_gets_a_round_on_every_path(gate, configured, repair_attempts,
                                                           with_failing_tests):
    failing = [gate] + (["written_tests_pass"] if with_failing_tests else [])
    rounds = g.warm_fix_rounds(failing, configured=configured, repair_attempts=repair_attempts,
                               use_microvm=False)
    assert rounds >= 1
    if configured == 1 or (with_failing_tests and repair_attempts):
        assert rounds == 1      # exactly one


def test_failing_tests_alone_keep_their_one_round_rule():
    assert g.warm_fix_rounds(["written_tests_pass"], configured=2, repair_attempts=0,
                             use_microvm=False) == 1
    assert g.warm_fix_rounds(["written_tests_pass"], configured=2, repair_attempts=1,
                             use_microvm=False) == 0


def test_no_warm_round_where_it_cannot_help():
    # A mechanical gate a warm round does not fix (the template gate, an IaC gate) goes to the
    # legacy paths; the micro-VM substrate has no warm session (cold repair instead, below).
    assert g.warm_fix_rounds(["code_review_passed", "template_tests"], configured=2,
                             repair_attempts=0, use_microvm=False) == 0
    assert g.warm_fix_rounds(["code_review_passed"], configured=2, repair_attempts=0,
                             use_microvm=True) == 0
    assert g.warm_fix_rounds([], configured=2, repair_attempts=0, use_microvm=False) == 0


def test_the_pipeline_decides_the_round_with_this_function_and_a_weak_suite_is_fixed_in_it():
    src = inspect.getsource(g._run_governed_code_task)
    loop = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    assert "inrun_max = warm_fix_rounds(failing, configured=inrun_max," in loop
    # The old guard (every failing gate convergeable AND a warm session) is gone.
    assert "set(failing) <= _convergeable" not in loop
    assert "and impl_session_id" not in loop
    # A weak suite: the test author strengthens it, then the probe re-judges it.
    assert '_route("write_tests")' in loop and "_strengthen_tests_prompt(" in loop
    assert 'gates["tests_strong_enough"] = _caught is not False' in loop
    assert '"tests_strong_enough"}:\n                        break' in loop


@pytest.mark.parametrize("advisory", [True, False])
@pytest.mark.parametrize("never_block", [True, False])
def test_a_blocker_still_open_after_its_round_blocks_on_every_profile(advisory, never_block):
    action = g.resolve_gate_failure_action(
        failing=GATE_B, advisory_gate=advisory, rounds_used=0, max_repair_rounds=2,
        never_block=never_block, blockers=["code_review_passed", "security_clean"],
        fix_round_ran=True)
    assert action == "block"


def test_findings_below_blocker_stay_advisory_after_the_round():
    for failing in (["code_review_passed"], ["tests_strong_enough"], ["consistent"]):
        assert g.resolve_gate_failure_action(
            failing=failing, advisory_gate=True, rounds_used=0, max_repair_rounds=2,
            blockers=[], fix_round_ran=True) == "ship"


def test_a_build_that_could_not_have_a_warm_round_gets_the_cold_one_first():
    kw = dict(failing=["code_review_passed"], advisory_gate=True, max_repair_rounds=2,
              blockers=["code_review_passed"], fix_round_ran=False)
    assert g.resolve_gate_failure_action(rounds_used=0, **kw) == "repair"
    assert g.resolve_gate_failure_action(rounds_used=2, **kw) == "block"


def test_a_harness_fault_is_not_a_blocker():
    assert g.resolve_gate_failure_action(
        failing=["code_review_passed"], advisory_gate=True, rounds_used=0, max_repair_rounds=2,
        faults={"code_review_passed": "HARNESS"}, blockers=["code_review_passed"]) == "inconclusive"


def test_what_counts_as_a_blocker():
    assert g.unresolved_blockers(GATE_B, review_summary=BLOCKER_REVIEW) == ["code_review_passed"]
    assert g.unresolved_blockers(GATE_B, review_summary=MAJOR_REVIEW) == []
    # The security seat says ISSUES only for an exploitable CRITICAL/HIGH finding.
    assert g.unresolved_blockers(GATE_B, security_verdict="issues") == ["security_clean"]
    assert g.unresolved_blockers(GATE_B, consistency_summary="CONSISTENT: NO") == []
    assert g.unresolved_blockers(
        GATE_B, consistency_summary="FINDING: BLOCKER — the login is not wired\nCONSISTENT: NO"
    ) == ["consistent"]
    # A passing gate, or a seat that never saw the change, is not an open blocker.
    assert g.unresolved_blockers(["consistent"], review_summary=BLOCKER_REVIEW) == []
    assert g.unresolved_blockers(GATE_B, review_summary=BLOCKER_REVIEW,
                                 hollow={"review_code": "named no file"}) == []


def test_the_pipeline_judges_blockers_after_the_round_and_says_not_done():
    src = inspect.getsource(g._run_governed_code_task)
    after = src[src.index("rounds_used = max(0"):]
    assert after.count("blockers=_blockers") == 3          # both decisions and the message
    assert after.count("fix_round_ran=_fix_rounds_run > 0") == 2
    error, reason = g.blocked_gate_messages("qg_1", ["code_review_passed"], None,
                                            blockers=["code_review_passed"])
    assert error.startswith("Not done: a BLOCKER finding is still open after its fix round "
                            "(code review)")
    assert "not done" in reason
