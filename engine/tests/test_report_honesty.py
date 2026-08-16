"""Official QA /reports stay evidence-only: missing UI/UX is not_scored."""
from __future__ import annotations

from boss_engine.services.benchmark_apps import completion_report, report_honesty


def test_missing_ui_ux_stays_not_scored_and_honest():
    report = completion_report(
        {
            "id": "run_1",
            "status": "completed",
            "spec": {},
            "finalize_ctx": {
                "qa_benchmark": {
                    "id": "bugbox",
                    "name": "BugBox",
                    "verification_commands": ["npm run verify"],
                    "completion_contract": [],
                }
            },
            "state": {
                "tests": [
                    {"command": "npm run verify", "exit_code": 0, "timed_out": False}
                ],
                "design": {"plan": "ship the canary"},
            },
        }
    )
    honesty = report["honesty"]
    assert honesty["honest"] is True
    missing = {item["id"] for item in honesty["missing_evidence"]}
    assert "ui" in missing
    assert "ux" in missing
    dims = {item["id"]: item for item in report["scorecard"]["dimensions"]}
    assert dims["ui"]["status"] == "not_scored"
    assert dims["ui"]["score"] is None
    assert dims["ux"]["status"] == "not_scored"
    assert dims["ux"]["score"] is None
    assert report["ui"]["status"] == "not_scored"
    assert report["ui"]["score"] is None
    assert report["ux"]["status"] == "not_scored"
    assert report["ux"]["score"] is None


def test_falsy_a11y_does_not_become_a_perfect_score():
    from boss_engine.services.benchmark_apps import honest_ui_evidence, _score_ui

    sanitized = honest_ui_evidence({"a11y_violations": False, "contrast_ok": ""})
    assert "a11y_violations" not in sanitized
    assert "contrast_ok" not in sanitized
    assert _score_ui({"a11y_violations": False}) is None
    measured_zero = honest_ui_evidence({"a11y_violations": 0})
    assert measured_zero["a11y_violations"] == 0


def test_invented_null_score_is_flagged_dishonest():
    honesty = report_honesty(
        {
            "scorecard": {
                "dimensions": [
                    {"id": "ui", "status": "scored", "score": None},
                    {"id": "ux", "status": "not_scored", "reason": "no walkthrough"},
                ]
            }
        }
    )
    assert honesty["honest"] is False
    assert "ui" in honesty["invented_scores"]
    assert honesty["missing_evidence"][0]["id"] == "ux"


def test_workspace_scan_scores_ui_without_inventing_ux_or_a11y_zero():
    """Live official runs attach scan markers. JTBD passed=null stays not_scored."""
    report = completion_report(
        {
            "id": "crun_01c41b278b8d4875a5f8",
            "status": "blocked",
            "spec": {},
            "finalize_ctx": {
                "qa_benchmark": {
                    "id": "bugbox",
                    "name": "BugBox",
                    "verification_commands": ["npm run verify"],
                    "completion_contract": [],
                }
            },
            "state": {
                "tests": [],
                "design": {"plan": "ship the canary"},
                "ui_evidence": {
                    "a11y_markers": 132,
                    "states_covered": ["empty", "error", "loading", "ok"],
                    "has_a11y_script": False,
                },
                "ux_evidence": {
                    "jtbd": [
                        {
                            "id": "activity log",
                            "passed": None,
                            "source": "bugbox/tests/api/activity.test.ts",
                        }
                    ]
                },
            },
        }
    )
    honesty = report["honesty"]
    assert honesty["honest"] is True
    assert honesty["invented_scores"] == []
    assert "ui" in honesty["scored"]
    assert "ux" not in honesty["scored"]
    missing = {item["id"] for item in honesty["missing_evidence"]}
    assert "ux" in missing
    dims = {item["id"]: item for item in report["scorecard"]["dimensions"]}
    assert dims["ui"]["status"] == "scored"
    assert isinstance(dims["ui"]["score"], (int, float))
    assert dims["ux"]["status"] == "not_scored"
    assert dims["ux"]["score"] is None
    assert report["ui"]["status"] == "scored"
    assert isinstance(report["ui"]["score"], (int, float))
    assert report["ux"]["status"] == "not_scored"
    assert report["ux"]["score"] is None


def test_factory_recorded_verify_is_not_treated_as_missing():
    """Live official runs write test_command/test_exit_code, not state.tests[]."""
    report = completion_report(
        {
            "id": "crun_01c41b278b8d4875a5f8",
            "status": "completed",
            "spec": {},
            "finalize_ctx": {
                "qa_benchmark": {
                    "id": "bugbox",
                    "name": "BugBox",
                    "verification_commands": ["npm run verify"],
                    "completion_contract": [],
                }
            },
            "state": {
                "design": {"plan": "ship the canary"},
                "test_command": "npm run verify",
                "test_exit_code": 0,
                "tests_passed": True,
                "impl_files": ["bugbox/src/index.ts"],
                "test_files": ["bugbox/tests/api/issues.test.ts"],
                "ui_evidence": {
                    "states_covered": ["empty", "error", "loading", "ok"],
                },
                "last_verify_output": "  ✓ creates and round-trips an issue (12ms)\n",
                "ux_evidence": {
                    "jtbd": [
                        {
                            "id": "creates and round-trips an issue",
                            "passed": None,
                            "source": "bugbox/tests/api/issues.test.ts",
                        }
                    ]
                },
            },
        }
    )
    assert report["results"][1]["status"] == "pass"
    assert report["results"][1]["exit_code"] == 0
    assert report["passed"] is True
    dims = {item["id"]: item for item in report["scorecard"]["dimensions"]}
    assert dims["functionality"]["status"] == "scored"
    assert dims["functionality"]["score"] == 100
    assert dims["reliability"]["status"] == "scored"
    assert dims["ux"]["status"] == "scored"
    assert dims["effort"]["status"] == "scored"
    assert report["honesty"]["honest"] is True
    assert report["honesty"]["invented_scores"] == []


def test_factory_recorded_verify_fail_does_not_promote_jtbd():
    report = completion_report(
        {
            "id": "crun_d15a1f5556e946ab865a",
            "status": "completed",
            "spec": {},
            "finalize_ctx": {
                "qa_benchmark": {"id": "hookwatch", "name": "HookWatch"}
            },
            "state": {
                "design": {"plan": "build it"},
                "test_command": "npm run verify",
                "test_exit_code": 1,
                "tests_passed": False,
                "ux_evidence": {
                    "jtbd": [
                        {"id": "creates an inbox", "passed": None, "source": "t.ts"}
                    ]
                },
            },
        }
    )
    assert report["results"][1]["status"] == "fail"
    assert report["passed"] is False
    dims = {item["id"]: item for item in report["scorecard"]["dimensions"]}
    assert dims["functionality"]["status"] == "scored"
    assert dims["functionality"]["score"] == 0
    assert dims["ux"]["status"] == "not_scored"
    assert report["honesty"]["invented_scores"] == []
