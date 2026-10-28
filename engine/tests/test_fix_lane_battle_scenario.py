"""A runnable fix-lane battle scenario: a small, evidenced bug report is triaged,
driven through a simulated red -> green -> scoped-verify -> review -> PR run (two
outcomes: one that stays in budget, one that blows it and escalates), with its
per-step timing recorded throughout.

This does not re-run ``governed_code_task_service`` end to end (that needs a live
sandbox, DB and agents) -- it drives the SAME decision functions
(``fix_lane_triage.classify`` and every ``fix_lane_runner`` helper) the pipeline
calls, in the order the pipeline calls them, over one concrete report. That is what
acceptance criteria #1-#5 ask to be demonstrated: triage, skip-design, a narrowed
review panel, scoped re-verification/escalation, and per-step timing -- wired
together as one scenario rather than as isolated unit facts.
"""
from __future__ import annotations

import unittest

from boss_engine.services import fix_lane_runner, fix_lane_triage

_BUG_SPEC = {
    "kind": "bug",
    "repro": "clicking 'open the project' link drops project_id from the URL",
    "instructions": "fix the deep link so project_id survives navigation",
}

_ALL_PANEL_SEATS = ("review_code", "security_check", "regression", "consistency_check")


class DiffEstimate:
    def __init__(self, files: int, lines: int, touched_paths=None):
        self.files = files
        self.lines = lines
        self.touched_paths = touched_paths or []


def _run_scenario(*, real_files: int, real_lines: int, real_touched: list[str]):
    """Drive one fix-lane report through triage, stage-skipping, the narrowed review
    panel, a red->green test the lane carries, scoped escalation against the real
    diff, and per-step timing -- in the order the pipeline runs them.
    """
    timeline: list[dict] = []

    # 1. Pre-flight triage, from the caller's size ESTIMATE (acceptance criterion #1).
    verdict = fix_lane_triage.classify(
        _BUG_SPEC, diff_estimate=DiffEstimate(files=1, lines=12,
                                              touched_paths=["app/links.py"]))
    lane = verdict.lane
    timeline.append({"stage": "triage", "total_ms": 50})

    # 2. The design chain never runs for the fix lane (acceptance criterion #2).
    design_ran = not fix_lane_runner.skips_design(lane)
    timeline.append({"stage": "design", "total_ms": 0 if not design_ran else 4000})

    # 3. RED: a failing test keyed off the report's repro, authored before any fix.
    red_test = {"test_file": "tests/test_links.py", "name": "test_project_id_survives",
                "status": "red"}
    timeline.append({"stage": "red", "total_ms": 800})

    # 4. GREEN: the agent's change makes the carried red test pass.
    red_test = {**red_test, "status": "green"}
    timeline.append({"stage": "green", "total_ms": 1500})

    # 5. Scoped re-verification against the REAL diff -- this can still escalate
    # (acceptance criterion #3) even though the pre-flight estimate looked small.
    escalation = fix_lane_runner.check_escalation(
        _BUG_SPEC, files=real_files, lines=real_lines, touched_paths=real_touched,
        carried_test=red_test)
    timeline.append({"stage": "verify", "total_ms": 600})
    if escalation.escalated:
        lane = fix_lane_triage.LANE_FULL

    # 6. The review panel: narrowed to one combined seat on the fix lane, the full
    # panel once escalated (acceptance criterion #2 and #3 together).
    seats = fix_lane_runner.review_seats_for(lane, _ALL_PANEL_SEATS)
    review_prompt = fix_lane_runner.combined_review_prompt(
        "judge correctness against the repro", "judge security of the touched paths")
    timeline.append({"stage": "review", "total_ms": 2000})

    # 7. PR: only once review is clean and (if escalated) the carried test rode along.
    pr = {"opened": True, "carried_test": escalation.carried_test if escalation.escalated else None}
    timeline.append({"stage": "pr", "total_ms": 300})

    return {
        "lane": lane,
        "design_ran": design_ran,
        "seats": seats,
        "review_prompt": review_prompt,
        "escalation": escalation,
        "pr": pr,
        "per_step_seconds": fix_lane_runner.per_step_seconds(timeline),
    }


class TestFixLaneBattleScenario(unittest.TestCase):
    def test_a_small_evidenced_bug_runs_the_fix_lane_end_to_end_without_escalating(self):
        result = _run_scenario(real_files=1, real_lines=14, real_touched=["app/links.py"])

        self.assertEqual(result["lane"], fix_lane_triage.LANE_FIX)
        self.assertFalse(result["design_ran"])
        self.assertEqual(result["seats"], frozenset({"review_code"}))
        self.assertIn("judge correctness", result["review_prompt"])
        self.assertIn("judge security", result["review_prompt"])
        self.assertFalse(result["escalation"].escalated)
        self.assertTrue(result["pr"]["opened"])
        self.assertIsNone(result["pr"]["carried_test"])

        steps = result["per_step_seconds"]
        self.assertEqual(steps["design"], 0.0)
        self.assertGreater(steps["red"], 0.0)
        self.assertGreater(steps["green"], 0.0)
        self.assertGreater(steps["verify"], 0.0)
        self.assertGreater(steps["review"], 0.0)
        self.assertGreater(steps["pr"], 0.0)

    def test_a_report_that_blows_its_real_diff_budget_escalates_to_the_full_pipeline(self):
        result = _run_scenario(real_files=6, real_lines=40, real_touched=["app/links.py"])

        self.assertEqual(result["lane"], fix_lane_triage.LANE_FULL)
        self.assertTrue(result["escalation"].escalated)
        self.assertEqual(result["escalation"].reason, "diff_too_large")
        # The full panel is restored once escalated -- no seat narrowing survives.
        self.assertEqual(result["seats"], frozenset(_ALL_PANEL_SEATS))
        # The red->green test the fix lane already wrote carries forward, not lost.
        self.assertEqual(result["pr"]["carried_test"]["name"], "test_project_id_survives")
        self.assertEqual(result["pr"]["carried_test"]["status"], "green")

    def test_a_late_protected_path_touch_also_escalates(self):
        result = _run_scenario(real_files=1, real_lines=10,
                                real_touched=["engine/boss_engine/identity/session.py"])

        self.assertEqual(result["lane"], fix_lane_triage.LANE_FULL)
        self.assertTrue(result["escalation"].escalated)
        self.assertEqual(result["escalation"].reason, "protected_path_touch")


if __name__ == "__main__":
    unittest.main()
