"""BOSS-267 — the repo-host extension seam: protocol conformance for both plugins, factory
dispatch (github default = zero behavior change), centralized clone URLs, and the Bitbucket
adapter's request shaping/normalization against a stubbed wire."""
import httpx
import pytest

from boss_engine.config import Settings
from boss_engine.services import bitbucket_adapter as bb
from boss_engine.services.bitbucket_adapter import BitbucketAdapter
from boss_engine.services.github_adapter import GitHubAdapter, PullRequestResult
from boss_engine.services.repo_host import (
    RepoHost,
    clone_url_for,
    make_repo_host,
    resolve_repo_host_kind,
)


def _settings(**kw) -> Settings:
    # Settings is alias-driven (extra="ignore" silently drops field names) — use env aliases.
    base = {"BOSS_BITBUCKET_TOKEN": "bbtok", "BOSS_TARGET_BITBUCKET_WORKSPACE": "acme-ws",
            "BOSS_TARGET_BITBUCKET_REPO": "shop"}
    base.update(kw)
    return Settings(**base)


# --- protocol + factory -------------------------------------------------------
def test_both_adapters_conform_to_the_protocol():
    assert isinstance(GitHubAdapter(_settings()), RepoHost)
    assert isinstance(BitbucketAdapter(_settings()), RepoHost)


def test_factory_defaults_to_github_zero_behavior_change():
    host = make_repo_host(Settings())
    assert host.kind == "github" and isinstance(host, GitHubAdapter)


def test_factory_dispatches_and_rejects_unknown():
    assert make_repo_host(_settings(), host="bitbucket").kind == "bitbucket"
    assert make_repo_host(_settings(BOSS_TARGET_REPO_HOST="bitbucket")).kind == "bitbucket"
    with pytest.raises(ValueError, match="unsupported repo host"):
        make_repo_host(_settings(), host="gitlab")


def test_resolve_kind_spec_wins_over_settings():
    s = _settings(BOSS_TARGET_REPO_HOST="github")
    assert resolve_repo_host_kind(s, {"repo_host": "bitbucket"}) == "bitbucket"
    assert resolve_repo_host_kind(s, {}) == "github"
    with pytest.raises(ValueError):
        resolve_repo_host_kind(s, {"repo_host": "svn"})


# --- clone URLs (the one place that knows each host's credential form) --------
def test_clone_url_forms():
    assert clone_url_for("github", "o", "r", "T") == "https://x-access-token:T@github.com/o/r.git"
    assert clone_url_for("github", "o", "r") == "https://github.com/o/r.git"
    assert clone_url_for("bitbucket", "ws", "slug", "T") == \
        "https://x-token-auth:T@bitbucket.org/ws/slug.git"
    assert clone_url_for("bitbucket", "ws", "slug") == "https://bitbucket.org/ws/slug.git"
    with pytest.raises(ValueError):
        clone_url_for("gitlab", "o", "r")


# --- bitbucket adapter (stubbed wire) -----------------------------------------
@pytest.fixture
def wire(monkeypatch):
    calls = []

    def fake(method, url, *, headers=None, auth=None, json=None, data=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers or {},
                      "auth": auth, "json": json, "data": data})
        req = httpx.Request(method, url)
        path = url.split("/2.0")[1]
        if path.endswith("/refs/branches/boss/code/x1"):
            return httpx.Response(404, request=req, json={"type": "error"})
        if path.endswith("/refs/branches/main"):
            return httpx.Response(200, request=req,
                                  json={"target": {"hash": "basehash"}})
        if path.endswith("/refs/branches") and method == "POST":
            return httpx.Response(201, request=req, json={"target": {"hash": "newhash"}})
        if path.endswith("/src"):
            return httpx.Response(201, request=req, content=b"")
        if "/pullrequests?" in path:
            return httpx.Response(200, request=req, json={"values": []})
        if path.endswith("/pullrequests") and method == "POST":
            return httpx.Response(201, request=req, json={
                "id": 7, "links": {"html": {"href": "https://bitbucket.org/acme-ws/shop/pull-requests/7"}}})
        if path.endswith("/merge"):
            return httpx.Response(200, request=req, json={
                "state": "MERGED", "merge_commit": {"hash": "mergesha"}})
        if path.endswith("/comments"):
            return httpx.Response(201, request=req, json={
                "id": 42, "links": {"html": {"href": "https://bitbucket.org/c/42"}}})
        if path.endswith("/diff"):
            return httpx.Response(200, request=req, content=b"diff --git a/x b/x")
        if path.endswith("/repositories/acme-ws/shop"):
            return httpx.Response(200, request=req, json={"mainbranch": {"name": "main"}})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bb.httpx, "request", fake)
    return calls


def test_bitbucket_ensure_branch_creates_off_default(wire):
    a = BitbucketAdapter(_settings())
    assert a.ensure_branch("boss/code/x1") == "newhash"
    # 404 on the branch -> read repo mainbranch -> read base -> create from base hash
    assert wire[-1]["json"] == {"name": "boss/code/x1", "target": {"hash": "basehash"}}


def test_bitbucket_upsert_and_pr_flow(wire):
    a = BitbucketAdapter(_settings())
    a.upsert_file("b1", "src/App.tsx", "content", "feat: app")
    src_call = wire[-1]
    assert src_call["data"]["src/App.tsx"] == "content"
    assert src_call["data"]["branch"] == "b1"

    pr = a.create_pull_request("b1", "Title", "Body")
    assert pr == PullRequestResult(url="https://bitbucket.org/acme-ws/shop/pull-requests/7", number=7)
    create = wire[-1]["json"]
    assert create["source"]["branch"]["name"] == "b1"
    assert create["destination"]["branch"]["name"] == "main"


def test_bitbucket_merge_comment_diff(wire):
    a = BitbucketAdapter(_settings(BOSS_SANDBOX_MERGE_METHOD="squash"))
    m = a.merge_pull_request(7, "merge it")
    assert m.merged is True and m.sha == "mergesha"
    assert wire[-1]["json"]["merge_strategy"] == "squash"
    c = a.create_issue_comment(7, "hello")
    assert c.id == 42
    assert wire[-1]["json"] == {"content": {"raw": "hello"}}
    assert a.get_pull_request_diff(7).startswith("diff --git")


def test_bitbucket_auth_modes(wire):
    BitbucketAdapter(_settings()).default_branch()
    assert wire[-1]["headers"]["Authorization"] == "Bearer bbtok"     # access token
    BitbucketAdapter(_settings(BOSS_BITBUCKET_TOKEN=None, BOSS_BITBUCKET_USERNAME="u",
                               BOSS_BITBUCKET_APP_PASSWORD="pw")).default_branch()
    assert wire[-1]["auth"] == ("u", "pw")                            # app password
    from boss_engine.services.github_adapter import IntegrationConfigurationError
    with pytest.raises(IntegrationConfigurationError):
        BitbucketAdapter(_settings(BOSS_BITBUCKET_TOKEN=None)).default_branch()


def test_bitbucket_workspace_fallback_to_github_target():
    # single-target deployments only set what differs
    a = BitbucketAdapter(Settings(BOSS_TARGET_GITHUB_OWNER="o", BOSS_TARGET_GITHUB_REPO="r",
                                  BOSS_BITBUCKET_TOKEN="t"))
    assert a.repository_full_name == "o/r"
    b = BitbucketAdapter(_settings())
    assert b.repository_full_name == "acme-ws/shop"
    assert b.clone_url("") == "https://x-token-auth:bbtok@bitbucket.org/acme-ws/shop.git"
