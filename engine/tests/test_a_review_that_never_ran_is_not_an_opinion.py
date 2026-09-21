"""A check that never finished must not be shipped past as if it had a view.

Measured on a 39-file build (crun_cfcccba8c40848a7814d): review_code ran out of turns at the
16-turn cap and judged nothing, yet the run shipped with three gates recorded as advisory,
`adversarial_verdict: "not_run"`, `review_confidence: 0.0`, and no review comment on the pull
request at all.

Two separate faults produced that, and both are fixed here:

  * resolve_gate_failure_action checked HARNESS faults BELOW the advisory branch, which
    returns "ship" for anything outside the hard floor. review_code is outside it, so the
    escalation built for exactly this case — re-run the CHECK with a doubled budget — could
    never fire, because the action was decided before the question was asked.

  * the reviewers' own prose was dropped. security_check and consistency_check each wrote a
    substantial review; none of it was in the graded form parse_findings expects, so the PR
    carried nothing and no event said why.

Advisory means a reviewer's OPINION cannot withhold the PR. It cannot mean the opinion was
never needed.
"""

from __future__ import annotations

from boss_engine.services.governed_code_task_service import (
    classify_gate_faults,
    resolve_gate_failure_action,
)

REVIEW = "code_review_passed"
CONSISTENT = "consistent"
SECURITY = "security_clean"           # the name the pipeline actually records
NO_CODE = "code_written"              # the whole advisory hard floor

# What a panel that hit the turn cap actually stores. The keys are the ones
# classify_gate_faults reads — not the old `no_security_findings` alias.
_PANEL_NEVER_RAN = {
    "review_summary": "(ran out of turns before finishing — partial attempt)",
    "adversarial_verdict": "not_run",
    "security_summary": "(ran out of turns before finishing — partial attempt)",
    "security_verdict": "not_run",
    "consistency_summary": "(ran out of turns before finishing — partial attempt)",
    "consistency_verdict": "not_run",
}


def _action(failing, faults, *, advisory=True, rounds=0, open_pr=True):
    return resolve_gate_failure_action(
        failing=failing, advisory_gate=advisory, rounds_used=rounds,
        max_repair_rounds=2, open_pr=open_pr, never_block=True, faults=faults)


def test_a_reviewer_that_ran_out_of_turns_escalates_instead_of_shipping():
    """THE BUG: this returned "ship" because advisory was checked first."""
    assert _action([REVIEW], {REVIEW: "HARNESS"}) == "inconclusive"


def test_every_reviewer_failing_as_a_harness_fault_escalates():
    failing = [REVIEW, CONSISTENT, "tests_strong_enough"]
    faults = {g: "HARNESS" for g in failing}
    assert _action(failing, faults) == "inconclusive"


def test_a_reviewer_that_DID_judge_is_still_advisory():
    """The autonomy default is unchanged: a real opinion does not withhold the PR."""
    assert _action([REVIEW], {REVIEW: "VERDICT"}) == "ship"


def test_a_mix_of_a_real_verdict_and_a_harness_fault_is_not_escalated():
    """Something was said about the code, so the normal path applies."""
    assert _action([REVIEW, CONSISTENT],
                   {REVIEW: "HARNESS", CONSISTENT: "VERDICT"}) == "ship"


def test_no_code_produced_still_outranks_everything():
    """The hard floor is untouched: nothing to ship is a fact, not a judgement."""
    assert _action([NO_CODE], {NO_CODE: "VERDICT"}) in {"repair", "ship_with_dissent"}


def test_a_harness_fault_on_the_hard_floor_still_escalates():
    """The narrowed-to-hard-floor path keeps its own inconclusive check."""
    assert _action([NO_CODE, REVIEW],
                   {NO_CODE: "HARNESS", REVIEW: "HARNESS"}) == "inconclusive"


def test_passing_gates_ship():
    assert _action([], {}) == "ship"


def test_no_faults_recorded_behaves_as_before():
    """faults is optional; absent means nothing is known to be a harness fault."""
    assert _action([REVIEW], None) == "ship"
    assert _action([REVIEW], {}) == "ship"


def test_a_security_check_that_never_ran_is_a_harness_fault():
    """THE BUG: classify_gate_faults looked for `no_security_findings`, a name the
    pipeline has never recorded. `security_clean` with verdict not_run fell through
    to WORK, so a check that judged nothing was treated as a real finding."""
    faults = classify_gate_faults(
        failing=[SECURITY], evidence=_PANEL_NEVER_RAN)
    assert faults[SECURITY] == "HARNESS"


def test_a_panel_that_never_finished_escalates_including_security():
    """The measured case: review + security + consistency all not_run.

    Advisory still ships a real opinion. It must not ship past a panel that
    never produced one — and security_clean being misclassified as WORK was
    enough to defeat the all-HARNESS guard on its own.
    """
    failing = [REVIEW, SECURITY, CONSISTENT]
    faults = classify_gate_faults(failing=failing, evidence=_PANEL_NEVER_RAN)
    assert all(faults[g] == "HARNESS" for g in failing)
    assert _action(failing, faults) == "inconclusive"
