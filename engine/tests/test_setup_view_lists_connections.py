"""setup_view's repository payload must list every connection a project holds, each with its own
account_login, so the console can group repos by account (MR Part 2, change 2)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import setup_view
from boss_engine.services.github_connections import GitHubConnectionService
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


def test_p1_repository_view_lists_every_connection_the_project_holds(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    settings = get_settings()
    service = GitHubConnectionService(settings)

    with engine.begin() as conn:
        first = _connect(service, conn, pid, label="acme", env_key="BOSS_TEST_SV_ACME",
                         monkeypatch=monkeypatch)
        second = _connect(service, conn, pid, label="globex", env_key="BOSS_TEST_SV_GLOBEX",
                          monkeypatch=monkeypatch)
        service.create_repository_binding(
            conn, project_id=pid, connection_id=first["id"], repository="acme/boss",
            role="primary", default_branch=None, metadata={}, actor="test")
        service.create_repository_binding(
            conn, project_id=pid, connection_id=second["id"], repository="globex/widgets",
            role="source", default_branch=None, metadata={}, actor="test")
        conn.execute(
            text("UPDATE github_connections SET account_login = :l WHERE id = :i"),
            {"l": "acme", "i": first["id"]})
        conn.execute(
            text("UPDATE github_connections SET account_login = :l WHERE id = :i"),
            {"l": "globex", "i": second["id"]})

    with engine.begin() as conn:
        view = setup_view._repository(conn, settings, pid, datetime.now(timezone.utc))

    connections = view.get("connections")
    assert connections is not None, "a multi-account project must list its connections"
    assert len(connections) == 2
    logins = {c["account_login"] for c in connections}
    assert logins == {"acme", "globex"}

    repos_by_name = {r["full_name"]: r for r in view["repos"]}
    assert repos_by_name["acme/boss"]["connection_id"] == first["id"]
    assert repos_by_name["globex/widgets"]["connection_id"] == second["id"]

    # Compatibility: the singular `connection` key keeps working for callers that have not
    # migrated to the `connections` list.
    assert view["connection"] is not None
