"""`token_for_repository` must never hand a repository the project has not bound the token of a
different, bound repository (the primary's). That silent fallback is a credential-leak bug, not
a convenience: a build on a repository BOSS was never told to touch would otherwise push with
whatever account happens to be the project's primary (FR1.5)."""
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


def test_p1_a_bound_repository_still_resolves_its_own_connections_token():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    bind(engine, world, pid, "acme/api", role="source")

    service = GitHubConnectionService(get_settings())
    service._resolve_secret = lambda record, connection: "tok-for-acme-api"  # noqa: SLF001
    with engine.connect() as conn:
        token = service.token_for_repository(conn, project_id=pid, owner="acme", repo="api")
    assert token == "tok-for-acme-api"
