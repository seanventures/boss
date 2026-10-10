"""Setup › Repo: the three ways a build reaches GitHub, each proved THROUGH EGRESS with a stub GitHub.

  (a) a GitHub App from a manifest (recommended) -> per-run installation tokens;
  (c) a fine-grained token held in the vault as an org connection, only for the repos it was given;
  (b) "act as me": a person's own connection, only when the run's grant says so;
plus the BOSS_GITHUB_TOKEN migration (under enforce the env copy is not read again) and the posture
check that says which mode is active. The sandbox never holds any of these credentials.
"""
from __future__ import annotations

import base64
import json
import sys

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import github_app, github_setup, posture, vault

from ._identity_agents_support import ROOT, FakeGitHub, build_env, link_github, start

sys.path.insert(0, str(ROOT))
from apps.egress.boss_egress.proxy import EgressProxy, EgressRequest, EgressResponse  # noqa: E402

PAT = "github_pat_ORGTOKEN0000000000000000000000"


class SetupGitHub(FakeGitHub):
    """FakeGitHub plus the App-manifest conversion, the App's installations and token checks."""

    def __init__(self):
        super().__init__()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()
        self.installs = {"4242": "acme"}
        self.token_repos = {PAT: {"acme/web"}}
        # GitHub's own answers about accounts: a person or an organisation (github_urls).
        self.users = {"acme": "Organization", "paramg": "User"}
        self.owner = "acme"
        # What the App asks for now, and what the installation ACCEPTED (they differ until the
        # installation accepts a new permission).
        self.app_perms = dict(github_setup.APP_PERMISSIONS)
        self.granted = dict(github_setup.APP_PERMISSIONS)
        self.reachable = ["acme/web"]

    def __call__(self, method, url, headers, body):
        from boss_engine.identity.github_app import Response

        auth = headers.get("Authorization", "")
        if method == "POST" and "/app-manifests/" in url and url.endswith("/conversions"):
            self.calls.append((method, url, dict(headers), body or b""))
            if "/app-manifests/good-code/" not in url:
                return Response(404, b"{}")
            return Response(201, json.dumps({"id": 777, "slug": "boss-test", "pem": self.pem, "client_id": "Iv1.app",
                                             "client_secret": "app-client-secret",
                                             "owner": {"login": self.owner, "type": self.users[self.owner]},
                                             "html_url": "https://github.test/apps/boss-test"}).encode())
        if method == "GET" and url.endswith("/app/installations"):
            self.calls.append((method, url, dict(headers), body or b""))
            return Response(200, json.dumps([{"id": int(i), "account": {"login": a, "type": self.users.get(a)},
                                              "repository_selection": "all"}
                                             for i, a in self.installs.items()]).encode())
        if method == "GET" and "/app/installations/" in url:
            self.calls.append((method, url, dict(headers), body or b""))
            iid = url.rsplit("/", 1)[1]
            if iid not in self.installs or not auth.startswith("Bearer ey"):
                return Response(404, b"{}")
            a = self.installs[iid]
            return Response(200, json.dumps({"id": int(iid), "account": {"login": a, "type": self.users.get(a)},
                                             "permissions": self.granted,
                                             "repository_selection": "selected"}).encode())
        if method == "GET" and url.endswith("/app"):
            self.calls.append((method, url, dict(headers), body or b""))
            return Response(200, json.dumps({"id": 777, "slug": "boss-test", "permissions": self.app_perms,
                                             "owner": {"login": self.owner,
                                                       "type": self.users[self.owner]}}).encode())
        if method == "GET" and "/users/" in url:
            self.calls.append((method, url, dict(headers), body or b""))
            login = url.rsplit("/", 1)[1]
            if login not in self.users:
                return Response(404, b"{}")
            return Response(200, json.dumps({"login": login, "type": self.users[login]}).encode())
        if method == "GET" and "/installation/repositories" in url:
            self.calls.append((method, url, dict(headers), body or b""))
            return Response(200, json.dumps({"repositories": [
                {"full_name": r, "private": True, "owner": {"login": r.split("/")[0],
                                                            "type": self.users.get(r.split("/")[0])}}
                for r in self.reachable]}).encode())
        if method == "GET" and url.endswith("/user"):
            self.calls.append((method, url, dict(headers), body or b""))
            ok = auth.split(" ", 1)[-1] in self.token_repos
            return Response(200 if ok else 401, json.dumps({"login": "octo-admin"} if ok else {}).encode())
        tok = auth.split(" ", 1)[-1]
        if method == "GET" and "/repos/" in url and tok in self.token_repos and url.count("/") == 5:
            self.calls.append((method, url, dict(headers), body or b""))
            repo = url.split("/repos/", 1)[1]
            return Response(200 if repo in self.token_repos[tok] else 404, b"{}")
        return super().__call__(method, url, headers, body)


