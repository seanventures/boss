"""ADD A GITHUB ACCOUNT (BOSSAPP-945): the account a person types, the path BOSS picks for it, the
installation that appears, its repositories and allowing them — through the real routes, signed in
as an Owner, against a fake GitHub (github_accounts_fake.py). Nothing reaches github.com.

  lookup   a user, an organisation, a name GitHub does not know, a github.com link pasted;
  path     installed -> pick; private App and not its owner -> make_public (Advanced settings link);
           public App -> install (aimed at the account's id); the App's owner itself -> install;
  watch    the App made public, then the installation appearing (recorded beside the default);
  repos    the installation's repositories with "used by <project>";
  allow    the registry's allow + the access check, each in words; the token comes from the
           account's own installation; a repository outside it is refused.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.github_accounts_fake import demo_world
from apps.api.tests.identity_s1_world import (  # noqa: F401  (fixtures)
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from apps.api.tests.test_identity_integration_transport import api_auth_required  # noqa: F401

from boss_engine.identity import authn, github_accounts, github_app, github_setup
from boss_engine.identity.authn_support import default_org_id
from boss_engine.services import setup_checks

SLUG = "boss-localhost-ab12"


@pytest.fixture
def hub(monkeypatch, engine):
    for name in ("BOSS_GITHUB_APP_ID", "BOSS_GITHUB_APP_INSTALLATION_ID", "BOSS_GITHUB_APP_CLIENT_ID",
                 "BOSS_GITHUB_API_URL", github_setup.WEB_BASE_ENV):
        monkeypatch.delenv(name, raising=False)
    gh = demo_world(slug=SLUG)
    monkeypatch.setattr(github_app, "TRANSPORT", gh)
    monkeypatch.setattr(github_app, "app_jwt", lambda db, now=None: "eyJfake.app.jwt")
    monkeypatch.setattr(setup_checks, "default_http", gh.http)

    def ready(db, org_id=None):
        cfg = github_setup.app_config(db, org_id)
        return bool(cfg["app_id"] and cfg["installation_id"])
    monkeypatch.setattr(github_setup, "app_ready", ready)
    with engine.begin() as db:
        org = default_org_id(db)
        _clean(db, org)
        db.execute(text(
            "INSERT INTO identity_github_app (org_id, app_id, slug, client_id, installation_id, account_login, "
            "account_type, owner_login, owner_type, created_by) VALUES (:o, '777', :s, 'Iv1.x', :i, 'paramg', "
            "'User', 'paramg', 'User', 'test')"), {"o": org, "s": SLUG, "i": gh.install("paramg")})
    gh.org = org
    yield gh
    with engine.begin() as db:
        _clean(db, org)


def _clean(db, org: str) -> None:
    db.execute(text("DELETE FROM identity_github_installations WHERE org_id = :o"), {"o": org})
    db.execute(text("DELETE FROM identity_github_app WHERE org_id = :o"), {"o": org})
    db.execute(text("DELETE FROM github_repos WHERE org_id = :o"), {"o": org})
    db.execute(text("DELETE FROM github_repository_bindings WHERE id = 'ghb_ga_helpdesk'"))
    db.execute(text("DELETE FROM github_connections WHERE id = 'ghc_ga'"))


@pytest.fixture
def owner(world, engine, api_auth_required, monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    make_user(engine, "ada@example.test", role="owner", name="Ada")
    client = fresh_client()
    assert sign_in(client, "ada@example.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    client.headers.update(csrf(client))
    return client


def _lookup(client, account: str):
    return client.post("/v1/identity/github/accounts/lookup", json={"account": account})


def _project(engine, org: str) -> tuple[str, str]:
    with engine.connect() as db:
        row = db.execute(text("SELECT p.id, p.name FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                              "WHERE w.org_id = :o ORDER BY p.created_at LIMIT 1"), {"o": org}).first()
    assert row is not None, "the test database has a project in the default org"
    return str(row[0]), str(row[1])


# ---- the one input --------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, login", [
    ("kaappu", "kaappu"), ("@kaappu", "kaappu"), ("github.com/kaappu", "kaappu"),
    ("https://github.com/kaappu/helpdesk", "kaappu"), ("https://github.com/orgs/kaappu/repositories", "kaappu"),
    ("  Kaappu  ", "Kaappu"),
])
def test_a_name_or_a_github_link_names_the_account(typed, login):
    assert github_accounts.parse_account(typed) == login


@pytest.mark.parametrize("typed, words", [
    ("https://gitlab.com/kaappu", "not on GitHub"), ("https://github.com/settings/apps", "does not name an account"),
    ("kaa ppu", "is not a GitHub name"), ("", "Type a GitHub user or organisation name"),
])
def test_what_is_not_an_account_is_said_in_words(typed, words):
    with pytest.raises(github_setup.GitHubSetupError) as caught:
        github_accounts.parse_account(typed)
    assert words in caught.value.extra["message"]


# ---- lookup and the path, as data ---------------------------------------------------------------------

def test_an_organisation_with_the_app_private_asks_its_owner_to_make_it_installable(hub, owner):
    res = _lookup(owner, "kaappu")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["account"] == {"login": "kaappu", "type": "Organization", "id": "7202", "name": "Kaappu",
                              "avatar_url": "https://avatars.githubusercontent.com/u/7202?v=4",
                              "html_url": "https://github.com/kaappu", "public_repos": 14}
    assert out["path"] == "make_public"
    assert out["installation"] is None
    assert out["connection"]["covered"] is False
    app = out["app"]
    assert app["public"] is False and app["is_owner"] is False and app["owner_login"] == "paramg"
    # The App is owned by a person: its Advanced settings live under /settings, never /organizations.
    assert app["settings_url"] == f"https://github.com/settings/apps/{SLUG}/advanced"
    assert app["install_url"] == f"https://github.com/apps/{SLUG}/installations/new/permissions?target_id=7202"
    assert out["line"].endswith("It is private, so one switch first.")
    assert "GET /users/kaappu" in hub.calls and f"GET /apps/{SLUG}" in hub.calls


def test_a_pasted_link_is_looked_up_as_its_account(hub, owner):
    out = _lookup(owner, "https://github.com/kaappu/helpdesk").json()
    assert out["account"]["login"] == "kaappu" and out["account"]["type"] == "Organization"


def test_a_name_github_does_not_know_is_said_in_words(hub, owner):
    res = _lookup(owner, "nobody-here")
    assert res.status_code == 404
    assert res.json()["error"] == "github_account_not_found"
    assert res.json()["message"] == "No GitHub account called nobody-here. Check the spelling."


def test_a_person_with_the_app_public_goes_straight_to_install_aimed_at_them(hub, owner):
    hub.make_public()
    out = _lookup(owner, "octo-person").json()
    assert out["account"]["type"] == "User" and out["account"]["id"] == "8303"
    assert out["path"] == "install"
    assert out["app"]["public"] is True
    assert out["app"]["install_url"].endswith("/installations/new/permissions?target_id=8303")


def test_the_apps_owner_itself_installs_without_making_it_public(hub, owner, engine):
    with engine.begin() as db:              # the owner's own installation was removed on GitHub
        db.execute(text("UPDATE identity_github_app SET installation_id = NULL WHERE org_id = :o"), {"o": hub.org})
    hub.uninstall(next(iter(hub.installations)))
    hub.calls.clear()
    out = _lookup(owner, "paramg").json()
    assert out["path"] == "install"
    assert out["app"]["is_owner"] is True
    assert f"GET /apps/{SLUG}" not in hub.calls            # its public page does not matter to the owner


def test_an_account_already_installed_goes_straight_to_pick_repos(hub, owner, engine):
    kaappu = hub.install("kaappu")
    out = _lookup(owner, "kaappu").json()
    assert out["path"] == "pick"
    assert out["installation"]["installation_id"] == kaappu
    with engine.connect() as db:
        kept = db.execute(text("SELECT account_login, account_type, account_id FROM identity_github_installations "
                               "WHERE org_id = :o AND installation_id = :i"), {"o": hub.org, "i": kaappu}).first()
        default = db.execute(text("SELECT installation_id FROM identity_github_app WHERE org_id = :o"),
                             {"o": hub.org}).scalar()
    assert tuple(kept) == ("kaappu", "Organization", "7202")
    assert default != kaappu                  # adding an account never moves the default installation


def test_the_persons_own_connection_is_named(hub, owner, engine, monkeypatch):
    from boss_engine.identity import github_device

    monkeypatch.setattr(github_device, "my_connection", lambda pid, db: {"login": "octo-person", "status": "active"})
    out = _lookup(owner, "octo-person").json()
    assert out["connection"] == {"covered": True, "how": "signin", "words": "Your GitHub sign-in is this account."}


# ---- the watch: GitHub changes while the sheet is open ----------------------------------------------------

def test_the_watch_sees_the_app_made_public_then_the_installation_appear(hub, owner, engine):
    first = _lookup(owner, "kaappu").json()
    assert first["path"] == "make_public"
    body = {"login": "kaappu", "account_id": first["account"]["id"]}
    assert owner.post("/v1/identity/github/accounts/watch", json=body).json()["path"] == "make_public"
    hub.make_public()                                         # the owner pressed Make public on GitHub
    seen = owner.post("/v1/identity/github/accounts/watch", json=body).json()
    assert seen["path"] == "install" and seen["app"]["public"] is True
    iid = hub.install("kaappu")                               # …then Install on kaappu
    seen = owner.post("/v1/identity/github/accounts/watch", json=body).json()
    assert seen["path"] == "pick"
    assert seen["installation"]["installation_id"] == iid and seen["installation"]["new"] is True
    assert seen["installation"]["settings_url"] == f"https://github.com/organizations/kaappu/settings/installations/{iid}"
    again = owner.post("/v1/identity/github/accounts/watch", json=body).json()
    assert again["installation"]["new"] is False              # recorded once
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_github_installations WHERE org_id = :o "
                               "AND installation_id = :i"), {"o": hub.org, "i": iid}).scalar() == 1


# ---- the installation's repositories, and allowing them ------------------------------------------------------

def _seed_use(engine, project_id: str) -> None:
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, "
            "status, created_by) VALUES ('ghc_ga', :p, 'app', 'github_app', 'identity_github:app', 'active', 'test')"),
            {"p": project_id})
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, role, "
            "created_by) VALUES ('ghb_ga_helpdesk', :p, 'ghc_ga', 'kaappu', 'helpdesk', 'kaappu/helpdesk', "
            "'source', 'test')"), {"p": project_id})


def test_the_installations_repositories_say_which_project_uses_them(hub, owner, engine):
    project_id, project_name = _project(engine, hub.org)
    _seed_use(engine, project_id)
    iid = hub.install("kaappu")
    assert _lookup(owner, "kaappu").json()["path"] == "pick"
    res = owner.get(f"/v1/identity/github/accounts/installations/{iid}/repos")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["account"] == "kaappu"
    by = {r["full_name"]: r for r in out["repos"]}
    assert sorted(by) == ["kaappu/helpdesk", "kaappu/infra-scripts", "kaappu/website"]
    assert by["kaappu/helpdesk"]["used_by"] == [project_name]
    assert by["kaappu/website"]["used_by"] == [] and by["kaappu/website"]["state"] == "not_allowed"
    assert "ghs_fake" not in res.text                         # no token ever leaves the API
    assert owner.get("/v1/identity/github/accounts/installations/123/repos").status_code == 404


def test_allowing_runs_the_access_check_on_the_accounts_own_installation(hub, owner, engine):
    project_id, _ = _project(engine, hub.org)
    iid = hub.install("kaappu")
    hub.read_only.add("kaappu/website")
    assert _lookup(owner, "kaappu").json()["path"] == "pick"
    hub.calls.clear()
    res = owner.post("/v1/identity/github/accounts/allow",
                     json={"project_id": project_id, "installation_id": iid,
                           "repos": ["kaappu/helpdesk", "kaappu/website"]})
    assert res.status_code == 200, res.text
    out = res.json()
    by = {r["full_name"]: r for r in out["results"]}
    assert by["kaappu/helpdesk"]["state"] == "working"
    assert by["kaappu/helpdesk"]["words"] == "Working: BOSS can read it, push branches and open pull requests."
    assert by["kaappu/website"]["state"] == "read_only"
    assert by["kaappu/website"]["words"].startswith("Read only:")
    assert out["line"] == "1 of 2 working."
    # Each check's token was minted on kaappu's installation, not the default (paramg's) one.
    mints = [c for c in hub.calls if c.endswith("/access_tokens")]
    assert f"POST /app/installations/{iid}/access_tokens" in mints
    assert "ghs_fake" not in res.text
    with engine.connect() as db:
        allowed = db.execute(text("SELECT full_name FROM github_repos WHERE org_id = :o AND allowed "
                                  "ORDER BY 1"), {"o": hub.org}).scalars().all()
    assert allowed == ["kaappu/helpdesk", "kaappu/website"]
    # The default account's repositories are still listed (the refresh read both installations).
    with engine.connect() as db:
        reachable = db.execute(text("SELECT full_name FROM github_repos WHERE org_id = :o AND reachable "
                                    "ORDER BY 1"), {"o": hub.org}).scalars().all()
    assert "paramg/boss" in reachable and "kaappu/infra-scripts" in reachable


def test_a_repository_outside_the_installation_is_refused_in_words(hub, owner, engine):
    project_id, _ = _project(engine, hub.org)
    iid = hub.install("kaappu")
    assert _lookup(owner, "kaappu").json()["path"] == "pick"
    res = owner.post("/v1/identity/github/accounts/allow",
                     json={"project_id": project_id, "installation_id": iid, "repos": ["paramg/boss"]})
    assert res.status_code == 422
    assert "is not one of kaappu's repositories" in res.json()["message"]


def test_a_token_for_another_accounts_repo_comes_from_that_accounts_installation(hub, owner, engine):
    iid = hub.install("kaappu")
    assert _lookup(owner, "kaappu").json()["path"] == "pick"
    with engine.begin() as db:
        token = github_setup.control_plane_token(db, "kaappu", "helpdesk")
        default = github_setup.control_plane_token(db, "paramg", "boss")
    assert token and hub.tokens[token] == iid
    assert default and hub.tokens[default] != iid
