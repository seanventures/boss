from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from apps.api.app.config import Settings
from apps.api.app.services.github_adapter import GitHubAdapter
from apps.api.app.services.github_app_adapter import (
    GitHubAppAdapter,
    InstallationToken,
)


class _PatOnly:
    configured = False


class _InstalledApp:
    configured = True

    def __init__(self) -> None:
        self.repositories: list[tuple[str, str]] = []

    def token_for_repo(self, owner: str, repo: str) -> str:
        self.repositories.append((owner, repo))
        return "app-installation-token"


def _settings() -> Settings:
    return Settings(
        BOSS_GITHUB_TOKEN="legacy-token",
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_APP_ID=None,
        BOSS_GITHUB_APP_PRIVATE_KEY=None,
        BOSS_GITHUB_APP_PRIVATE_KEY_PATH=None,
        BOSS_GITHUB_APP_INSTALLATION_ID=None,
    )


def _response(
    method: str,
    url: str,
    status_code: int,
    *,
    json_body: dict | None = None,
    text: str = "",
) -> httpx.Response:
    request = httpx.Request(method, url)
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, text=text, request=request)


def test_comment_review_and_diff_use_existing_app_auth_path(monkeypatch):
    calls: list[dict] = []

    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        if url.endswith("/issues/41/comments"):
            return _response(
                method,
                url,
                201,
                json_body={"id": 101, "html_url": "https://github.test/comment/101"},
            )
        if url.endswith("/pulls/42/reviews"):
            return _response(
                method,
                url,
                200,
                json_body={
                    "id": 202,
                    "html_url": "https://github.test/review/202",
                    "state": "COMMENTED",
                },
            )
        return _response(method, url, 200, text="diff --git a/a.py b/a.py\n")

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = GitHubAdapter(_settings())
    app = _InstalledApp()
    adapter._app = app

    comment = adapter.create_issue_comment(41, "Thanks for the report.")
    review = adapter.create_review(
        42,
        "One inline finding.",
        comments=[
            {"path": "a.py", "line": 7, "side": "RIGHT", "body": "Handle None."}
        ],
    )
    diff = adapter.get_pull_request_diff(42)

    assert (comment.id, comment.url) == (101, "https://github.test/comment/101")
    assert (review.id, review.state) == (202, "COMMENTED")
    assert diff.startswith("diff --git")
    assert app.repositories == [("acme", "widget")] * 3
    assert all(
        call["headers"]["Authorization"] == "Bearer app-installation-token"
        for call in calls
    )
    assert calls[0]["json"] == {"body": "Thanks for the report."}
    assert calls[1]["json"] == {
        "body": "One inline finding.",
        "event": "COMMENT",
        "comments": [
            {"path": "a.py", "line": 7, "side": "RIGHT", "body": "Handle None."}
        ],
    }
    assert calls[2]["headers"]["Accept"] == "application/vnd.github.v3.diff"


def test_branch_snapshot_compare_and_checks_use_immutable_github_reads(monkeypatch):
    calls: list[str] = []

    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        calls.append(url)
        if "/git/ref/heads/ai%2Fcodex%2Fwork" in url:
            return _response(
                method,
                url,
                200,
                json_body={"object": {"sha": "a" * 40}},
            )
        if "/git/commits/" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "tree": {"sha": "1" * 40},
                    "parents": [{"sha": "b" * 40}],
                },
            )
        if "/branches/ai%2Fcodex%2Fwork" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "protected": True,
                    "protection": {
                        "required_status_checks": {"contexts": ["test"]},
                        "enforce_admins": {"enabled": True},
                    },
                },
            )
        if "/compare/" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "status": "ahead",
                    "ahead_by": 2,
                    "behind_by": 0,
                    "total_commits": 2,
                    "merge_base_commit": {
                        "sha": "b" * 40,
                        "commit": {"tree": {"sha": "2" * 40}},
                    },
                },
            )
        if "/check-runs" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "check_runs": [
                        {
                            "name": "test",
                            "status": "completed",
                            "conclusion": "success",
                            "app": {"slug": "github-actions"},
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub request: {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = GitHubAdapter(
        _settings(),
        owner="octo",
        repo="project",
        token="scoped-token",
        api_base_url="https://github.example/api/v3",
    )

    snapshot = adapter.branch_snapshot("ai/codex/work")
    comparison = adapter.compare_commits("b" * 40, "a" * 40)
    checks = adapter.commit_checks("a" * 40)

    assert snapshot == {
        "ref": "ai/codex/work",
        "exists": True,
        "commit_sha": "a" * 40,
        "tree_sha": "1" * 40,
        "parents": ["b" * 40],
        "protected": True,
        "protection": {
            "enabled": True,
            "required_status_checks": {"contexts": ["test"]},
            "required_pull_request_reviews": None,
            "required_signatures": None,
            "enforce_admins": {"enabled": True},
        },
    }
    assert comparison["status"] == "ahead"
    assert comparison["ahead_by"] == 2
    assert comparison["merge_base_tree_sha"] == "2" * 40
    assert checks == [
        {
            "name": "test",
            "status": "completed",
            "conclusion": "success",
            "app": "github-actions",
        }
    ]
    assert all(url.startswith("https://github.example/api/v3/") for url in calls)
    assert any("ai%2Fcodex%2Fwork" in url for url in calls)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda adapter: adapter.create_issue_comment(41, "body"),
        lambda adapter: adapter.create_review(42, "body"),
        lambda adapter: adapter.get_pull_request_diff(42),
    ],
)
def test_community_methods_surface_github_errors(
    monkeypatch, invoke: Callable[[GitHubAdapter], object]
):
    def denied(method: str, url: str, **kwargs) -> httpx.Response:
        return _response(method, url, 403, json_body={"message": "forbidden"})

    monkeypatch.setattr(httpx, "request", denied)
    adapter = GitHubAdapter(_settings())
    adapter._app = _PatOnly()

    with pytest.raises(httpx.HTTPStatusError):
        invoke(adapter)


def test_review_rejects_unknown_event_without_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid review event must not call GitHub")

    monkeypatch.setattr(httpx, "request", unexpected)
    adapter = GitHubAdapter(_settings())

    with pytest.raises(ValueError, match="event must be"):
        adapter.create_review(42, "body", event="MERGE")


def test_app_token_requests_community_write_permissions(monkeypatch):
    captured: dict = {}
    app = GitHubAppAdapter(_settings())

    def mint(**kwargs) -> InstallationToken:
        captured.update(kwargs)
        return InstallationToken(
            token="scoped-token",
            expires_at="",
            permissions=kwargs["permissions"],
            repository_selection="selected",
        )

    monkeypatch.setattr(app, "installation_token", mint)

    assert app.token_for_repo("acme", "widget") == "scoped-token"
    assert captured == {
        "repositories": ["widget"],
        "permissions": {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
        },
    }


def test_partially_configured_app_is_inert_with_blank_key_path():
    settings = Settings(
        BOSS_GITHUB_APP_ID="123",
        BOSS_GITHUB_APP_PRIVATE_KEY="",
        BOSS_GITHUB_APP_PRIVATE_KEY_PATH="",
        BOSS_GITHUB_APP_INSTALLATION_ID="456",
    )

    assert GitHubAppAdapter(settings).configured is False
