"""Severity-calibrated review gate + test arbiter (autonomy calibration).

The gate used to be a single bit: any adversarial finding, however cosmetic, failed
`code_review_passed` and sank the whole run. And a failing test was unfalsifiable ground truth,
so a mis-encoded test burned every repair round. These tests pin the new behaviour AND the
fail-closed defaults that keep the moat intact.
"""
from boss_engine.services.governed_code_task_service import (
    _parse_arbiter,
    _parse_findings,
    _review_gate_from_findings,
)


class TestParseFindings:
    def test_parses_graded_findings(self):
        out = _parse_findings(
            "FINDING: BLOCKER — saves overwrite each other\n"
            "FINDING: NIT - button padding is 7px\n"
            "VERDICT: REFUTED\n")
        assert [f["severity"] for f in out] == ["blocker", "nit"]
        assert "overwrite" in out[0]["text"]

    def test_tolerates_list_markers_and_case(self):
        assert len(_parse_findings("  - finding: major — off-by-one\n* FINDING: Minor: typo")) == 2

    def test_no_structured_findings_is_empty(self):
        assert _parse_findings("Looks fine to me.\nVERDICT: SURVIVED") == []


class TestReviewGate:
    def test_minor_findings_do_not_block(self):
        passed, blocking, deferred = _review_gate_from_findings(
            "FINDING: MINOR — win banner uses CSS that fights [hidden]\nVERDICT: REFUTED",
            "refuted")
        assert passed is True
        assert blocking == []
        assert len(deferred) == 1

    def test_major_finding_blocks(self):
        passed, blocking, deferred = _review_gate_from_findings(
            "FINDING: MAJOR — scoring ignores the difficulty multiplier\nVERDICT: REFUTED",
            "refuted")
        assert passed is False
        assert len(blocking) == 1 and deferred == []

    def test_blocker_beats_a_survived_verdict(self):
        """Graded findings are authoritative: a reviewer cannot grade BLOCKER then wave it through."""
        passed, blocking, _ = _review_gate_from_findings(
            "FINDING: BLOCKER — deletes the user's saved games\nVERDICT: SURVIVED", "survived")
        assert passed is False and len(blocking) == 1

    def test_falls_back_to_verdict_without_structured_findings(self):
        """A model that ignores the FINDING contract keeps the old fail-closed behaviour."""
        assert _review_gate_from_findings("It is broken somehow.", "refuted")[0] is False
        assert _review_gate_from_findings("Looks correct.", "survived")[0] is True

    def test_threshold_is_configurable(self):
        text = "FINDING: MINOR — cosmetic\nVERDICT: REFUTED"
        assert _review_gate_from_findings(text, "refuted", "minor")[0] is False
        assert _review_gate_from_findings(text, "refuted", "major")[0] is True

    def test_unknown_threshold_defaults_to_major(self):
        assert _review_gate_from_findings(
            "FINDING: MINOR — cosmetic\nVERDICT: REFUTED", "refuted", "banana")[0] is True


class TestArbiter:
    def test_reads_both_rulings(self):
        assert _parse_arbiter("ARBITER: TEST_WRONG test_scoring.py::test_exact_copy")[0] == "test_wrong"
        assert _parse_arbiter("ARBITER: CODE_WRONG — the criterion does require this")[0] == "code_wrong"

    def test_captures_the_target_test(self):
        ruling, target = _parse_arbiter("ARBITER: TEST_WRONG test_scoring.py::test_banner_text")
        assert ruling == "test_wrong" and "test_banner_text" in target

    def test_unparseable_never_amends_the_oracle(self):
        """Fail closed: only an explicit TEST_WRONG may unlock rewriting a test."""
        for junk in ["", "I am not sure.", "ARBITER: MAYBE", None]:
            assert _parse_arbiter(junk)[0] == "code_wrong"
