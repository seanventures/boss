"""BOSS-269 — Phase 2 of the repo-host seam: Bitbucket Pipelines CI adapter, the MCP-backed
(SSO/OAuth identity) repo host, webhook payload normalization, and factory/registry coverage."""
import pytest

from boss_engine.config import Settings
from boss_engine.services.github_adapter import PullRequestResult
from boss_engine.services.mcp_repo_host import McpRepoHost
from boss_engine.services.pipeline_adapter import (
    BitbucketPipelines,
    make_pipeline_adapter,
)
from boss_engine.services.repo_host import KNOWN_REPO_HOSTS, RepoHost, make_repo_host
from boss_engine.services.webhooks import InboundWebhookService


def _settings(**kw) -> Settings:
    base = {"BOSS_BITBUCKET_TOKEN": "bbtok", "BOSS_TARGET_BITBUCKET_WORKSPACE": "acme-ws",
            "BOSS_TARGET_BITBUCKET_REPO": "shop"}
    base.update(kw)
    return Settings(**base)


# --- Bitbucket Pipelines ------------------------------------------------------
class _FakeBB:
    repository_full_name = "acme-ws/shop"

    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def _repo_path(self):
        return "/repositories/acme-ws/shop"

    def _request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path.endswith("/pipelines/") and method == "POST":
            return {"uuid": "{pipe-1}"}
        if "/pipelines/{pipe-1}" in path:
            return self.states.pop(0)
        if path.endswith("/pipelines_config"):
            return {"enabled": True}
        raise AssertionError(path)


def test_bitbucket_pipelines_trigger_poll_verdict():
    bb = _FakeBB([
        {"state": {"name": "IN_PROGRESS"}},
        {"state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
         "created_on": "t0", "completed_on": "t1"},
        {"state": {"name": "COMPLETED", "result": {"name": "FAILED"}}},
    ])
    p = BitbucketPipelines(_settings(), bitbucket=bb)
    handle = p.trigger("boss/code/x1")
    assert handle.external_id == "{pipe-1}"
    assert bb.calls[0][2]["json_body"]["target"]["ref_name"] == "boss/code/x1"

    running = p.verdict(handle)
    assert running.completed is False and running.status == "in_progress"
    passed = p.verdict(handle)
    assert passed.completed is True and passed.status == "passed"
    assert passed.checks[0].name == "Bitbucket Pipelines"
    failed = p.verdict(handle)
    assert failed.status == "failed" and "failed" in failed.summary


def test_pipeline_factory_dispatches_bitbucket():
    adapter = make_pipeline_adapter(_settings(BOSS_PIPELINE_PROVIDER="bitbucket_pipelines"))
    assert adapter.provider == "bitbucket_pipelines"


# --- MCP-backed repo host (SSO/OAuth identity path) ---------------------------
class _FakeMcp:
    def __init__(self, tools, results=None):
        self.tools = tools
        self.results = results or {}
        self.calls = []

    def list_tools(self, url, headers=None):
        self.calls.append(("list", url, headers))
        return [{"name": t} for t in self.tools]

    def call_tool(self, url, name, args, headers=None):
        self.calls.append(("call", name, args, headers))
        return self.results.get(name, {})


def _mcp_settings(**kw):
    return _settings(BOSS_REPO_MCP_URL="https://gateway.corp/mcp",
                     BOSS_REPO_MCP_AUTH_ENV="CORP_MCP_TOKEN", **kw)


def test_mcp_host_requires_url_and_conforms():
    with pytest.raises(Exception, match="BOSS_REPO_MCP_URL"):
        McpRepoHost(_settings())
    host = McpRepoHost(_mcp_settings(), client=_FakeMcp([]))
    assert isinstance(host, RepoHost) and host.kind == "mcp"
    assert host.repository_full_name == "acme-ws/shop"


def test_mcp_host_discovers_prefixed_tools_and_calls_with_identity(monkeypatch):
    monkeypatch.setenv("CORP_MCP_TOKEN", "sso-session")
    mcp = _FakeMcp(
        tools=["bitbucket_create_pull_request", "bitbucket_merge_pull_request",
               "bitbucket_add_pull_request_comment"],
        results={"bitbucket_create_pull_request":
                 {"id": 12, "url": "https://bitbucket.org/acme-ws/shop/pull-requests/12"}})
    host = McpRepoHost(_mcp_settings(), client=mcp)
    pr = host.create_pull_request("b1", "Title", "Body")
    assert pr == PullRequestResult(url="https://bitbucket.org/acme-ws/shop/pull-requests/12",
                                   number=12)
    call = next(c for c in mcp.calls if c[0] == "call")
    assert call[1] == "bitbucket_create_pull_request"          # suffix-discovered
    assert call[2]["repository"] == "acme-ws/shop"
    assert call[3] == {"Authorization": "Bearer sso-session"}  # user identity rides the gateway


def test_mcp_host_toolmap_override_and_minted_clone_token(monkeypatch):
    monkeypatch.delenv("CORP_MCP_TOKEN", raising=False)
    mcp = _FakeMcp(tools=[], results={"corp_pr_open": {"id": 3, "url": "u"},
                                      "get_access_token": {"token": "oauth-tok"}})
    host = McpRepoHost(
        _mcp_settings(BOSS_REPO_MCP_TOOLMAP='{"create_pull_request": "corp_pr_open"}'),
        client=mcp)
    assert host.create_pull_request("b", "t", "d").number == 3
    # clone credential minted from the gateway (OAuth token works as x-token-auth)
    assert host.clone_url("") == "https://x-token-auth:oauth-tok@bitbucket.org/acme-ws/shop.git"


def test_factory_kind_mcp():
    assert "mcp" in KNOWN_REPO_HOSTS
    host = make_repo_host(_mcp_settings(), host="mcp")
    assert host.kind == "mcp"


# --- webhook normalization ----------------------------------------------------
def test_normalize_bitbucket_pullrequest_payload():
    payload = {"pullrequest": {
        "id": 9, "title": "Fix checkout", "state": "MERGED",
        "source": {"branch": {"name": "boss/code/r1"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": "https://bitbucket.org/w/r/pull-requests/9"}},
        "author": {"nickname": "dev1"},
        "merge_commit": {"hash": "abc123"},
    }}
    n = InboundWebhookService._normalize_bitbucket_payload("pullrequest", "fulfilled", payload)
    assert n["merged"] is True and n["number"] == 9
    assert n["source_branch"] == "boss/code/r1" and n["destination_branch"] == "main"
    assert n["merge_commit"] == "abc123" and n["author"] == "dev1"
    assert InboundWebhookService._normalize_bitbucket_payload("repo", "push", {}) == {}
