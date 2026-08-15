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