class Upstream:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return EgressResponse(200, {"content-type": "application/json"}, b'{"ok":true}')


@pytest.fixture()
def env(monkeypatch):
    e = build_env(monkeypatch)
    for name in ("BOSS_GITHUB_APP_ID", "BOSS_GITHUB_APP_INSTALLATION_ID", "BOSS_GITHUB_APP_CLIENT_ID",
                 "BOSS_GITHUB_APP_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOSS_GITHUB_API_URL", "https://api.github.test")
    monkeypatch.setenv(github_setup.WEB_BASE_ENV, "https://github.test")
    e.hub = SetupGitHub()
    e.mp = monkeypatch
    monkeypatch.setattr(github_app, "TRANSPORT", e.hub)
    monkeypatch.setitem(vault.REFRESHERS, "github", github_app._oauth_refresh)
    return e


def _run(env, *, repos=("acme/web",), gh=None, run="run_ct:gh1"):
    g = env.grant(agents=(env.forge,), children=(env.vera,), repos=repos, gh=gh)
    shim = start(env, g.id, agent=env.forge, repos=repos, run=run,
                 github_needs={"contents": "write", "pull_requests": "write"})
    up = Upstream()
    return shim, up, EgressProxy(env.engine, upstream=up)


def _clone(proxy, shim, repo="acme/web"):
    auth = "Basic " + base64.b64encode(f"boss-egress:{shim.egress_credential}".encode()).decode()
    return proxy.handle(EgressRequest("GET", f"/github/{repo}.git/info/refs", "service=git-upload-pack",
                                      {"authorization": auth}, b""))


def _injected(up) -> str:
    return base64.b64decode(up.calls[-1][2]["Authorization"].split()[1]).decode().split(":", 1)[1]


def _connect_app(env):
    with env.db() as c:
        start_ = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice)
        out = github_setup.complete_manifest(code="good-code", state=start_["state"], org_id="org_boss", db=c)
    state = out["install_url"].split("state=", 1)[1]
    with env.db() as c:
        github_setup.complete_install(installation_id="4242", state=state, org_id="org_boss", db=c)
    return start_, out


# ---- (a) the GitHub App from a manifest ---------------------------------------------------------------------

def test_the_manifest_asks_for_least_privilege_and_returns_to_this_boss(env):
    m = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice, owner="acme")
    assert m["post_url"].startswith("https://github.test/organizations/acme/settings/apps/new?state=")
    body = m["manifest"]
    assert body["redirect_url"] == "https://boss.acme.test/v1/identity/github/app/created"
    assert body["setup_url"] == "https://boss.acme.test/v1/identity/github/app/installed"
    assert body["public"] is False and "administration" not in body["default_permissions"]
    assert "workflows" not in body["default_permissions"]
    with pytest.raises(github_setup.GitHubSetupError) as e:
        github_setup.manifest(origin="", person_id=env.alice)
    assert e.value.code == "public_url_required"


