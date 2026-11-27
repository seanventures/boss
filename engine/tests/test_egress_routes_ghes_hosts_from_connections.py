"""Egress knows a GitHub Enterprise Server host set on a CONNECTION, not only the install's env
(BOSSAPP-1286 review). One list (github_connections.ghes_hosts) feeds all three:

* the CONNECT fence refuses the host (its credential is added only by the GitHub route);
* the GitHub route routes it as GitHub, upstream to THAT host, with the run project's
  connection credential bound on that host — never the install App's token;
* a run's git rewrites that host's web origin to egress, and a repository on it clones through
  /github@<host>/.
"""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.identity import github_urls
from boss_engine.services import github_connections as gc

ROOT = Path(__file__).resolve().parents[2]
GHES_API = "https://ghe.conn.example/api/v3"
HOST = "ghe.conn.example"


@pytest.fixture(autouse=True)
def _one_ghes_connection(monkeypatch):
    for name in (github_urls.API_BASE_ENV, github_urls.WEB_BASE_ENV, "BOSS_GITHUB_GIT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gc, "GHES_BASES_LOOKUP",
                        lambda db: [GHES_API, "https://api.github.com", None])
    gc.reset_ghes_hosts_cache()
    monkeypatch.syspath_prepend(str(ROOT / "apps" / "egress"))
    yield
    gc.reset_ghes_hosts_cache()


def test_the_one_list_holds_the_connections_ghes_host_and_not_github_com():
    assert gc.ghes_hosts(object()) == {HOST: GHES_API}


def test_the_connect_fence_refuses_a_connection_ghes_host(monkeypatch):
    from boss_egress import connect

    monkeypatch.setenv(connect.ALLOW_ENV, f"{HOST},allowed.example")
    monkeypatch.setattr(connect, "_ENGINE", [object()])
    assert connect.ghes_fenced(HOST)
    assert not connect.host_allowed(HOST)
    assert "GitHub proxy" in connect.refusal_reason(HOST, 443, "host_not_allowed")


class _Db:
    def begin_nested(self):
        return nullcontext()


def _proxy_world(monkeypatch, *, bound_base: str, token: str = "ghes-conn-token"):
    from boss_egress import proxy
    from boss_engine.identity import github_app
    from boss_engine.identity.principal import TaskBinding

    monkeypatch.setattr(github_app, "run_permissions", lambda claims, db=None: {"contents": "write"})
    monkeypatch.setattr(github_app, "run_repos", lambda task: ["platform/gateway"])
    monkeypatch.setattr(github_app, "push_ref_pattern", lambda claims, db=None: "boss/*")
    monkeypatch.setattr(proxy, "bitbucket_repos", lambda claims, db: [])
    monkeypatch.setattr(gc, "api_base_for_repository", lambda db, p, o, r: bound_base)
    monkeypatch.setattr(gc.GitHubConnectionService, "token_for_repository",
                        # the real source tags the token by where its secret lives (BOSSAPP-1286)
                        lambda self, db, *, project_id, owner, repo:
                        github_urls.tag(token, bound_base))

    def no_app(self, claims, db):
        raise AssertionError("the install App's token was minted for a GHES connection host")

    monkeypatch.setattr(proxy.EgressProxy, "_installation_token", no_app)
    refused: list[str] = []
    monkeypatch.setattr(proxy.EgressProxy, "_refuse",
                        lambda self, db, claims, provider, method, template, reason:
                        refused.append(reason) or reason)
    claims = SimpleNamespace(task=TaskBinding(run_id="run_x", project_id="p_1"))
    req = proxy.EgressRequest(method="GET", path="/github@" + HOST + "/platform/gateway.git/info/refs",
                              headers={}, query="service=git-upload-pack", body=b"")
    return proxy, claims, req, refused


def test_the_route_sends_the_connections_token_to_that_host_only(monkeypatch):
    proxy, claims, req, refused = _proxy_world(monkeypatch, bound_base=GHES_API)
    api = proxy.ghes_api_for(_Db(), HOST)
    assert api == GHES_API
    judged = proxy.EgressProxy(engine=None)._judge(
        _Db(), claims, "github", "/platform/gateway.git/info/refs", req, allow_delete=True,
        ghes_api=api)
    assert not refused, refused
    url, headers, _template, token, _rest = judged
    assert url.startswith(f"https://{HOST}/platform/gateway.git/info/refs")
    assert token == "ghes-conn-token" and headers["Authorization"].startswith("Basic ")
    url, headers = proxy.EgressProxy(engine=None)._upstream_request(
        "github-api", "/repos/platform/gateway", req, github_urls.tag("t", GHES_API), api=GHES_API)
    assert url.startswith(f"{GHES_API}/repos/platform/gateway")


def test_a_repo_not_bound_on_that_host_gets_no_credential(monkeypatch):
    proxy, claims, req, refused = _proxy_world(monkeypatch, bound_base=github_urls.PUBLIC_API)
    proxy.EgressProxy(engine=None)._judge(
        _Db(), claims, "github", "/platform/gateway.git/info/refs", req, allow_delete=True,
        ghes_api=GHES_API)
    assert refused == ["ghes_repo_not_bound"], refused


def test_a_runs_git_rewrites_the_ghes_origin_and_clones_through_its_route(monkeypatch):
    from boss_engine.identity import runs

    monkeypatch.setenv(runs.EGRESS_URL_ENV, "https://egress.internal")
    shim = runs.Shim(run_id="run_ghes_rewrite")
    try:
        access = runs.egress_access(shim, owner="platform", repo="gateway", api_base=GHES_API,
                                    db=object())
    finally:
        runs.close_shim_socket(shim.run_id)
    assert access.clone_url == f"https://egress.internal/github@{HOST}/platform/gateway.git"
    assert (f"url.https://egress.internal/github@{HOST}/.insteadOf", f"https://{HOST}/") \
        in access.git_config
    assert ("url.https://egress.internal/github/.insteadOf", "https://github.com/") \
        in access.git_config
    # A github.com repository still clones through the plain GitHub route.
    shim2 = runs.Shim(run_id="run_gh_rewrite")
    try:
        plain = runs.egress_access(shim2, owner="o", repo="r", db=object())
    finally:
        runs.close_shim_socket(shim2.run_id)
    assert plain.clone_url == "https://egress.internal/github/o/r.git"



def test_the_list_is_read_from_the_connections_table(monkeypatch):
    """The real read: an active connection's api_base_url on a fresh database (no env)."""
    from sqlalchemy import text

    from ._ops2_support import fresh_engine, make_project

    engine, drop = fresh_engine("ghesh")
    try:
        project = make_project(engine)
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                "credential_ref, status, api_base_url, created_by) VALUES ('ghc_x', :p, 'GHES', "
                "'personal_access_token', 'env:X', 'active', :b, 'ceo')"), {"p": project, "b": GHES_API})
        monkeypatch.setattr(gc, "GHES_BASES_LOOKUP", None)
        gc.reset_ghes_hosts_cache()
        assert gc.ghes_hosts(engine) == {HOST: GHES_API}
        gc.reset_ghes_hosts_cache()
        with engine.begin() as c:
            assert gc.ghes_hosts(c) == {HOST: GHES_API}
    finally:
        drop()
