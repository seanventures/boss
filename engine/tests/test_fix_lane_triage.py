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


if __name__ == "__main__":
    unittest.main()
