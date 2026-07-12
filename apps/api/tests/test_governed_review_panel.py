"""#9 verifier hardening — refute-panel aggregation + confidence score.

Pure unit tests for `_aggregate_panel`: the function that combines an N-member
adversarial refute panel into a single fail-closed verdict plus a confidence score
(the fraction of independent reviewers that could NOT refute the change).
"""
from apps.api.app.services.governed_code_task_service import _aggregate_panel


def test_single_seat_preserves_legacy_behavior():
    # panel_size 1 must be identical to the pre-#9 single-reviewer path.
    assert _aggregate_panel(["survived"], 0.5) == ("survived", 1.0)
    assert _aggregate_panel(["refuted"], 0.5) == ("refuted", 0.0)


def test_unanimous_survive():
    assert _aggregate_panel(["survived", "survived", "survived"], 0.5) == ("survived", 1.0)


def test_majority_survive_passes():
    verdict, conf = _aggregate_panel(["survived", "survived", "refuted"], 0.5)
    assert verdict == "survived"
    assert conf == 2 / 3


def test_majority_refute_fails_closed():
    verdict, conf = _aggregate_panel(["refuted", "refuted", "survived"], 0.5)
    assert verdict == "refuted"
    assert conf == 1 / 3


def test_tie_passes_at_default_quorum_but_fails_at_higher():
    # quorum is the MIN survived-fraction to pass; a 1-1 tie is exactly 0.5.
    assert _aggregate_panel(["survived", "refuted"], 0.5) == ("survived", 0.5)
    assert _aggregate_panel(["survived", "refuted"], 0.6) == ("refuted", 0.5)


def test_higher_quorum_demands_stronger_agreement():
    # 2/3 = 0.667 clears a 0.5 quorum but not a 0.7 one.
    assert _aggregate_panel(["survived", "survived", "refuted"], 0.7)[0] == "refuted"


def test_not_run_votes_are_ignored():
    # A reviewer that failed to emit a verdict doesn't count as a vote either way.
    verdict, conf = _aggregate_panel(["survived", "not_run", "refuted"], 0.5)
    assert (verdict, conf) == ("survived", 0.5)


def test_all_not_run_is_fail_closed():
    # No cast votes -> cannot certify -> not_run (gate treats != 'survived' as a fail).
    assert _aggregate_panel(["not_run", "not_run"], 0.5) == ("not_run", 0.0)
    assert _aggregate_panel([], 0.5) == ("not_run", 0.0)
