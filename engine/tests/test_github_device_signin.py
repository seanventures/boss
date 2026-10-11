"""Sign in with GitHub (identity/github_device.py): OAuth device flow against ONE published App.

Proved against the e2e fake GitHub's device flow (tests/e2e/stubs/github_device_fake.py):
  - the happy path, and GitHub's slow_down, expired_token and access_denied answers;
  - the poll handle is sealed and bound to the person who started it;
  - the sign-in AUTO-LINKS "as me on GitHub" (a run whose grant says so uses it, through egress);
  - the repo picker is scoped to what GitHub says THIS person can push to where the App is installed;
  - "Install BOSS on <org>" is named for every account of theirs without an installation;
  - nothing is widened: no grant appears or grows, no org token is made, nobody else is lent it,
    and a repository the person cannot reach is still refused.
"""
from __future__ import annotations

import base64
import importlib.util
import json

import pytest
from sqlalchemy import text

from boss_engine.identity import github_app, github_device, github_setup, github_urls, vault

from ._identity_agents_support import BUILDER, ROOT, FakeGitHub, add_person, build_env, start

_spec = importlib.util.spec_from_file_location("_device_fake", ROOT / "tests/e2e/stubs/github_device_fake.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DeviceFlowFake = _mod.DeviceFlowFake

import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from apps.egress.boss_egress.proxy import EgressProxy, EgressRequest, EgressResponse  # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    e = build_env(monkeypatch)
    for name in ("BOSS_GITHUB_APP_ID", "BOSS_GITHUB_APP_INSTALLATION_ID", "BOSS_GITHUB_APP_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOSS_GITHUB_APP_CLIENT_ID", "Iv23-boss-published")
    monkeypatch.setenv("BOSS_GITHUB_APP_SLUG", "boss-hq")
    monkeypatch.setenv("BOSS_GITHUB_API_URL", "https://api.github.test")
    monkeypatch.setenv(github_urls.WEB_BASE_ENV, "https://github.test")
    e.hub, e.gh = FakeGitHub(), DeviceFlowFake()
    with e.engine.begin() as c:
        add_person(c, e.bob, name="Bob")             # a second person of the same org
    e.world.grant(e.bob, set(BUILDER) | {"delegation:grant"}, e.project)
    e.world.grant(e.bob, {"delegation:grant"})

    def transport(method, url, headers, body):
        got = e.gh.handle(method, url, headers, body or b"")
        if got is not None:
            return github_app.Response(got[0], json.dumps(got[1]).encode())
        return e.hub(method, url, headers, body)

    monkeypatch.setattr(github_app, "TRANSPORT", transport)
    monkeypatch.setitem(vault.REFRESHERS, "github", github_app._oauth_refresh)
    return e


def _poll(env, handle, person=None, **kw):
    with env.db() as c:
        return github_device.poll(person_id=person or env.alice, org_id="org_boss", handle=handle, db=c, **kw)


def sign_in(env, person=None):
    started = github_device.start(person or env.alice)
    assert _poll(env, started["handle"], person)["state"] == "pending"
    env.gh.approve(started["user_code"])
    out = _poll(env, started["handle"], person)
    assert out["state"] == "connected", out
    return started, out


def _token_of(env, person):
    with env.db() as c:
        return vault.token_for_access_check(person, provider="github", db=c)


# ---- the device flow --------------------------------------------------------------------------------------

def test_the_happy_path_needs_no_callback_and_stores_the_persons_own_connection(env):
    started = github_device.start(env.alice)
    assert started["user_code"].startswith("BOSS-")
    assert started["verification_uri"] == "https://github.com/login/device"
    assert started["interval"] == 5 and started["expires_in"] == 900
    # GitHub's device code claims the token: it never reaches the browser in the clear.
    assert "dc_" not in started["handle"] and b"dc_" not in base64.urlsafe_b64decode(started["handle"] + "==")
    assert _poll(env, started["handle"]) == {"state": "pending", "interval": 5}
    env.gh.approve(started["user_code"])
    out = _poll(env, started["handle"])
    assert out["state"] == "connected" and out["login"] == "octo-dev"
    with env.db() as c:
        me = github_device.status(env.alice, db=c)["me"]
        row = c.execute(text("SELECT kind, provider, owner_principal_id FROM identity_vault_items WHERE id = :i"),
                        {"i": out["item"]}).mappings().one()
    assert me["login"] == "octo-dev" and me["via_signin"] is True
    assert dict(row) == {"kind": "oauth_user", "provider": "github", "owner_principal_id": env.alice}
    # used once: GitHub refuses the same device code again
    assert _poll(env, started["handle"])["state"] == "failed"
    # no client secret and no callback anywhere in what BOSS sent
    sent = [p for _, p in env.gh.calls]
    assert sent[:1] == ["/login/device/code"] and "/login/oauth/access_token" in sent


def test_slow_down_lengthens_the_wait(env):
    started = github_device.start(env.alice)
    env.gh.slow_down_next = True
    out = _poll(env, started["handle"])
    assert out == {"state": "pending", "interval": 10, "slow_down": True}


def test_an_expired_code_says_expired_and_stores_nothing(env):
    started = github_device.start(env.alice)
    env.gh.expire(started["user_code"])
    assert _poll(env, started["handle"]) == {"state": "expired", "error": "expired_token"}
    # and BOSS's own clock: past the code's life it does not even ask GitHub
    before = len(env.gh.calls)
    assert _poll(env, started["handle"], now=10**10)["state"] == "expired" and len(env.gh.calls) == before
    assert _token_of(env, env.alice) is None


def test_a_denied_request_says_denied_and_stores_nothing(env):
    started = github_device.start(env.alice)
    env.gh.deny(started["user_code"])
    assert _poll(env, started["handle"]) == {"state": "denied", "error": "access_denied"}
    assert _token_of(env, env.alice) is None


def test_the_handle_is_bound_to_the_person_who_started_it(env):
    started = github_device.start(env.alice)
    env.gh.approve(started["user_code"])
    with pytest.raises(github_app.GitHubRefused) as e:
        _poll(env, started["handle"], env.bob)
    assert e.value.code == "not_your_signin"
    with pytest.raises(github_app.GitHubRefused) as e:
        _poll(env, "A" * 40)
    assert e.value.code == "bad_handle"
    assert _token_of(env, env.bob) is None


def test_unset_config_means_the_advanced_path_and_a_disabled_device_flow_says_so(env, monkeypatch):
    env.gh.device_flow_enabled = False
    with pytest.raises(github_app.GitHubRefused) as e:
        github_device.start(env.alice)
    assert e.value.code == "device_flow_disabled"
    monkeypatch.delenv("BOSS_GITHUB_APP_CLIENT_ID")
    with env.db() as c:
        assert github_device.status(env.alice, db=c)["configured"] is False
    with pytest.raises(github_app.GitHubRefused) as e:
        github_device.start(env.alice)
    assert e.value.code == "signin_not_configured" and e.value.status == 409


# ---- auto-link "as me" ----------------------------------------------------------------------------------------

class Upstream:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return EgressResponse(200, {"content-type": "application/json"}, b'{"ok":true}')


def _clone(env, *, gh=None, repos=("acme/web",), run="run_ct:dev1", person=None):
    g = env.grant(agents=(env.forge,), children=(env.vera,), repos=repos, gh=gh, person=person)
    shim = start(env, g.id, agent=env.forge, repos=repos, run=run, person=person,
                 github_needs={"contents": "write", "pull_requests": "write"})
    up = Upstream()
    auth = "Basic " + base64.b64encode(f"boss-egress:{shim.egress_credential}".encode()).decode()
    resp = EgressProxy(env.engine, upstream=up).handle(EgressRequest(
        "GET", f"/github/{repos[0]}.git/info/refs", "service=git-upload-pack", {"authorization": auth}, b""))
    injected = (base64.b64decode(up.calls[-1][2]["Authorization"].split()[1]).decode().split(":", 1)[1]
                if up.calls else None)
    return resp, injected


def test_signing_in_is_the_as_me_link_no_second_trip(env):
    sign_in(env)
    token = _token_of(env, env.alice)
    assert token and token.startswith("ghu_fake")
    env.hub.user_repo_perms[(token, "acme/web")] = {"pull": True, "push": True}
    resp, injected = _clone(env, gh={"contents": "write", "pull_requests": "write", "as_user": True})
    assert resp.status == 200 and injected == token
    # signing in again retires the older link: one GitHub identity per person
    sign_in(env)
    with env.db() as c:
        active = c.execute(text("SELECT count(*) FROM identity_vault_items WHERE owner_principal_id = :p "
                                "AND kind = 'oauth_user' AND status = 'active'"), {"p": env.alice}).scalar()
    assert active == 1


# ---- the repo picker and install-needed detection ------------------------------------------------------------

def test_the_picker_lists_only_repos_this_person_can_push_to_where_boss_is_installed(env):
    sign_in(env)
    with env.db() as c:
        found = github_device.reach(env.alice, db=c)
        assert [r["full_name"] for r in found["repos"]] == ["acme/web"]
        assert found["read_only"] == 1                           # acme/api: pull only
        assert github_device.reachable(env.alice, "ACME/web", db=c)
        assert not github_device.reachable(env.alice, "acme/secret-infra", db=c)   # installed, not theirs
        assert not github_device.reachable(env.alice, "someone-else/dotfiles", db=c)  # not their account
    assert [i["account"] for i in found["installed_on"]] == ["acme"]


def test_install_needed_names_each_account_without_the_app_with_githubs_page_for_it(env):
    sign_in(env)
    with env.db() as c:
        found = github_device.reach(env.alice, db=c)
    assert found["install_needed"] == [
        {"account": "octo-dev", "type": "User",
         "install_url": "https://github.test/apps/boss-hq/installations/new/permissions?target_id=501"},
        {"account": "globex", "type": "Organization",
         "install_url": "https://github.test/apps/boss-hq/installations/new/permissions?target_id=9002"},
    ]
    assert found["install_url"] == "https://github.test/apps/boss-hq/installations/new"
    # once installed there, it is no longer asked for
    env.gh.installations[73] = {"account": "globex", "type": "Organization", "selection": "all", "repos": []}
    with env.db() as c:
        assert [n["account"] for n in github_device.reach(env.alice, db=c)["install_needed"]] == ["octo-dev"]


def test_the_picker_refuses_a_person_who_has_not_signed_in(env):
    with env.db() as c, pytest.raises(github_app.GitHubRefused) as e:
        github_device.reach(env.bob, db=c)
    assert e.value.code == "not_signed_in"


# ---- no widening ------------------------------------------------------------------------------------------------

def test_signing_in_widens_nothing(env):
    with env.db() as c:
        grants_before = c.execute(text("SELECT count(*) FROM identity_delegation_grants")).scalar()
    sign_in(env)
    token = _token_of(env, env.alice)
    env.hub.user_repo_perms[(token, "acme/web")] = {"pull": True, "push": True}
    with env.db() as c:
        assert c.execute(text("SELECT count(*) FROM identity_delegation_grants")).scalar() == grants_before
        assert github_setup.org_token(c, "org_boss") is None          # never the org's credential
        assert github_setup.mode(c, "org_boss")["mode"] == "as_me"
    # a run whose grant does not say "as me" is not lent the person's sign-in
    resp, injected = _clone(env, run="run_ct:dev2")
    assert resp.status == 403 and b"github_not_connected" in resp.body and injected is None
    # nor anybody else: Bob never signed in, so a grant of his saying "as me" is refused outright
    from boss_engine.identity.grants import GrantRefused

    as_me = {"contents": "write", "pull_requests": "write", "as_user": True}
    with pytest.raises(GrantRefused) as e:
        _clone(env, gh=as_me, run="run_ct:dev3", person=env.bob)
    assert e.value.code == "github_access_lacking"
    # and the person's own "as me" grant never reaches more than the person does on GitHub
    with pytest.raises(GrantRefused) as e:
        _clone(env, gh=as_me, repos=("acme/secret-infra",), run="run_ct:dev4")
    assert e.value.code == "github_access_lacking"


def test_a_picked_repo_resolves_to_that_persons_sign_in_only(env):
    from boss_engine.services.secret_backends import IdentityGitHubBackend, SecretUnavailable

    sign_in(env)
    assert IdentityGitHubBackend().get(f"user:{env.alice}") == _token_of(env, env.alice)
    with pytest.raises(SecretUnavailable):
        IdentityGitHubBackend().get(f"user:{env.bob}")
