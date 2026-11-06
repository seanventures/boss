"""GitHub's repository-access update redirects here with installation_id and setup_action, and no state.

The install callback stays state-bound. An update is a signed-in console session, or it is
unauthenticated. GitHub is never called: the installation's repository list is a stand-in.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (fixtures)
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from apps.api.tests.test_identity_integration_transport import api_auth_required  # noqa: F401

from boss_engine.identity import authn, github_setup


def _no_github(monkeypatch):
    """The callback must not reach api.github.com. A stand-in list is the installation's repos."""
    monkeypatch.setattr(
        "apps.api.app.routers.identity_github.github_setup.installation_repositories",
        lambda db: [],
    )


def test_a_signed_in_console_session_confirms_a_github_app_update(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    _no_github(monkeypatch)
    make_user(engine, "ada@example.test", role="owner", name="Ada")
    client = fresh_client()
    assert sign_in(client, "ada@example.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "424242", "setup_action": "update"},
                     follow_redirects=False)
    assert res.status_code == 303, res.text
    location = res.headers["location"]
    assert "github=app_updated" in location
    assert "github=app_failed" not in location
    assert "unauthenticated" not in res.text
    assert "424242" not in location


def test_an_anonymous_update_stays_unauthenticated(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    _no_github(monkeypatch)
    client = fresh_client()
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "424242", "setup_action": "update"},
                     follow_redirects=False)
    assert res.status_code == 401, res.text
    assert res.json() == {"error": "unauthenticated"}
    assert "app_updated" not in res.headers.get("location", "")


def test_a_missing_cookie_is_not_accepted_as_the_network_bridge(world, engine, api_auth_required, monkeypatch):
    """No cookie is anonymous even when the network bridge would otherwise name an operator."""
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    world.network_bridge = True
    monkeypatch.setattr("apps.api.app.identity_guard._legacy_method", lambda: "network")
    _no_github(monkeypatch)
    client = fresh_client()
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "424242", "setup_action": "update"},
                     follow_redirects=False)
    assert res.status_code == 401, res.text
    assert res.json() == {"error": "unauthenticated"}
    assert "app_updated" not in res.headers.get("location", "")


def test_a_forged_session_cookie_stays_unauthenticated(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    _no_github(monkeypatch)
    client = fresh_client()
    client.cookies.set(authn.SESSION_COOKIE, "bss_forged")
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "424242", "setup_action": "update"},
                     follow_redirects=False)
    assert res.status_code == 401, res.text
    assert res.json() == {"error": "unauthenticated"}


_PROJECT = "northpeak-bank-b"
_TOKEN = "ghs_do_not_log_this_token"


def _seed_repos(engine) -> None:
    """acme/app is the default repo. acme/billing and acme/web have components. acme/notes does not."""
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, "
            "status, created_by) VALUES ('ghc_cb', :p, 'app', 'github_app', 'secret:none', 'active', 'test')"),
            {"p": _PROJECT})
        for bid, full, role in (
            ("ghb_app", "acme/app", "primary"),
            ("ghb_billing", "acme/billing", "source"),
            ("ghb_web", "acme/web", "source"),
            ("ghb_notes", "acme/notes", "source"),
        ):
            owner, name = full.split("/")
            db.execute(text(
                "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, "
                "role, created_by) VALUES (:i, :p, 'ghc_cb', :o, :n, :f, :r, 'test')"),
                {"i": bid, "p": _PROJECT, "o": owner, "n": name, "f": full, "r": role})
        for cid, bid, app in (("cmp_billing", "ghb_billing", "billing"), ("cmp_web", "ghb_web", "web")):
            db.execute(text(
                "INSERT INTO app_components (id, project_id, app_key, app_name, name, binding_id) "
                "VALUES (:i, :p, :a, :a, 'api', :b)"),
                {"i": cid, "p": _PROJECT, "a": app, "b": bid})


