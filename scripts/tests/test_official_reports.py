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


def test_official_posts_record_run_id_and_pass_engine(monkeypatch):
    monkeypatch.setenv(ALLOW_OFFICIAL_FLAG, "1")
    monkeypatch.setenv("BOSS_QA_ENGINE", "openai")
    client = FakeClient(
        {
            ("POST", "/v1/qa/benchmark-apps/bugbox/runs"): (
                201,
                {"status": "queued", "run_id": "crun_official_1"},
            ),
            ("POST", "/v1/qa/benchmark-apps/hookwatch/runs"): (
                201,
                {"status": "queued", "run_id": "crun_official_2"},
            ),
            ("POST", "/v1/qa/benchmark-apps/approveit/runs"): (
                201,
                {"status": "queued", "run_id": "crun_official_3"},
            ),
        }
    )
    checks = request_official_runs(client)
    assert [c.ok for c in checks] == [True, True, True]
    assert checks[0].evidence["run_id"] == "crun_official_1"
    assert client.posts[0][1]["official"] is True
    assert client.posts[0][1]["engine"] == "openai"


def test_inspect_reports_honest_when_ux_not_scored():
    client = FakeClient(
        {
            ("GET", "/v1/qa/benchmark-apps"): (
                200,
                {
                    "items": [
                        {"id": "bugbox", "built": {"run_id": "run_live"}},
                        {"id": "hookwatch"},
                        {"id": "approveit"},
                    ]
                },
            ),
            ("GET", "/v1/qa/benchmark-apps/runs/run_live/report"): (
                200,
                {
                    "passed": False,
                    "honesty": {
                        "honest": True,
                        "scored": ["ui"],
                        "invented_scores": [],
                        "missing_evidence": [
                            {"id": "ux", "reason": "JTBD passed is unknown"}
                        ],
                    },
                    "scorecard": {
                        "dimensions": [
                            {"id": "ui", "status": "scored", "score": 100},
                            {
                                "id": "ux",
                                "status": "not_scored",
                                "score": None,
                                "reason": "JTBD passed is unknown",
                            },
                        ]
                    },
                },
            ),
        }
    )
    checks = inspect_reports(client)
    honesty = next(c for c in checks if c.name == "official.bugbox.honesty")
    assert honesty.ok is True
    assert honesty.evidence["invented_scores"] == []
    assert honesty.evidence["passed"] is False


def test_inspect_prefers_official_run_id_override(monkeypatch):
    monkeypatch.setenv(
        "BOSS_OFFICIAL_RUN_IDS",
        "bugbox=crun_01c41b278b8d4875a5f8,hookwatch=crun_d15a1f5556e946ab865a",
    )
    client = FakeClient(
        {
            ("GET", "/v1/qa/benchmark-apps"): (
                200,
                {
                    "items": [
                        {
                            "id": "bugbox",
                            "built": {"run_id": "crun_stale_catalog"},
                        },
                        {"id": "hookwatch"},
                        {"id": "approveit"},
                    ]
                },
            ),
            (
                "GET",
                "/v1/qa/benchmark-apps/runs/crun_01c41b278b8d4875a5f8/report",
            ): (
                200,
                {
                    "passed": False,
                    "honesty": {
                        "honest": True,
                        "invented_scores": [],
                        "missing_evidence": [{"id": "ux"}],
                    },
                },
            ),
            (
                "GET",
                "/v1/qa/benchmark-apps/runs/crun_d15a1f5556e946ab865a/report",
            ): (
                200,
                {
                    "passed": False,
                    "honesty": {
                        "honest": True,
                        "invented_scores": [],
                        "missing_evidence": [{"id": "ux"}],
                    },
                },
            ),
        }
    )
    checks = inspect_reports(client)
    names = [c.name for c in checks]
    assert "official.bugbox.honesty" in names
    assert "official.hookwatch.honesty" in names
    paths = [path for _method, path, _q in client.queries]
    assert "/v1/qa/benchmark-apps/runs/crun_01c41b278b8d4875a5f8/report" in paths
    assert "/v1/qa/benchmark-apps/runs/crun_stale_catalog/report" not in paths