def test_the_manifest_asks_for_workflows_only_when_a_repository_opted_in(env):
    """CI capability: "Builds may change CI workflows" is opt-in per repository. Off (the
    default, above) the App never asks for workflows; with the opt-in it asks for write, and an
    App created earlier is sent to its own permissions page to accept it."""
    m = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice, owner="acme",
                              allow_workflows=True)
    perms = m["manifest"]["default_permissions"]
    assert perms["workflows"] == "write"
    assert {k: v for k, v in perms.items() if k != "workflows"} == github_setup.APP_PERMISSIONS
    assert "workflows" not in github_setup.APP_PERMISSIONS
    assert "workflows" not in github_setup.app_permissions(False)
    _connect_app(env)
    with env.db() as c:
        link = github_setup.workflows_permission_link(c, "org_boss")
    assert link["permissions_url"] == (
        "https://github.test/organizations/acme/settings/apps/boss-test/permissions")
    assert link["installation_url"] == (
        "https://github.test/organizations/acme/settings/installations/4242")
    assert "Accept new permissions" in link["note"]


def test_a_github_app_from_a_manifest_mints_per_run_tokens_through_egress(env):
    _connect_app(env)
    with env.db() as c:
        assert github_setup.mode(c, "org_boss")["mode"] == "app"
        row = c.execute(text("SELECT app_id, installation_id, account_login FROM identity_github_app")).one()
        assert tuple(row) == ("777", "4242", "acme")
        kinds = {r[0] for r in c.execute(text("SELECT kind FROM identity_vault_items WHERE status = 'active'"))}
        assert {"app_private_key", "oidc_client_secret"} <= kinds
        assert env.hub.pem not in json.dumps([dict(r._mapping) for r in c.execute(
            text("SELECT * FROM identity_github_app"))], default=str)        # the key lives only in the vault
    shim, up, proxy = _run(env)
    assert _clone(proxy, shim).status == 200
    assert _injected(up) == env.hub.issued[0]                                  # an installation token
    mint = next(u for m, u, _, _ in env.hub.calls if u.endswith("/access_tokens"))
    assert "/app/installations/4242/access_tokens" in mint
    assert shim.egress_credential not in json.dumps(up.calls[-1][2])


def test_the_manifest_and_install_states_are_signed_single_use_and_purpose_bound(env):
    start_, out = _connect_app(env)
    with env.db() as c, pytest.raises(github_setup.GitHubSetupError) as e:
        github_setup.complete_manifest(code="good-code", state=start_["state"], org_id="org_boss", db=c)
    assert e.value.code == "state_replayed"
    with env.db() as c, pytest.raises(github_setup.GitHubSetupError) as e:   # install state != manifest state
        github_setup.complete_manifest(code="good-code", state=out["install_url"].split("state=")[1],
                                       org_id="org_boss", db=c)
    assert e.value.code == "bad_state"
    with env.db() as c, pytest.raises(github_setup.GitHubSetupError) as e:
        github_setup.complete_install(installation_id="4242", state=None, org_id="org_boss", db=c)
    assert e.value.code == "bad_state"
    fresh = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice)["state"]
    with env.db() as c, pytest.raises(github_setup.GitHubSetupError) as e:
        github_setup.complete_manifest(code="../../user", state=fresh, org_id="org_boss", db=c)
    assert e.value.code == "bad_code"


# ---- the account type decides every settings link (identity/github_urls) ------------------------------------

def test_the_apps_name_is_unique_per_install_and_fits_githubs_limit():
    a, b = github_setup.app_name("localhost:3000"), github_setup.app_name("localhost:3000")
    assert a != b and a.startswith("BOSS localhost-3000 ") and len(a) <= 34
    long = github_setup.app_name("a-very-long-hostname.example.internal.corp:8443")
    assert len(long) <= 34 and long.startswith("BOSS a-very-long")


def test_a_personal_owner_creates_the_app_on_the_personal_page_never_an_organizations_one(env):
    """paramg is a person: /organizations/paramg/... is a 404 at GitHub. GitHub says which it is."""
    personal = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice, owner="paramg")
    assert personal["post_url"].startswith("https://github.test/settings/apps/new?state=")
    assert personal["owner"] == {"login": "paramg", "type": "User"}
    org = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice, owner="acme")
    assert org["post_url"].startswith("https://github.test/organizations/acme/settings/apps/new?state=")
    blank = github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice)
    assert blank["post_url"].startswith("https://github.test/settings/apps/new?state=")
    with pytest.raises(github_setup.GitHubSetupError) as e:
        github_setup.manifest(origin="https://boss.acme.test", person_id=env.alice, owner="nobody-here")
    assert e.value.code == "github_account_not_found" and "nobody-here" in e.value.body()["message"]


