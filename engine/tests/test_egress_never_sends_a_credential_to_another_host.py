"""Egress never sends a GitHub credential to a host it does not belong to (BOSSAPP-1286, PR #330
third verification, S1a).

THE LEAK: a sandbox ran `git clone https://github.com/<owner>/<repo>` for a repository bound to a
GitHub Enterprise Server connection. The plain /github/ route asked the credential ladder, whose
bound-connection step (_bound_connection_token) handed back the GHES connection's token without
looking at the connection's api_base_url, and the proxy put it on the wire to github.com as
x-access-token.

Now: (1) every token source tags its credential with its host (github_urls.GitHubCredential); the
bound connection's token carries THAT connection's API base; (2) the install's route refuses a
repository bound to another host before any credential is chosen; (3) the send point
(_upstream_request -> github_urls.check_send) refuses a credential tagged for another host even
if the route check were bypassed; (4) the CONNECT fence fails CLOSED when the GHES host list
cannot be read.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.identity import github_urls
from boss_engine.services import github_connections as gc

ROOT = Path(__file__).resolve().parents[2]
GHES_API = "https://ghe.conn.example/api/v3"
GHES_TOKEN = "ghes-connection-token-for-s1a"      # a placeholder, not a credential
OWNER, REPO = "platform", "gateway"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (github_urls.API_BASE_ENV, github_urls.WEB_BASE_ENV, github_urls.GIT_BASE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.syspath_prepend(str(ROOT / "apps" / "egress"))
    gc.reset_ghes_hosts_cache()
    yield
    gc.reset_ghes_hosts_cache()


@pytest.fixture(scope="module")
def world():
    """A project whose platform/gateway is bound to a GHES connection (real database)."""
    from ._ops2_support import fresh_engine, make_project

    engine, drop = fresh_engine("egxh")
    project = make_project(engine)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, api_base_url, created_by) VALUES (:i, :p, 'GHES', "
            "'personal_access_token', 'env:GHES_X', 'active', :b, 'ceo')"),
            {"i": f"ghc_{project}", "p": project, "b": GHES_API})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, created_by) VALUES (:i, :p, :c, :o, :n, :f, 'primary', 'ceo')"),
            {"i": f"ghb_{project}", "p": project, "c": f"ghc_{project}", "o": OWNER, "n": REPO,
             "f": f"{OWNER}/{REPO}"})
    yield engine, project
    drop()


def _token_for_repository(monkeypatch):
    monkeypatch.setattr(gc.GitHubConnectionService, "token_for_repository",
                        # the real source tags the token with where its secret lives (this
                        # connection's own stored secret: the GHES host); egress keeps that tag
                        lambda self, db, *, project_id, owner, repo:
                        github_urls.tag(GHES_TOKEN, GHES_API))


def test_the_bound_connection_token_is_tagged_with_that_connections_host(world, monkeypatch):
    from boss_engine.identity import github_app
    from boss_engine.identity.principal import TaskBinding

    engine, project = world
    _token_for_repository(monkeypatch)
    claims = SimpleNamespace(task=TaskBinding(run_id="run_s1a", project_id=project))
    with engine.begin() as db:
        token = github_app._bound_connection_token(claims, [f"{OWNER}/{REPO}"], db=db)  # noqa: SLF001
    assert token == GHES_TOKEN
    assert isinstance(token, github_urls.GitHubCredential) and token.api_base == GHES_API
    with pytest.raises(github_urls.CrossHostCredential):
        github_urls.check_send(token, f"https://github.com/{OWNER}/{REPO}.git/info/refs")


def _proxy(monkeypatch, project):
    """The real proxy judge on the plain /github/ route, its upstream recorded; the credential
    ladder is the bound-connection step (the source of the S1a leak)."""
    from boss_egress import proxy
    from boss_engine.identity import github_app
    from boss_engine.identity.principal import TaskBinding

    monkeypatch.setattr(github_app, "run_permissions", lambda claims, db=None: {"contents": "write"})
    monkeypatch.setattr(github_app, "run_repos", lambda task: [f"{OWNER}/{REPO}"])
    monkeypatch.setattr(github_app, "push_ref_pattern", lambda claims, db=None: "boss/*")
    monkeypatch.setattr(proxy, "bitbucket_repos", lambda claims, db: [])

    def ladder(self, claims, db):
        return github_app._bound_connection_token(claims, [f"{OWNER}/{REPO}"], db=db)  # noqa: SLF001

    monkeypatch.setattr(proxy.EgressProxy, "_installation_token", ladder)
    refused: list[str] = []
    monkeypatch.setattr(proxy.EgressProxy, "_refuse",
                        lambda self, db, claims, provider, method, template, reason:
                        refused.append(reason) or proxy.EgressResponse(403, {}, reason.encode()))
    sent: list[tuple[str, dict]] = []

    def upstream(method, url, headers, body):
        sent.append((url, dict(headers)))
        return proxy.EgressResponse(200, {}, b"")

    claims = SimpleNamespace(task=TaskBinding(run_id="run_s1a", project_id=project))
    req = proxy.EgressRequest(method="GET", path=f"/github/{OWNER}/{REPO}.git/info/refs",
                              query="service=git-upload-pack")
    return proxy, proxy.EgressProxy(engine=None, upstream=upstream), claims, req, refused, sent


def test_s1a_a_sandbox_clone_of_a_ghes_bound_repo_on_the_github_com_route_is_refused(world,
                                                                                    monkeypatch):
    """EXACTLY the repro: `git clone https://github.com/platform/gateway` from a sandbox, the repo
    bound to a GHES connection. Refused; the GHES token never reaches github.com."""
    engine, project = world
    _token_for_repository(monkeypatch)
    proxy, egress, claims, req, refused, sent = _proxy(monkeypatch, project)
    with engine.begin() as db:
        judged = egress._judge(db, claims, "github", f"/{OWNER}/{REPO}.git/info/refs", req,  # noqa: SLF001
                               allow_delete=False)
    assert refused == ["repo_bound_to_other_host"], refused
    assert isinstance(judged, proxy.EgressResponse) and judged.status == 403
    assert sent == []


def test_s1a_even_past_the_route_check_the_send_point_refuses_the_ghes_token(world, monkeypatch):
    """Belt and braces: with the route check bypassed, the GHES-tagged credential is refused at
    the send point (credential_host_mismatch) and nothing is sent to github.com."""
    engine, project = world
    _token_for_repository(monkeypatch)
    proxy, egress, claims, req, refused, sent = _proxy(monkeypatch, project)
    monkeypatch.setattr(proxy.EgressProxy, "_bound_elsewhere", lambda self, claims, db, repo: False)
    with engine.begin() as db:
        judged = egress._judge(db, claims, "github", f"/{OWNER}/{REPO}.git/info/refs", req,  # noqa: SLF001
                               allow_delete=False)
    assert refused == ["credential_host_mismatch"], refused
    assert isinstance(judged, proxy.EgressResponse) and sent == []
    for url, headers in sent:
        assert "github.com" not in url or "authorization" not in {k.lower() for k in headers}


@pytest.mark.parametrize("provider,api", [("github", ""), ("github-api", ""),
                                          ("github", GHES_API), ("github-api", GHES_API)])
def test_the_upstream_request_refuses_a_credential_tagged_for_the_other_host(monkeypatch,
                                                                            provider, api):
    from boss_egress import proxy

    req = proxy.EgressRequest(method="GET", path="/x")
    wrong = github_urls.tag("t0k", github_urls.PUBLIC_API if api else GHES_API)
    right = github_urls.tag("t0k", GHES_API if api else github_urls.PUBLIC_API)
    egress = proxy.EgressProxy(engine=None)
    path = f"/{OWNER}/{REPO}.git/info/refs" if provider == "github" else f"/repos/{OWNER}/{REPO}"
    with pytest.raises(github_urls.CrossHostCredential):
        egress._upstream_request(provider, path, req, wrong, api=api)  # noqa: SLF001
    url, headers = egress._upstream_request(provider, path, req, right, api=api)  # noqa: SLF001
    assert headers["Authorization"]


def test_the_github_app_client_refuses_a_credential_for_another_host():
    from boss_engine.identity import github_app

    with pytest.raises(github_app.GitHubRefused) as refused:
        github_app._call("GET", "/repos/o/r", token=github_urls.tag("t", GHES_API))  # noqa: SLF001
    assert refused.value.code == "credential_host_mismatch"


def test_the_connect_fence_fails_closed_when_the_ghes_list_cannot_be_read(monkeypatch):
    from boss_egress import connect

    class Broken:
        def connect(self):
            raise RuntimeError("database down")

    monkeypatch.setattr(connect, "_ENGINE", [Broken()])
    monkeypatch.setattr(connect, "_LAST_GOOD", {})
    monkeypatch.setenv(connect.ALLOW_ENV, "ghe.unknown.example,allowed.example")
    assert connect.ghes_fenced("allowed.example") is True       # nothing read: every host fenced
    assert not connect.host_allowed("ghe.unknown.example")
    # A list read shortly before the outage still decides.
    import time

    monkeypatch.setattr(connect, "_LAST_GOOD", {"at": time.monotonic(),
                                                "hosts": {"ghe.conn.example"}})
    assert connect.ghes_fenced("ghe.conn.example") is True
    assert connect.ghes_fenced("allowed.example") is False
