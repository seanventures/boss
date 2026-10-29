"""github_token_for must resolve the token that belongs to the SPECIFIC owner/repo being acted
on, never "whichever connection was updated most recently" (MR Part 2, change 4)."""
from __future__ import annotations

from boss_engine.config import get_settings
from boss_engine.services.github_connections import GitHubConnectionService, github_token_for
from boss_engine.services.secret_refs import SecretRefService

from ._multirepo_support import make_engine, make_world


def _connect(service, conn, pid, *, label, env_key, monkeypatch):
    secret_refs = SecretRefService()
    monkeypatch.setenv(env_key, f"token-for-{label}")
    secret_refs.create_secret_ref(
        conn, project_id=pid, ref_kind="env", ref_key=env_key, provider="github",
        status="active", metadata={"purpose": "test"}, actor="test")
    return service.create_connection(
        conn, project_id=pid, connection_label=label, auth_method="personal_access_token",
        credential_ref=f"env:{env_key}", app_id=None, installation_id=None,
        api_base_url="https://api.github.com", required_permissions=["pull"], metadata={},
        actor="test")


def test_p0_github_token_for_returns_each_connections_own_token(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    settings = get_settings()
    service = GitHubConnectionService(settings)

    with engine.begin() as conn:
        first = _connect(service, conn, pid, label="acme", env_key="BOSS_TEST_GTF_ACME",
                         monkeypatch=monkeypatch)
        second = _connect(service, conn, pid, label="globex", env_key="BOSS_TEST_GTF_GLOBEX",
                          monkeypatch=monkeypatch)
        service.create_repository_binding(
            conn, project_id=pid, connection_id=first["id"], repository="acme/boss",
            role="primary", default_branch=None, metadata={}, actor="test")
        service.create_repository_binding(
            conn, project_id=pid, connection_id=second["id"], repository="globex/widgets",
            role="source", default_branch=None, metadata={}, actor="test")

    with engine.begin() as conn:
        token_acme = github_token_for(conn, settings, pid, "acme", "boss")
        token_globex = github_token_for(conn, settings, pid, "globex", "widgets")

    assert token_acme == "token-for-acme"
    assert token_globex == "token-for-globex"
    assert token_acme != token_globex


def test_p1_github_token_for_returns_empty_for_an_unbound_repo(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    settings = get_settings()
    service = GitHubConnectionService(settings)

    with engine.begin() as conn:
        conn_row = _connect(service, conn, pid, label="acme", env_key="BOSS_TEST_GTF_UNBOUND",
                            monkeypatch=monkeypatch)
        service.create_repository_binding(
            conn, project_id=pid, connection_id=conn_row["id"], repository="acme/boss",
            role="primary", default_branch=None, metadata={}, actor="test")

    with engine.begin() as conn:
        token = github_token_for(conn, settings, pid, "someone-else", "unrelated-repo")

    assert token == "", "an unbound repository must never borrow a sibling connection's token"
