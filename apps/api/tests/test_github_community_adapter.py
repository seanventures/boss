from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from apps.api.app.config import Settings
from apps.api.app.services.github_adapter import (
    GitHubAdapter,
    GitHubMergeConflictError,
    GitHubMergeRejectedError,
)
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

    def usable_token_for_repo(self, owner: str, repo: str) -> tuple[str, list[str]]:
        self.repositories.append((owner, repo))
        return "app-installation-token", []

    def token_for_repo(self, owner: str, repo: str) -> str:
        return self.usable_token_for_repo(owner, repo)[0]


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


def test_rate_limit_status_reads_core_quota_without_repository_mutation(
    monkeypatch,
):
    calls: list[dict] = []

    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        return _response(
            method,
            url,
            200,
            json_body={
                "resources": {
                    "core": {
                        "limit": 5000,
                        "remaining": 3210,
                        "used": 1790,
                        "reset": 4102444800,
                    }
                }
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = GitHubAdapter(_settings())

    assert adapter.rate_limit_status() == {
        "resource": "core",
        "limit": 5000,
        "remaining": 3210,
        "used": 1790,
        "reset_epoch": 4102444800,
    }
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/rate_limit")
    assert calls[0]["json"] is None


def test_safe_branch_write_primitives_are_non_force_and_conflicts_are_typed(
    monkeypatch,
):
    calls: list[dict] = []
    old_sha = "a" * 40
    new_sha = "b" * 40

    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        if url.endswith("/compare/base...head"):
            return _response(
                method,
                url,
                200,
                json_body={
                    "files": [
                        {
                            "filename": "engine/example.py",
                            "status": "modified",
                            "additions": 2,
                            "deletions": 1,
                            "changes": 3,
                            "patch": "x" * 25000,
                        }
                    ]
                },
            )
        if method == "POST" and url.endswith("/git/refs"):
            return _response(
                method,
                url,
                201,
                json_body={"object": {"sha": old_sha}},
            )
        if method == "GET" and "/git/ref/heads/ai%2Fcodex%2Fwork" in url:
            return _response(
                method, url, 200, json_body={"object": {"sha": old_sha}}
            )
        if method == "GET" and url.endswith(f"/git/commits/{old_sha}"):
            return _response(
                method,
                url,
                200,
                json_body={"tree": {"sha": "1" * 40}, "parents": []},
            )
        if method == "GET" and "/branches/ai%2Fcodex%2Fwork" in url:
            return _response(
                method,
                url,
                200,
                json_body={"protected": False, "protection": {}},
            )
        if method == "PATCH" and "/git/refs/heads/ai%2Fcodex%2Fwork" in url:
            return _response(
                method,
                url,
                200,
                json_body={"object": {"sha": new_sha}},
            )
        if method == "POST" and url.endswith("/merges"):
            return _response(
                method,
                url,
                409,
                json_body={"message": "Merge conflict"},
            )
        if method == "POST" and url.endswith("/git/blobs"):
            return _response(
                method, url, 201, json_body={"sha": "2" * 40}
            )
        if method == "POST" and url.endswith("/git/trees"):
            return _response(
                method, url, 201, json_body={"sha": "3" * 40}
            )
        if method == "POST" and url.endswith("/git/commits"):
            return _response(
                method,
                url,
                201,
                json_body={"sha": "4" * 40, "tree": {"sha": "3" * 40}},
            )
        raise AssertionError(f"unexpected GitHub request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = GitHubAdapter(_settings(), token="scoped-token")
    files = adapter.compare_changed_files("base", "head")
    created = adapter.create_branch_ref("ai/codex/work", old_sha)
    updated = adapter.fast_forward_ref(
        "ai/codex/work",
        expected_sha=old_sha,
        new_sha=new_sha,
    )
    with pytest.raises(GitHubMergeConflictError):
        adapter.merge_branch(
            base_branch="ai/codex/work",
            head_sha=new_sha,
            message="merge",
        )
    resolution = adapter.create_resolution_commit(
        base_tree_sha="1" * 40,
        parent_shas=[old_sha, new_sha],
        resolved_files=[
            {
                "path": "engine/example.py",
                "content": "resolved = True\n",
                "mode": "100644",
            }
        ],
        message="resolve",
    )

    assert len(files[0]["patch"]) == 20000
    assert created == {"ref": "ai/codex/work", "commit_sha": old_sha}
    assert updated["forced"] is False
    assert resolution["commit_sha"] == "4" * 40
    ref_update = next(call for call in calls if call["method"] == "PATCH")
    assert ref_update["json"] == {"sha": new_sha, "force": False}
    commit_call = next(
        call
        for call in calls
        if call["method"] == "POST" and call["url"].endswith("/git/commits")
    )
    assert commit_call["json"]["parents"] == [old_sha, new_sha]


def test_guarded_pull_request_merge_binds_exact_snapshot(monkeypatch):
    calls: list[dict] = []
    head_sha = "a" * 40
    base_sha = "b" * 40

    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "GET" and url.endswith(f"/git/commits/{head_sha}"):
            return _response(
                method,
                url,
                200,
                json_body={"sha": head_sha, "tree": {"sha": "1" * 40}},
            )
        if method == "GET" and url.endswith("/pulls/42"):
            return _response(
                method,
                url,
                200,
                json_body={
                    "number": 42,
                    "html_url": "https://github.test/pull/42",
                    "state": "open",
                    "draft": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head": {
                        "ref": "ai/codex/work",
                        "sha": head_sha,
                        "repo": {"full_name": "acme/widget"},
                    },
                    "base": {
                        "ref": "main",
                        "sha": base_sha,
                        "repo": {"full_name": "acme/widget"},
                    },
                    "updated_at": "2026-07-26T16:00:00Z",
                },
            )
        if method == "GET" and "/pulls/42/reviews" in url:
            return _response(
                method,
                url,
                200,
                json_body=[
                    {
                        "id": 7,
                        "user": {"login": "cto"},
                        "state": "APPROVED",
                        "commit_id": head_sha,
                        "submitted_at": "2026-07-26T16:01:00Z",
                    }
                ],
            )
        if method == "PUT" and url.endswith("/pulls/42/merge"):
            return _response(
                method,
                url,
                200,
                json_body={
                    "sha": "c" * 40,
                    "merged": True,
                    "message": "Pull Request successfully merged",
                },
            )
        raise AssertionError(f"unexpected GitHub request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = GitHubAdapter(_settings(), token="scoped-token")

    assert adapter.commit_identity(head_sha) == {
        "commit_sha": head_sha,
        "tree_sha": "1" * 40,
    }
    snapshot = adapter.pull_request_snapshot(42)
    reviews = adapter.pull_request_reviews(42)
    merged = adapter.merge_pull_request_guarded(
        pull_number=42,
        expected_head_sha=head_sha,
        expected_base_sha=base_sha,
        expected_head_ref="ai/codex/work",
        expected_base_ref="main",
        merge_method="merge",
        commit_title="governed merge",
    )

    assert snapshot["head_sha"] == head_sha
    assert snapshot["base_sha"] == base_sha
    assert reviews == [
        {
            "id": 7,
            "reviewer": "cto",
            "state": "APPROVED",
            "commit_sha": head_sha,
            "submitted_at": "2026-07-26T16:01:00Z",
        }
    ]
    assert merged["commit_sha"] == "c" * 40
    merge_call = next(call for call in calls if call["method"] == "PUT")
    assert merge_call["json"] == {
        "sha": head_sha,
        "commit_title": "governed merge",
        "merge_method": "merge",
    }


def test_guarded_pull_request_merge_rejects_stale_head_without_write(monkeypatch):
    calls: list[dict] = []

    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        return _response(
            method,
            url,
            200,
            json_body={
                "number": 42,
                "html_url": "https://github.test/pull/42",
                "state": "open",
                "draft": False,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {
                    "ref": "ai/codex/work",
                    "sha": "c" * 40,
                    "repo": {"full_name": "acme/widget"},
                },
                "base": {
                    "ref": "main",
                    "sha": "b" * 40,
                    "repo": {"full_name": "acme/widget"},
                },
                "updated_at": "2026-07-26T16:00:00Z",
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = GitHubAdapter(_settings(), token="scoped-token")

    with pytest.raises(
        GitHubMergeRejectedError,
        match="snapshot changed",
    ):
        adapter.merge_pull_request_guarded(
            pull_number=42,
            expected_head_sha="a" * 40,
            expected_base_sha="b" * 40,
            expected_head_ref="ai/codex/work",
            expected_base_ref="main",
            merge_method="merge",
            commit_title="governed merge",
        )

    assert [call["method"] for call in calls] == ["GET"]


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
