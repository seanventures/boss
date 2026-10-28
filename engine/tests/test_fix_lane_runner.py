"""Tests for fix_lane_runner — the runnable half of the fix lane: which stages it
skips, what its review panel looks like, and when it must escalate to the full
pipeline. Covers the fix-lane spec's follow-up acceptance criterion #5:

  (a) a fix-lane run skips the full-pipeline (design-chain) stages;
  (b) an over-limit change escalates and carries its test forward;
  (c) a feature request routes to the normal workflow (and so never narrows the
      review panel or skips design).
"""
from __future__ import annotations

import unittest

from boss_engine.services import fix_lane_runner
from boss_engine.services.fix_lane_triage import LANE_FIX, LANE_FULL

_ALL_PANEL_SEATS = ("review_code", "security_check", "regression", "consistency_check")


class TestFixLaneSkipsDesign(unittest.TestCase):
    def test_a_fix_lane_skips_the_design_chain(self):
        self.assertTrue(fix_lane_runner.skips_design(LANE_FIX))

    def test_a_full_lane_runs_the_design_chain(self):
        self.assertFalse(fix_lane_runner.skips_design(LANE_FULL))

    def test_a_fix_lane_narrows_the_review_panel_to_one_combined_seat(self):
        seats = fix_lane_runner.review_seats_for(LANE_FIX, _ALL_PANEL_SEATS)
        self.assertEqual(seats, fix_lane_runner.FIX_LANE_REVIEW_SEATS)
        self.assertEqual(seats, frozenset({"review_code"}))

    def test_c_full_lane_keeps_the_whole_review_panel(self):
        seats = fix_lane_runner.review_seats_for(LANE_FULL, _ALL_PANEL_SEATS)
        self.assertEqual(seats, frozenset(_ALL_PANEL_SEATS))

    def test_combined_review_prompt_carries_both_concerns(self):
        prompt = fix_lane_runner.combined_review_prompt("judge correctness", "judge security")
        self.assertIn("judge correctness", prompt)
        self.assertIn("judge security", prompt)


class TestFixLaneEscalation(unittest.TestCase):
    _BUG_SPEC = {
        "kind": "bug",
        "repro": "clicking 'open the project' link drops project_id from the URL",
        "instructions": "fix the deep link so project_id survives navigation",
    }

    def test_b_over_limit_change_escalates_and_carries_its_test(self):
        carried = {"test_file": "tests/test_deep_link.py", "name": "test_project_id_survives"}
        verdict = fix_lane_runner.check_escalation(
            self._BUG_SPEC, files=5, lines=30, touched_paths=["a.py"],
            carried_test=carried,
        )
        self.assertTrue(verdict.escalated)
        self.assertEqual(verdict.reason, "diff_too_large")
        self.assertEqual(verdict.carried_test, carried)

    def test_b_over_limit_by_lines_alone_also_escalates(self):
        verdict = fix_lane_runner.check_escalation(
            self._BUG_SPEC, files=1, lines=500, touched_paths=[],
        )
        self.assertTrue(verdict.escalated)

    def test_b_protected_path_touch_escalates(self):
        verdict = fix_lane_runner.check_escalation(
            self._BUG_SPEC, files=1, lines=10,
            touched_paths=["engine/boss_engine/identity/session.py"],
        )
        self.assertTrue(verdict.escalated)
        self.assertEqual(verdict.reason, "protected_path_touch")

    def test_b_within_limits_never_escalates(self):
        verdict = fix_lane_runner.check_escalation(
            self._BUG_SPEC, files=1, lines=12, touched_paths=["a.py"],
        )
        self.assertFalse(verdict.escalated)
        self.assertIsNone(verdict.reason)
        self.assertIsNone(verdict.carried_test)

    def test_c_feature_request_is_not_a_fix_lane_escalation_concern(self):
        # A feature request never enters the fix lane in the first place (fix_lane_triage
        # routes it to "full" before any diff exists) -- the escalation check below only
        # ever runs for requests the fix lane accepted, but confirms a feature-shaped spec
        # still reads as "full" sized against the same caps, for completeness.
        from boss_engine.services.fix_lane_triage import classify

        spec = {"kind": "feature", "instructions": "add a new export-to-CSV button"}
        verdict = classify(spec, diff_estimate={"files": 1, "lines": 10, "touched_paths": []})
        self.assertEqual(verdict.lane, LANE_FULL)


class TestPerStepSeconds(unittest.TestCase):
    def test_sums_matching_stage_entries_to_seconds(self):
        timings = [
            {"stage": "red", "total_ms": 1500},
            {"stage": "green", "total_ms": 2500},
            {"stage": "green", "total_ms": 500},
        ]
        seconds = fix_lane_runner.per_step_seconds(timings)
        self.assertEqual(seconds["red"], 1.5)
        self.assertEqual(seconds["green"], 3.0)

    def test_falls_back_to_phase_and_duration_ms_keys(self):
        timings = [{"phase": "review_panel", "duration_ms": 4000}]
        seconds = fix_lane_runner.per_step_seconds(timings)
        self.assertEqual(seconds["review_panel"], 4.0)

    def test_empty_input_is_empty_output(self):
        self.assertEqual(fix_lane_runner.per_step_seconds([]), {})


if __name__ == "__main__":
    unittest.main()