@pytest.mark.parametrize("kind,login,perm_root", [
    ("User", "paramg", "https://github.test/settings"),
    ("Organization", "acme", "https://github.test/organizations/acme/settings"),
])
def test_every_app_link_follows_the_account_type_github_reported(env, kind, login, perm_root):
    env.hub.owner = login
    env.hub.installs = {"4242": login}
    _connect_app(env)
    with env.db() as c:
        row = c.execute(text("SELECT owner_login, owner_type, account_login, account_type "
                             "FROM identity_github_app")).one()
        assert tuple(row) == (login, kind, login, kind)
        m = github_setup.mode(c, "org_boss")
        link = github_setup.workflows_permission_link(c, "org_boss")
    assert m["app"]["links"]["permissions"] == f"{perm_root}/apps/boss-test/permissions"
    assert m["app"]["links"]["installation"] == f"{perm_root}/installations/4242"
    assert m["app"]["links"]["install"] == "https://github.test/apps/boss-test/installations/new"
    assert link["permissions_url"] == f"{perm_root}/apps/boss-test/permissions"
    assert link["installation_url"] == f"{perm_root}/installations/4242"


# ---- verify, don't trust: Setup shows what GitHub actually granted ---------------------------------------------

def test_verify_reads_the_installations_real_permissions_and_repositories(env):
    env.hub.owner = "paramg"
    env.hub.installs = {"4242": "paramg"}
    env.hub.reachable = ["paramg/boss-sandbox"]
    _connect_app(env)
    registered = ["paramg/boss-sandbox", "paramg/not-installed"]
    env.mp.setattr(github_setup, "_registered_repos", lambda db: registered)
    with env.db() as c:
        out = github_setup.verify(c, "org_boss")
    assert out["state"] == "ok" and out["account"] == {"login": "paramg", "type": "User"}
    assert all(p["state"] == "accepted" for p in out["permissions"])
    repos = {r["full_name"]: r for r in out["repos"]}
    assert repos["paramg/boss-sandbox"]["installed"] is True
    assert repos["paramg/not-installed"]["installed"] is False
    assert repos["paramg/not-installed"]["fix_url"] == "https://github.test/settings/installations/4242"
    assert out["ok"] is False
    with env.db() as c:
        cached = github_setup.mode(c, "org_boss")["app"]["verified"]
    assert cached["repos"] == out["repos"]                       # kept: the page needs no GitHub call


def test_verify_says_workflows_is_waiting_until_the_installation_accepts_it(env):
    _connect_app(env)
    from boss_engine.services import ci_gate

    env.hub.app_perms = {**github_setup.APP_PERMISSIONS, "workflows": "write"}   # the App asks now
    import unittest.mock as mock

    with mock.patch.object(ci_gate, "any_repo_allows_workflows", return_value=True), env.db() as c:
        waiting = {p["name"]: p for p in github_setup.verify(c, "org_boss")["permissions"]}
    assert waiting["workflows"]["state"] == "waiting"
    assert waiting["workflows"]["fix_url"] == "https://github.test/organizations/acme/settings/installations/4242"
    env.hub.app_perms = dict(github_setup.APP_PERMISSIONS)                      # the App never asked
    with mock.patch.object(ci_gate, "any_repo_allows_workflows", return_value=True), env.db() as c:
        lacks = {p["name"]: p for p in github_setup.verify(c, "org_boss")["permissions"]}
    assert lacks["workflows"]["state"] == "app_lacks"
    assert lacks["workflows"]["fix_url"] == "https://github.test/organizations/acme/settings/apps/boss-test/permissions"
    env.hub.granted = {**github_setup.APP_PERMISSIONS, "workflows": "write"}    # accepted on GitHub
    with mock.patch.object(ci_gate, "any_repo_allows_workflows", return_value=True), env.db() as c:
        out = github_setup.verify(c, "org_boss")
        view = ci_gate._verified_workflows(github_setup.app_config(c, "org_boss"))
    assert {p["name"]: p["state"] for p in out["permissions"]}["workflows"] == "accepted"
    assert view["state"] == "accepted"


