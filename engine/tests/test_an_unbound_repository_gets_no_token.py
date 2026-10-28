"""`token_for_repository` must never hand a repository the project has not bound the token of a
different, bound repository (the primary's). That silent fallback is a credential-leak bug, not
a convenience: a build on a repository BOSS was never told to touch would otherwise push with
whatever account happens to be the project's primary (FR1.5)."""
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.github_connections import GitHubConnectionService

from ._multirepo_support import bind, make_engine, make_world


def test_p0_an_unbound_repository_returns_none_even_with_a_bound_primary():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    bind(engine, world, pid, "acme/api", role="primary")

    service = GitHubConnectionService(get_settings())
    with engine.connect() as conn:
        token = service.token_for_repository(conn, project_id=pid, owner="other", repo="repo")
    assert token is None


def _register_connection_secret(engine, world, pid: str, env_key: str) -> None:
    """Point the project's connection at a real, resolvable env secret_ref."""
    from boss_engine.services.secret_refs import SecretRefService

    secret_refs = SecretRefService()
    cid = world["connections"][pid]
    with engine.begin() as conn:
        secret_refs.create_secret_ref(
            conn,
            project_id=pid,
            ref_kind="env",
            ref_key=env_key,
            provider="github",
            status="active",
            metadata={"purpose": "test"},
            actor="test",
        )
        conn.execute(
            text(f"UPDATE github_connections SET credential_ref = 'env:{env_key}' WHERE id = :c"),
            {"c": cid},
        )


def test_p1_a_bound_repository_still_resolves_its_own_connections_token(monkeypatch):
    monkeypatch.setenv("ACME_API_GITHUB_TOKEN", "tok-for-acme-api")
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    bind(engine, world, pid, "acme/api", role="source")
    _register_connection_secret(engine, world, pid, "ACME_API_GITHUB_TOKEN")

    service = GitHubConnectionService(get_settings())
    with engine.connect() as conn:
        token = service.token_for_repository(conn, project_id=pid, owner="acme", repo="api")
    assert token == "tok-for-acme-api"


def test_p1_two_repos_on_two_connections_each_resolve_their_own_token(monkeypatch):
    monkeypatch.setenv("CONTRACTS_GITHUB_TOKEN", "tok-for-contracts")
    monkeypatch.setenv("WEB_GITHUB_TOKEN", "tok-for-web")
    engine = make_engine()
    world = make_world(engine)
    pid_a, pid_b = world["projects"][0], world["projects"][1]
    bind(engine, world, pid_a, "acme/contracts", role="source")
    bind(engine, world, pid_b, "acme/web", role="source")
    _register_connection_secret(engine, world, pid_a, "CONTRACTS_GITHUB_TOKEN")
    _register_connection_secret(engine, world, pid_b, "WEB_GITHUB_TOKEN")

    service = GitHubConnectionService(get_settings())
    with engine.connect() as conn:
        token_a = service.token_for_repository(conn, project_id=pid_a, owner="acme", repo="contracts")
        token_b = service.token_for_repository(conn, project_id=pid_b, owner="acme", repo="web")
    assert token_a == "tok-for-contracts"
    assert token_b == "tok-for-web"
    assert token_a != token_b
