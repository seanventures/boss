"""Setup › Add repo lists what EVERY connected account reaches, grouped by account (operator,
2026-11-06: only the personal token's repositories were listed, while paramg/helpdesk-ai-demo,
newly granted to the BOSS GitHub App, never appeared). A refused account says so in its own group;
the org's App is offered even when this project has no connection to it yet, and picking one of
its repositories connects the App to the project through repository_from_app, once."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import github_setup
from boss_engine.services import setup_actions, setup_checks
from boss_engine.services.github_connections import GitHubConnectionService
from boss_engine.services.secret_refs import SecretRefService

from ._multirepo_support import make_engine, make_world


def _token_connection(conn, pid, monkeypatch):
    monkeypatch.setenv("BOSS_TEST_ADD_REPO_TOKEN", "token-for-paramg")
    SecretRefService().create_secret_ref(
        conn, project_id=pid, ref_kind="env", ref_key="BOSS_TEST_ADD_REPO_TOKEN", provider="github",
        status="active", metadata={"purpose": "test"}, actor="test")
    return GitHubConnectionService(get_settings()).create_connection(
        conn, project_id=pid, connection_label="paramg token", auth_method="personal_access_token",
        credential_ref="env:BOSS_TEST_ADD_REPO_TOKEN", app_id=None, installation_id=None,
        api_base_url="https://api.github.com", required_permissions=["pull"], metadata={}, actor="test")


def _fake_github(monkeypatch):
    def inspect(token, *, api_base="https://api.github.com", http=None):
        assert token == "token-for-paramg"
        return {"ok": True, "state": "working", "account_login": "paramg", "expires_at": None,
                "repos": [{"full_name": "paramg/tool", "private": False, "default_branch": "main",
                           "owner_type": "User"},
                          {"full_name": "paramg/boss", "private": True, "default_branch": "main",
                           "owner_type": "User"}]}

    monkeypatch.setattr(setup_checks, "inspect_token", inspect)
    cfg = {"source": "setup", "app_id": "1", "installation_id": "9", "account_login": "paramg",
           "account_type": "User", "slug": "boss"}
    monkeypatch.setattr(github_setup, "app_config", lambda db, org_id=None: dict(cfg))
    monkeypatch.setattr(github_setup, "app_ready", lambda db, org_id=None: True)
    monkeypatch.setattr(github_setup, "installation_repositories", lambda db: [
        {"full_name": "paramg/helpdesk-ai-demo", "private": True, "default_branch": "main", "owner_type": "User"},
        {"full_name": "paramg/boss", "private": True, "default_branch": "main", "owner_type": "User"}])


def test_every_account_is_listed_grouped_and_the_apps_repo_binds_through_the_app(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    settings = get_settings()
    _fake_github(monkeypatch)
    with engine.begin() as conn:
        token = _token_connection(conn, pid, monkeypatch)

    with engine.begin() as conn:
        out = setup_actions.available_repositories(conn, settings, pid)
    groups = {g["connection_id"]: g for g in out["groups"]}
    # The token's group lists its repositories; the org App's group is offered as `org_app`.
    assert [r["full_name"] for r in groups[str(token["id"])]["repos"]] == ["paramg/tool", "paramg/boss"]
    app = groups[setup_actions.ORG_APP_PICK]
    assert app["kind"] == "org_app" and app["account"] == "paramg"
    # A repository two accounts reach is listed once, under the first.
    assert [r["full_name"] for r in app["repos"]] == ["paramg/helpdesk-ai-demo"]
    # The connection make_world seeded cannot be read: it says so, and the others still list.
    refused = [g for g in out["groups"] if g["error"]]
    assert refused and all(g["error"]["headline"] for g in refused)

    with engine.begin() as conn:
        first = setup_actions.resolve_pick_connection(conn, settings, pid, setup_actions.ORG_APP_PICK, actor="test")
        again = setup_actions.resolve_pick_connection(conn, settings, pid, setup_actions.ORG_APP_PICK, actor="test")
        assert first == again
        ref = conn.execute(text("SELECT credential_ref FROM github_connections WHERE id = :c"), {"c": first}).scalar()
        assert ref == setup_actions.APP_CREDENTIAL_REF
        assert setup_actions.resolve_pick_connection(conn, settings, pid, str(token["id"])) == str(token["id"])

    # With the App connected to the project, it is one group under its own connection.
    with engine.begin() as conn:
        out = setup_actions.available_repositories(conn, settings, pid)
    ids = [g["connection_id"] for g in out["groups"]]
    assert setup_actions.ORG_APP_PICK not in ids and first in ids


def test_a_bound_repository_is_marked_in_project(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    _fake_github(monkeypatch)
    with engine.begin() as conn:
        token = _token_connection(conn, pid, monkeypatch)
        GitHubConnectionService(get_settings()).create_repository_binding(
            conn, project_id=pid, connection_id=token["id"], repository="paramg/tool", role="source",
            default_branch=None, metadata={}, actor="test")
    with engine.begin() as conn:
        out = setup_actions.available_repositories(conn, get_settings(), pid)
    tool = next(r for g in out["groups"] for r in g["repos"] if r["full_name"] == "paramg/tool")
    assert tool["in_project"] is True


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def json(self):
        return self._body


def test_githubs_return_is_checked_against_the_apps_own_api(monkeypatch):
    """A GitHub-initiated return names an installation id; only the App's API can say it is ITS
    own. Nothing is stored either way: a redirect never swaps the installation builds use."""
    from boss_engine.identity import github_app

    cfg = {"source": "setup", "app_id": "1", "installation_id": "9", "account_login": "paramg"}
    monkeypatch.setattr(github_setup, "app_config", lambda db, org_id=None: dict(cfg))
    monkeypatch.setattr(github_app, "app_jwt", lambda db: "jwt")
    asked: list[str] = []

    def call(method, path, token="", **kw):
        asked.append(path)
        return _Resp(200, {"account": {"login": "paramg"}}) if path.endswith("/9") else _Resp(404, {})

    monkeypatch.setattr(github_app, "_call", call)
    assert github_setup.confirm_installation(installation_id="9", org_id="org", db=None) == "paramg"
    try:
        github_setup.confirm_installation(installation_id="77", org_id="org", db=None)
    except github_setup.GitHubSetupError as exc:
        assert exc.code == "not_this_apps_installation"
    else:
        raise AssertionError("an installation the App does not know was accepted")
    assert asked == ["/app/installations/9", "/app/installations/77"]
    cfg.update(source=None, app_id=None)
    assert github_setup.confirm_installation(installation_id="9", org_id="org", db=None) is None