def test_verify_says_when_the_app_was_uninstalled_on_github(env):
    _connect_app(env)
    env.hub.installs = {}
    with env.db() as c:
        out = github_setup.verify(c, "org_boss")
    assert out["state"] == "installation_gone" and out["ok"] is False
    assert out["links"]["install"] == "https://github.test/apps/boss-test/installations/new"


def test_the_installation_picker_only_accepts_this_apps_installations(env):
    _connect_app(env)
    with env.db() as c:
        assert github_setup.installations(db=c, org_id="org_boss") == [
            {"id": "4242", "account": "acme", "account_type": "Organization", "repository_selection": "all"}]
        with pytest.raises(github_setup.GitHubSetupError) as e:
            github_setup.select_installation(installation_id="9999", org_id="org_boss", db=c, by=env.alice)
    assert e.value.code == "not_this_apps_installation"


# ---- (c) the fine-grained token, as an org connection -----------------------------------------------------

def test_an_org_token_is_used_only_via_egress_and_only_for_its_repos(env):
    with env.db() as c:
        out = github_setup.store_org_token(token=PAT, repos=["Acme/Web"], org_id="org_boss", db=c, by=env.alice)
        assert out["repos"] == ["acme/web"] and out["login"] == "octo-admin"
        assert github_setup.mode(c, "org_boss")["mode"] == "token"
    shim, up, proxy = _run(env)
    assert _clone(proxy, shim).status == 200 and _injected(up) == PAT
    other, up2, proxy2 = _run(env, repos=("acme/api",), run="run_ct:gh2")
    resp = _clone(proxy2, other, "acme/api")
    assert resp.status == 403 and b"repo_outside_org_token" in resp.body and up2.calls == []
    github_app.revoke_runs(["run_ct:gh1"])
    assert env.hub.deleted == []                     # ending a run never deletes the org's token


def test_an_org_token_github_refuses_or_that_cannot_see_a_repo_is_not_stored(env):
    with env.db() as c:
        with pytest.raises(github_setup.GitHubSetupError) as e:
            github_setup.store_org_token(token="github_pat_WRONG000000", repos=["acme/web"], org_id="org_boss",
                                         db=c, by=env.alice)
        assert e.value.code == "token_refused_by_github"
        with pytest.raises(github_setup.GitHubSetupError) as e:
            github_setup.store_org_token(token=PAT, repos=["acme/web", "acme/secret"], org_id="org_boss",
                                         db=c, by=env.alice)
        assert e.value.code == "token_cannot_see_repo"
        assert github_setup.org_token(c, "org_boss") is None


def test_the_org_token_is_not_a_personal_credential(env):
    with env.db() as c:
        item = github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c,
                                            by=env.alice)["id"]
        mine = link_github(env, env.alice, "gho_ALICE")
        with pytest.raises(vault.VaultForbidden):
            vault.org_github_token(mine, db=c)
        assert vault.org_github_token(item, db=c) == PAT


# ---- (b) as me ---------------------------------------------------------------------------------------------

def test_as_me_uses_the_persons_connection_only_when_the_grant_says_so(env):
    env.hub.user_repo_perms[("gho_ALICE", "acme/web")] = {"pull": True, "push": True}
    link_github(env, env.alice, "gho_ALICE")
    with env.db() as c:
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
    shim, up, proxy = _run(env, gh={"contents": "write", "pull_requests": "write", "as_user": True})
    assert _clone(proxy, shim).status == 200 and _injected(up) == "gho_ALICE"
    plain, up2, proxy2 = _run(env, run="run_ct:gh2")
    assert _clone(proxy2, plain).status == 200 and _injected(up2) == PAT


