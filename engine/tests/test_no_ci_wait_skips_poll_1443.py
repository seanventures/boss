"""BOSSAPP-1443: GitHubActionsPipeline.wait_for_verdict skips the 120s CI wait when a repo has no .github/workflows."""
from __future__ import annotations

import httpx

from boss_engine.config import Settings
from boss_engine.services import decisions
from boss_engine.services.github_adapter import GitHubAdapter
from boss_engine.services.pipeline_adapter import GitHubActionsPipeline
from boss_engine.services.software_factory import SoftwareFactoryService


def _settings(**overrides) -> Settings:
    defaults = dict(
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_TOKEN="test-token",
        BOSS_SANDBOX_CI_TIMEOUT_SECONDS=120,
        BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS=1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _http_error(status_code: int, url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", url)
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)


def test_wait_for_verdict_returns_no_ci_immediately_without_sleeping(monkeypatch):
    github = GitHubAdapter(_settings())
    sleeps: list[float] = []
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.sleep", lambda s: sleeps.append(s))

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        if "/contents/.github/workflows" in path:
            raise _http_error(404, f"https://api.github.com{path}")
        raise AssertionError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    pipeline = GitHubActionsPipeline(_settings(), github=github)
    verdict = pipeline.wait_for_verdict("abc123", timeout_seconds=120, poll_interval_seconds=10)

    assert verdict.status == "no_ci"
    assert verdict.completed is True
    assert verdict.checks == []
    assert sleeps == []


def test_wait_for_verdict_polls_when_pr_adds_workflow_file(monkeypatch):
    github = GitHubAdapter(_settings())
    sleeps: list[float] = []
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.sleep", lambda s: sleeps.append(s))
    poll_count = 0

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        nonlocal poll_count
        if path.endswith("/check-runs"):
            poll_count += 1
            if poll_count == 1:
                return {"check_runs": []}
            return {
                "check_runs": [
                    {"name": "ci", "status": "completed", "conclusion": "success"}
                ]
            }
        if "/contents/.github/workflows" in path:
            assert "ref=prhead123" in path
            return [{"name": "ci.yml"}]
        raise AssertionError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    pipeline = GitHubActionsPipeline(_settings(), github=github)
    verdict = pipeline.wait_for_verdict("prhead123", timeout_seconds=120, poll_interval_seconds=1)

    assert verdict.status == "passed"
    assert verdict.completed is True
    assert len(sleeps) == 1


def test_wait_for_verdict_fails_open_to_polling_on_workflows_api_error(monkeypatch):
    github = GitHubAdapter(_settings())
    sleeps: list[float] = []
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.sleep", lambda s: sleeps.append(s))
    ticks = iter([0.0, 0.5, 2.0])
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.monotonic", lambda: next(ticks))

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        if "/contents/.github/workflows" in path:
            raise _http_error(500, f"https://api.github.com{path}")
        raise AssertionError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    pipeline = GitHubActionsPipeline(_settings(), github=github)
    verdict = pipeline.wait_for_verdict("abc123", timeout_seconds=1, poll_interval_seconds=1)

    assert verdict.status == "timed_out"
    assert verdict.completed is False
    assert len(sleeps) == 1


def test_software_factory_and_decisions_handle_no_ci_status(monkeypatch):
    sf = object.__new__(SoftwareFactoryService)
    go = sf._build_release_go_no_go(  # noqa: SLF001
        validation_status="no_ci",
        validation_summary="No CI is configured.",
        security_dependency_review={"status": "passed"},
        browser_validation={"status": "not_required"},
        evidence_refs=[],
    )
    assert go["status"] == "go"
    assert "ci_or_sandbox_validation_not_passed" not in go["blockers"]
    assert sf._review_confidence_for_status("no_ci") == "ci-none-manual-review-required"  # noqa: SLF001
    assert "No hosted CI" in sf._review_recommendation_for_status("no_ci")  # noqa: SLF001
    assert any("No hosted CI" in line for line in sf._validation_follow_up_for_status("no_ci"))  # noqa: SLF001

    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _Res:
                def mappings(self):
                    return self

                def first(self):
                    return {"content": {"status": "no_ci", "summary": "No CI configured"}}

            return _Res()

    service = object.__new__(decisions.DecisionService)
    service._boss_own_verdict = lambda *_args: {
        "passed": ["written_tests_pass", "existing_tests_intact", "nothing_broke"],
        "failed": [],
        "run_id": "crun_noci",
    }
    monkeypatch.setattr(decisions, "emit_event", lambda *_args: None)
    row = {"id": "dec_noci", "project_id": "p", "recommendation": {"test_report_artifact_id": "art_1"}}
    assert service._pull_request_ci_gate(_Conn(), row) is None  # noqa: SLF001

    service._boss_own_verdict = lambda *_args: {
        "passed": ["existing_tests_intact", "nothing_broke"],
        "failed": ["written_tests_pass"],
        "run_id": "crun_noci_fail",
    }
    blocked = service._pull_request_ci_gate(_Conn(), row)  # noqa: SLF001
    assert blocked is not None and blocked["status"] == "blocked_boss_validation_failed"