def test_losing_an_in_use_repo_is_named_and_nothing_else_is(world, engine, api_auth_required, monkeypatch, caplog):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    monkeypatch.setattr(
        "apps.api.app.routers.identity_github.github_setup.installation_repositories",
        lambda db: [{"full_name": "acme/web", "token": _TOKEN}],
    )
    _seed_repos(engine)
    make_user(engine, "bea@example.test", role="owner", name="Bea")
    client = fresh_client()
    assert sign_in(client, "bea@example.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "999001", "setup_action": "update"},
                     follow_redirects=False)
    assert res.status_code == 303, res.text
    query = parse_qs(urlparse(res.headers["location"]).query)
    assert query["github"] == ["app_updated"]
    assert query["lost"] == ["acme/app,acme/billing"]
    assert "acme/notes" not in res.headers["location"]
    assert "acme/web" not in query.get("lost", [""])[0]
    assert _TOKEN not in res.headers["location"] and _TOKEN not in res.text and _TOKEN not in caplog.text
    with engine.connect() as db:
        assert db.execute(text("SELECT installation_id FROM identity_github_app")).first() is None


def test_a_github_miss_still_confirms_without_a_warning(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")

    def unavailable(db):
        raise github_setup.GitHubSetupError("repositories_unavailable", 502)

    monkeypatch.setattr(
        "apps.api.app.routers.identity_github.github_setup.installation_repositories", unavailable)
    make_user(engine, "cy@example.test", role="owner", name="Cy")
    client = fresh_client()
    assert sign_in(client, "cy@example.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "424242", "setup_action": "update"},
                     follow_redirects=False)
    assert res.status_code == 303, res.text
    assert "github=app_updated" in res.headers["location"]
    assert "lost=" not in res.headers["location"]
    assert "app_failed" not in res.headers["location"]


# ── GitHub's own return carrying a state that no longer opens the install path (2026-11-06) ──
# Changing the App's repository access at github.com/settings/installations sent the operator
# back with a state that had expired or was used, and Setup said "The link expired or was used".


def _signed_in_owner(engine, email: str):
    make_user(engine, email, role="owner", name="Op")
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def test_a_stale_state_on_githubs_return_says_access_updated_with_the_repos(world, engine, api_auth_required,
                                                                               monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    monkeypatch.setattr(
        "apps.api.app.routers.identity_github.github_setup.installation_repositories",
        lambda db: [{"full_name": "paramg/helpdesk-ai-demo"}, {"full_name": "paramg/boss"}])
    asked: list[str] = []
    monkeypatch.setattr("apps.api.app.routers.identity_github.github_setup.confirm_installation",
                        lambda **kw: asked.append(kw["installation_id"]) or "paramg")
    # The signing key this install would check the state with (no vault key in the test DB).
    monkeypatch.setattr("boss_engine.identity.github_app._state_mac", lambda payload: "the-real-mac")
    client = _signed_in_owner(engine, "dee@example.test")
    for state in ("not-a-signed-state", "eyJwIjoxfQ.forged-mac"):
        res = client.get("/v1/identity/github/app/installed",
                         params={"installation_id": "424242", "setup_action": "update", "state": state},
                         follow_redirects=False)
        assert res.status_code == 303, res.text
        query = parse_qs(urlparse(res.headers["location"]).query)
        assert query["github"] == ["app_updated"], res.headers["location"]
        assert "reason" not in query
        assert query["repos"] == ["paramg/helpdesk-ai-demo,paramg/boss"]
        assert query["repo_count"] == ["2"]
    assert asked == ["424242", "424242"]


def test_an_installation_the_apps_api_does_not_know_is_refused(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    _no_github(monkeypatch)

    def unknown(**kw):
        raise github_setup.GitHubSetupError("not_this_apps_installation", 403)

    monkeypatch.setattr("apps.api.app.routers.identity_github.github_setup.confirm_installation", unknown)
    client = _signed_in_owner(engine, "eve@example.test")
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "777", "setup_action": "update", "state": "stale"},
                     follow_redirects=False)
    assert res.status_code == 303, res.text
    query = parse_qs(urlparse(res.headers["location"]).query)
    assert query["github"] == ["app_failed"]
    assert query["reason"] == ["not_this_apps_installation"]


def test_a_stale_state_without_a_session_stays_unauthenticated(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    _no_github(monkeypatch)
    client = fresh_client()
    res = client.get("/v1/identity/github/app/installed",
                     params={"installation_id": "424242", "setup_action": "update", "state": "stale"},
                     follow_redirects=False)
    assert res.status_code == 401, res.text
    assert res.json() == {"error": "unauthenticated"}