def test_nothing_connected_is_a_clear_refusal(env):
    shim, up, proxy = _run(env)
    resp = _clone(proxy, shim)
    assert resp.status == 403 and b"github_not_connected" in resp.body and up.calls == []
    with env.db() as c:
        check = posture.github_check("org_boss", db=c)
    assert check["status"] == "critical" and check["github_mode"] == "none"


# ---- the BOSS_GITHUB_TOKEN migration, and posture ---------------------------------------------------------

def test_moving_the_env_token_into_the_vault_stops_reading_it_under_enforce(env, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "github_token", PAT)
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", PAT)
    with env.db() as c:
        before = posture.github_check("org_boss", db=c)
        assert before["status"] == "critical" and "BOSS_GITHUB_TOKEN" in before["summary"]
        out = github_setup.migrate_env_token(repos=["acme/web"], org_id="org_boss", db=c, by=env.alice,
                                             settings=settings)
    assert out["migrated"] is True and settings.github_token is None
    import os

    assert "BOSS_GITHUB_TOKEN" not in os.environ
    with env.db() as c:
        m = github_setup.mode(c, "org_boss")
        assert m["mode"] == "token" and m["token"]["migrated"] and not m["env_token"]["present"]
        assert posture.github_check("org_boss", db=c)["github_mode"] == "token"
    monkeypatch.setattr(settings, "github_token", PAT)           # a restart with the env var still set
    assert github_setup.forget_env_token_if_moved(env.engine, settings) and settings.github_token is None
    shim, up, proxy = _run(env)
    assert _clone(proxy, shim).status == 200 and _injected(up) == PAT


def test_without_enforce_the_env_token_is_left_alone(env, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "shadow")
    monkeypatch.setattr(settings, "github_token", PAT)
    with env.db() as c:
        check = posture.github_check("org_boss", db=c)
        assert check["status"] == "warn" and check["github_mode"] == "env"
        github_setup.migrate_env_token(repos=["acme/web"], org_id="org_boss", db=c, by=env.alice, settings=settings)
    assert settings.github_token == PAT


def test_posture_prefers_the_app_and_marks_the_token_less_safe(env):
    with env.db() as c:
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
        assert posture.github_check("org_boss", db=c)["status"] == "warn"
    _connect_app(env)
    with env.db() as c:
        check = posture.github_check("org_boss", db=c)
    assert check["status"] == "ok" and check["github_mode"] == "app"


# ---- the API (Setup › Repo calls these) --------------------------------------------------------------------

