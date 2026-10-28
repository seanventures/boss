"""Tests for fix_lane_triage.classify — routes a request to the fix lane or the full
pipeline per the fix-lane spec's acceptance criterion #1.
"""
from __future__ import annotations

import unittest

from boss_engine.services.fix_lane_triage import classify


class DiffEstimate:
    def __init__(self, files: int, lines: int, touched_paths=None):
        self.files = files
        self.lines = lines
        self.touched_paths = touched_paths or []


class TestFixLaneTriage(unittest.TestCase):
    def test_p0_small_bug_with_repro_routes_to_fix_lane(self):
        spec = {
            "kind": "bug",
            "repro": "clicking 'open the project' link drops project_id from the URL",
            "instructions": "fix the deep link so project_id survives navigation",
        }
        verdict = classify(spec, diff_estimate=DiffEstimate(files=1, lines=12))
        self.assertEqual(verdict.lane, "fix")

    def test_p1_feature_request_without_bug_framing_routes_to_full_pipeline(self):
        spec = {
            "kind": "feature",
            "instructions": "add a new export-to-CSV button on the reports page",
        }
        verdict = classify(spec, diff_estimate=DiffEstimate(files=2, lines=40))
        self.assertEqual(verdict.lane, "full")

    def test_p1_request_without_repro_routes_to_full_pipeline(self):
        spec = {"kind": "bug", "instructions": "something feels broken somewhere"}
        verdict = classify(spec, diff_estimate=DiffEstimate(files=1, lines=10))
        self.assertEqual(verdict.lane, "full")

    def test_p1_forced_lane_always_short_circuits(self):
        spec = {
            "kind": "feature",
            "instructions": "add a new export-to-CSV button on the reports page",
        }
        verdict = classify(spec, diff_estimate=DiffEstimate(files=10, lines=900),
                            forced_lane="fix")
        self.assertEqual(verdict.lane, "fix")

    def test_p1_protected_path_touch_routes_to_full_pipeline(self):
        spec = {
            "kind": "bug",
            "repro": "login sometimes hangs after sign-in",
            "instructions": "fix the hang",
        }
        verdict = classify(
            spec,
            diff_estimate=DiffEstimate(
                files=1, lines=20,
                touched_paths=["engine/boss_engine/identity/session.py"],
            ),
        )
        self.assertEqual(verdict.lane, "full")

    def test_p1_missing_diff_estimate_fails_toward_full_pipeline(self):
        spec = {
            "kind": "bug",
            "repro": "the link drops a query param",
            "instructions": "fix the link",
        }
        verdict = classify(spec, diff_estimate=None)
        self.assertEqual(verdict.lane, "full")

    def test_p1_kindless_chat_report_with_failure_phrasing_routes_to_fix_lane(self):
        spec = {
            "message": "the save button stopped working after the last deploy",
        }
        verdict = classify(spec, diff_estimate=DiffEstimate(files=1, lines=5))
        self.assertEqual(verdict.lane, "fix")

    def test_p1_kindless_feature_request_with_evidence_key_routes_to_full_pipeline(self):
        spec = {
            "description": "add a dark mode toggle to settings",
            "evidence": "mockup.png",
        }
        verdict = classify(spec, diff_estimate=DiffEstimate(files=1, lines=5))
        self.assertEqual(verdict.lane, "full")

    def test_p1_oversized_diff_by_lines_alone_routes_to_full_pipeline(self):
        spec = {
            "kind": "bug",
            "repro": "search results are duplicated",
            "instructions": "dedupe the search results",
        }
        verdict = classify(spec, diff_estimate=DiffEstimate(files=1, lines=200))
        self.assertEqual(verdict.lane, "full")

    def test_p1_mapping_diff_estimate_is_read_correctly_not_defaulted_to_fix(self):
        spec = {
            "kind": "bug",
            "repro": "checkout double-charges on retry",
            "instructions": "fix the double charge",
        }
        verdict = classify(
            spec,
            diff_estimate={"files": 10, "lines": 900, "touched_paths": []},
        )
        self.assertEqual(verdict.lane, "full")
        self.assertEqual(verdict.reason, "diff_too_large")


if __name__ == "__main__":
    unittest.main()
