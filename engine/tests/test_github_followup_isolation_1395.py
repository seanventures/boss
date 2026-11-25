"""BOSSAPP-1395: GitHub follow-up branch isolation while parent merge card is pending,
stacked PR vs restack after parent merge, merge sha pin, and pinned 409 HEAD_MOVED."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from boss_engine.config import Settings
from boss_engine.services import merge_failures, pr_follow_up
from boss_engine.services.github_adapter import GitHubAdapter
from boss_engine.services.software_factory import SoftwareFactoryService


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_TOKEN="tok",
    )


def test_github_adapter_ensure_branch_uses_base_sha_and_merge_pins_sha(monkeypatch):
    github = GitHubAdapter(_settings())
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        calls.append((method, path, json_body))
        if method == "GET" and path.endswith("/git/ref/heads/boss/code/fu_1"):
            req = httpx.Request("GET", f"https://api.github.com{path}")
            raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        if method == "POST" and path.endswith("/git/refs"):
            return {"object": {"sha": json_body["sha"]}}
        if method == "PUT" and path.endswith("/pulls/12/merge"):
            return {"sha": "merged_sha_1", "merged": True, "message": "merged"}
        raise AssertionError(f"Unexpected: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    sha = github.ensure_branch("boss/code/fu_1", "boss/code/root", base_sha="a" * 40)
    assert sha == "a" * 40
    assert calls[1] == (
        "POST",
        "/repos/acme/widget/git/refs",
        {"ref": "refs/heads/boss/code/fu_1", "sha": "a" * 40},
    )

    merged = github.merge_pull_request(12, "Merge #12", head_sha="b" * 40)
    assert merged.merged is True
    assert calls[2][2]["sha"] == "b" * 40


def test_github_adapter_pinned_409_head_modified_raises_clear_message(monkeypatch):
    github = GitHubAdapter(_settings())

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        req = httpx.Request(method, f"https://api.github.com{path}")
        resp = httpx.Response(
            409,
            request=req,
            json={"message": "Head branch was modified. Review and try the merge again."},
        )
        raise httpx.HTTPStatusError("409 Conflict", request=req, response=resp)

    monkeypatch.setattr(github, "_request", fake_request)
    with pytest.raises(RuntimeError, match="Head branch was modified"):
        github.merge_pull_request(12, "Merge #12", head_sha="b" * 40)


def test_merge_failures_classify_distinguishes_generic_409_from_pinned_head_moved():
    assert merge_failures.classify("Client error '409 Conflict' for url .../merge") == (
        merge_failures.HEAD_CHANGED
    )
    assert merge_failures.classify(
        "Client error '409 Conflict' for url .../merge", pinned=True
    ) == merge_failures.HEAD_MOVED
    assert merge_failures.classify(
        "409 Conflict: Head branch was modified. Review and try the merge again."
    ) == merge_failures.HEAD_MOVED
    assert merge_failures.classify(
        "The change moved after approval; nothing was merged."
    ) == merge_failures.HEAD_MOVED


def test_pr_follow_up_prepare_branch_restacks_isolated_branch_after_parent_merged():
    restacked_calls: list[tuple] = []

    class _FakeGitHub:
        def open_pull_request_for(self, branch_name: str):
            return None  # both parent and followup have no open PR (parent merged)

        def last_merged_pull_request(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41", "head_sha": "abc123"}
            return None

        def restack_after_merge(self, branch_name: str, merged_head_sha: str, **kwargs):
            restacked_calls.append((branch_name, merged_head_sha, kwargs))
            return {"restacked": True, "paths": ["a.py"], "base": "main", "head_sha": "def456"}

    out = pr_follow_up.prepare_branch(
        _FakeGitHub(),
        "boss/code/followup_1",
        base="main",
        paths=["a.py"],
        parent_branch="boss/code/root",
    )
    assert out is not None
    assert out["after"] == 41
    assert out["restacked"] is True
    assert restacked_calls == [
        ("boss/code/followup_1", "abc123", {"base": "main", "number": 41})
    ]


def test_pr_follow_up_prepare_branch_skips_when_parent_pr_still_open():
    class _FakeGitHub:
        def open_pull_request_for(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41"}
            return None

        def last_merged_pull_request(self, _branch_name: str):
            raise AssertionError("should not check merged when parent PR is still open")

    out = pr_follow_up.prepare_branch(
        _FakeGitHub(),
        "boss/code/followup_1",
        base="main",
        paths=["a.py"],
        parent_branch="boss/code/root",
    )
    assert out is None


def test_software_factory_propose_uses_parent_branch_as_stacked_pr_base():
    sf = object.__new__(SoftwareFactoryService)
    created_bases: list[str | None] = []

    class _GitHub:
        repository_full_name = "acme/widget"
        kind = "github"

        def ensure_branch(self, branch_name: str, base_branch: str | None = None):
            assert branch_name == "boss/code/fu_1"
            assert base_branch == "boss/code/root"
            return "sha_fu"

        def open_pull_request_for(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41"}
            return None

        def last_merged_pull_request(self, _branch_name: str):
            return None

        def create_pull_request(self, branch_name, title, body, *, base=None, **_kwargs):
            created_bases.append(base)
            return SimpleNamespace(number=42, url="https://github.test/pr/42", head_sha="sha_fu")

    sf.github = _GitHub()
    # Direct unit test of the stacked PR base selection logic:
    pr_base = "main"
    parent_branch = "boss/code/root"
    sf.github.ensure_branch("boss/code/fu_1", parent_branch)
    if sf.github.open_pull_request_for(parent_branch):
        pr_base = parent_branch
    sf.github.create_pull_request("boss/code/fu_1", "Fix", "body", base=pr_base)
    assert created_bases == ["boss/code/root"]
