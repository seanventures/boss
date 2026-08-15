"""Official QA /reports gate is evidence-only and does not invent scores."""
from __future__ import annotations

from scripts.qa.official_reports import (
    ALLOW_OFFICIAL_FLAG,
    _honesty_from_report,
    inspect_reports,
    request_official_runs,
)


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.project_id = "boss-dogfood"
        self.posts = []

    def request(self, method, path, **kwargs):
        self.queries = getattr(self, "queries", [])
        self.queries.append((method, path, kwargs.get("query")))
        if method == "POST":
            self.posts.append((path, kwargs.get("body")))
        payload = self.routes.get((method, path))
        if payload is None:
            return 404, {"detail": "missing"}, 1
        status, body = payload
        return status, body, 1


def test_honesty_flags_invented_null_score():
    honesty = _honesty_from_report(
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


def test_inspect_reports_fails_on_invented_score():
    client = FakeClient(
        {
            ("GET", "/v1/qa/benchmark-apps"): (
                200,
                {
                    "items": [
                        {"id": "bugbox", "built": {"run_id": "run_1"}},
                        {"id": "hookwatch"},
                        {"id": "approveit"},
                    ]
                },
            ),
            ("GET", "/v1/qa/benchmark-apps/runs/run_1/report"): (
                200,
                {
                    "passed": True,
                    "honesty": {
                        "honest": False,
                        "invented_scores": ["ui"],
                        "missing_evidence": [],
                    },
                    "scorecard": {
                        "dimensions": [
                            {"id": "ui", "status": "scored", "score": None}
                        ]
                    },
                },
            ),
        }
    )
    checks = inspect_reports(client)
    honesty = next(c for c in checks if c.name == "official.bugbox.honesty")
    assert honesty.ok is False
    assert honesty.required is True
    assert client.queries[0][2] == {"project_id": "boss-dogfood"}


def test_official_posts_are_blocked_without_flag(monkeypatch):
    monkeypatch.delenv(ALLOW_OFFICIAL_FLAG, raising=False)
    client = FakeClient({})
    checks = request_official_runs(client)
    assert checks[0].ok is True
    assert client.posts == []
