"""Unit tests for GitHubActionsPipeline fast `no_ci` exit, github_fake workflows, and
software_factory `no_ci` handling (BOSSAPP-1448)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from boss_engine.config import Settings
from boss_engine.services.github_adapter import GitHubAdapter
from boss_engine.services.pipeline_adapter import GitHubActionsPipeline
from boss_engine.services.software_factory import SoftwareFactoryService


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_TOKEN="test-token",
        BOSS_SANDBOX_CI_TIMEOUT_SECONDS=120,
        BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS=10,
    )


def _http_error(status_code: int, url: str = "https://api.github.test/x") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", url)
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)


@pytest.mark.parametrize(
    "workflows_reply",
    [
        [],
        404,
    ],
)
def test_wait_for_verdict_returns_no_ci_immediately_without_sleeping(monkeypatch, workflows_reply):
    github = GitHubAdapter(_settings(), api_base_url="https://api.github.com")
    sleeps: list[float] = []
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.sleep", sleeps.append)

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        if "/contents/.github/workflows" in path:
            if isinstance(workflows_reply, int):
                raise _http_error(workflows_reply)
            return workflows_reply
        if path.endswith("/status"):
            return {"state": "pending", "statuses": []}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    pipeline = GitHubActionsPipeline(_settings(), github=github)
    verdict = pipeline.wait_for_verdict("deadbeef", timeout_seconds=120, poll_interval_seconds=10)

    assert verdict.status == "no_ci"
    assert verdict.completed is True
    assert verdict.checks == []
    assert sleeps == []


def test_discover_handles_empty_and_404_actions_workflows(monkeypatch):
    for reply in ({"total_count": 0, "workflows": []}, [], 404):
        github = GitHubAdapter(_settings(), api_base_url="https://api.github.com")

        def fake_request(method: str, path: str, json_body=None, _reply=reply, **_kwargs):
            if path.endswith("/actions/workflows?per_page=100"):
                if isinstance(_reply, int):
                    raise _http_error(_reply)
                return _reply
            raise AssertionError(f"unexpected call: {method} {path}")

        monkeypatch.setattr(github, "_request", fake_request)
        pipeline = GitHubActionsPipeline(_settings(), github=github)
        desc = pipeline.discover("acme/widget")
        assert desc.configured is False
        assert desc.workflows == []


def test_wait_for_verdict_polls_when_active_workflow_exists(monkeypatch):
    github = GitHubAdapter(_settings(), api_base_url="https://api.github.com")
    sleeps: list[float] = []
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.sleep", sleeps.append)
    calls = {"checks": 0}

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        if "/contents/.github/workflows" in path:
            return [{"name": "ci.yml", "path": ".github/workflows/ci.yml", "type": "file"}]
        if path.endswith("/check-runs"):
            calls["checks"] += 1
            if calls["checks"] == 1:
                return {"check_runs": []}
            return {
                "check_runs": [
                    {"name": "ci", "status": "completed", "conclusion": "success"}
                ]
            }
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    pipeline = GitHubActionsPipeline(_settings(), github=github)
    verdict = pipeline.wait_for_verdict("deadbeef", timeout_seconds=120, poll_interval_seconds=10)

    assert verdict.status == "passed"
    assert verdict.completed is True
    assert sleeps == [10]


def test_wait_for_verdict_does_not_claim_no_ci_on_403_or_commit_statuses(monkeypatch):
    github = GitHubAdapter(_settings(), api_base_url="https://api.github.com")
    sleeps: list[float] = []
    monkeypatch.setattr("boss_engine.services.pipeline_adapter.time.sleep", sleeps.append)

    # 1) 403 on workflows -> must keep polling until timeout rather than returning no_ci
    def fake_403(method: str, path: str, json_body=None, **_kwargs):
        if "/contents/.github/workflows" in path:
            raise _http_error(403)
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_403)
    pipeline = GitHubActionsPipeline(_settings(), github=github)
    verdict_403 = pipeline.wait_for_verdict("deadbeef", timeout_seconds=0, poll_interval_seconds=1)
    assert verdict_403.status == "timed_out"
    assert verdict_403.completed is False

    # 2) No workflows, but commit statuses exist -> must not falsely return no_ci
    def fake_statuses(method: str, path: str, json_body=None, **_kwargs):
        if "/contents/.github/workflows" in path:
            raise _http_error(404)
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        if path.endswith("/status"):
            return {"state": "pending", "statuses": [{"context": "ci/external", "state": "pending"}]}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_statuses)
    verdict_status = pipeline.wait_for_verdict("deadbeef", timeout_seconds=0, poll_interval_seconds=1)
    assert verdict_status.status == "timed_out"
    assert verdict_status.completed is False


def test_github_fake_serves_actions_workflows_and_runs(tmp_path: Path):
    stubs_dir = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "stubs"
    if str(stubs_dir) not in sys.path:
        sys.path.insert(0, str(stubs_dir))
    from github_fake import GitHubFake

    bare = tmp_path / "acme" / "app.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hi\n", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"], check=True, capture_output=True)

    remotes = SimpleNamespace(
        outage=False,
        owner_types={},
        is_fresh=lambda owner: False,
        exists=lambda owner, name: (owner, name) == ("acme", "app"),
        ensure=lambda owner, name: bare,
    )
    world = SimpleNamespace(prs=[], checks={})
    fake = GitHubFake(remotes, world)

    code, empty_wf = fake.handle("GET", "/repos/acme/app/actions/workflows", "per_page=100", None)
    assert code == 200 and empty_wf == {"total_count": 0, "workflows": []}

    code, runs = fake.handle("GET", "/repos/acme/app/actions/runs", "", None)
    assert code == 200 and runs == {"total_count": 0, "workflow_runs": []}

    wf_dir = work / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text("name: CI\non: push\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", ".github/workflows/ci.yml"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "add ci"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(work), "push", "origin", "main"], check=True, capture_output=True)

    code, seeded_wf = fake.handle("GET", "/repos/acme/app/actions/workflows", "per_page=100", None)
    assert code == 200
    assert seeded_wf["total_count"] == 1
    assert seeded_wf["workflows"][0]["path"] == ".github/workflows/ci.yml"
    assert seeded_wf["workflows"][0]["state"] == "active"


def test_software_factory_handles_no_ci_without_ci_blocker():
    sf = SoftwareFactoryService(_settings())
    assert sf._review_confidence_for_status("no_ci") == "ci-none-manual-review-required"
    assert "No hosted CI" in sf._review_recommendation_for_status("no_ci")
    assert any("No hosted CI" in line for line in sf._validation_follow_up_for_status("no_ci"))

    go_no_go = sf._build_release_go_no_go(
        validation_status="no_ci",
        validation_summary="No CI configured.",
        security_dependency_review={"status": "passed"},
        browser_validation={"status": "not_applicable"},
        evidence_refs=[],
    )
    assert "ci_or_sandbox_validation_not_passed" not in go_no_go["blockers"]
    assert go_no_go["status"] == "go"