def _client(env, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import identity_github

    @contextmanager
    def begin():
        with env.engine.begin() as c:
            yield c

    ctx = SimpleNamespace(principal=SimpleNamespace(id=env.alice, org_id="org_boss"))
    monkeypatch.setattr(identity_github, "begin", begin)
    monkeypatch.setattr(identity_github, "current_context", lambda request, db: ctx)
    monkeypatch.setattr(identity_github, "public_origin", lambda db, org=None: "https://boss.acme.test")
    monkeypatch.setattr(identity_github, "default_org_id", lambda db: "org_boss")
    app = FastAPI()
    app.include_router(identity_github.build_identity_github_router())
    return TestClient(app, follow_redirects=False)


def test_the_api_never_returns_a_credential_and_the_callbacks_land_back_in_setup(env, monkeypatch):
    client = _client(env, monkeypatch)
    assert client.get("/v1/identity/github").json()["mode"] == "none"
    stored = client.post("/v1/identity/github/token", json={"token": PAT, "repos": ["acme/web"]})
    assert stored.status_code == 200 and PAT not in stored.text and "id" not in stored.json()
    status = client.get("/v1/identity/github").json()
    assert status["mode"] == "token" and status["token"]["repos"] == ["acme/web"] and PAT not in json.dumps(status)
    bad = client.get("/v1/identity/github/app/created", params={"code": "good-code", "state": "forged.mac"})
    assert bad.status_code == 303 and "github=app_failed" in bad.headers["location"]
    started = client.post("/v1/identity/github/app/manifest", json={}).json()
    created = client.get("/v1/identity/github/app/created", params={"code": "good-code", "state": started["state"]})
    assert created.status_code == 303 and created.headers["location"].startswith(
        "https://github.test/apps/boss-test/installations/new?state=")
    done = client.get("/v1/identity/github/app/installed",
                      params={"installation_id": "4242", "state": created.headers["location"].split("state=")[1]})
    assert done.headers["location"] == ("https://boss.acme.test/?area=setup&focus=repository&github=app_connected")
    assert client.get("/v1/identity/github").json()["mode"] == "app"
    assert env.hub.pem not in client.get("/v1/identity/github").text
    assert client.delete("/v1/identity/github/app").json() == {"disconnected": True}
    assert client.get("/v1/identity/github").json()["mode"] == "token"


def test_every_github_setup_route_is_declared_and_changes_need_github_connect():
    from fastapi.routing import APIRoute

    from apps.api.app.routers.identity_github import build_identity_github_router
    from boss_engine.identity import access_of

    for route in build_identity_github_router().routes:
        if not isinstance(route, APIRoute):
            continue
        access = access_of(route.endpoint)
        assert access is not None, route.path
        if route.path.endswith(("/created", "/installed")):
            assert type(access).__name__ == "Public"
        elif route.methods == {"GET"} or route.path.endswith("/app/verify"):
            # a read (the status, the installation picker; verify asks GitHub what it granted and
            # keeps that answer, changing no credential) needs only github:read
            assert access.permission == "github:read", route.path
        else:
            assert access.permission == "github:connect", route.path


def test_the_worker_opens_its_pr_with_the_setup_app_or_the_org_token_for_that_repo_only(env):
    with env.db() as c:
        assert github_setup.control_plane_token(c, "acme", "web") is None
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
        assert github_setup.control_plane_token(c, "acme", "web") == PAT
        assert github_setup.control_plane_token(c, "acme", "api") is None
    _connect_app(env)
    with env.db() as c:
        tok = github_setup.control_plane_token(c, "acme", "web")
    assert tok == env.hub.issued[-1]
    mint = json.loads([b for m, u, _, b in env.hub.calls if u.endswith("/access_tokens")][-1])
    assert mint["repositories"] == ["web"] and "administration" not in mint["permissions"]
    assert "workflows" not in mint["permissions"]


def test_the_workers_pr_tokens_are_bound_to_the_build_and_revoked_when_it_ends(env):
    """Audit finding on 4637cba8: a control-plane token must not outlive the build that used it,
    and egress must never pick it up as the run's token."""
    _connect_app(env)
    github_setup.bind_control_plane_run("run_ct:pr1")
    try:
        with env.db() as c:
            tok = github_setup.control_plane_token(c, "acme", "web")
        assert github_app.cached_for_run("run_ct:pr1") is None              # never egress's token
    finally:
        github_setup.unbind_control_plane_run("run_ct:pr1")
    with env.db() as c:
        from boss_engine.identity import runs

        runs.end_run("run_ct:pr1", db=c, org_id="org_boss")
    assert tok in env.hub.deleted                                           # revoked at GitHub


def test_outside_a_build_they_are_revoked_once_due(env, monkeypatch):
    _connect_app(env)
    with env.db() as c:
        first = github_setup.control_plane_token(c, "acme", "web")
    assert first not in env.hub.deleted
    monkeypatch.setattr(github_setup, "CONTROL_PLANE_TTL_SECONDS", 0)
    with env.db() as c:
        github_setup.control_plane_token(c, "acme", "web")                  # the next mint sweeps
    assert first in env.hub.deleted
    assert github_setup.sweep_control_plane_tokens(everything=True) >= 0



def test_the_app_manifest_asks_for_no_webhook_so_a_private_install_can_create_it(env):
    # GitHub refuses a manifest whose hook URL isn't public ("Hook url is not supported because it
    # isn't reachable over the public Internet (localhost)"); BOSS subscribes to no events anyway.
    for origin in ("http://localhost:3000", "https://boss-1.tail18604a.ts.net", "https://10.0.0.5"):
        body = github_setup.manifest(origin=origin, person_id=env.alice)["manifest"]
        assert "hook_attributes" not in body and body["default_events"] == []
