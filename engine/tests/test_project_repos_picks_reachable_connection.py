"""Adding a repo with no connection_id must pick whichever of the project's connections can
ACTUALLY REACH the repo being added (probed), never just the newest working one (MR Part 2,
change 3)."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.multirepo import project_repos
from boss_engine.services import setup_checks
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


def _registry_row(engine, org_id: str, full_name: str) -> str:
    rid = f"ghrepo_{uuid.uuid4().hex[:10]}"
    owner, name = full_name.split("/")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, default_branch, allowed, "
            "archived, reachable) VALUES (:i, :o, :f, 'main', true, false, true)"),
            {"i": rid, "o": org_id, "f": full_name})
    return rid


def test_p1_add_without_connection_id_picks_the_connection_that_can_reach_the_repo(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    org_id = world["org"]
    settings = get_settings()
    service = GitHubConnectionService(settings)

    with engine.begin() as conn:
        # make_world already seeded a first connection with an unresolvable credential (acts as
        # a connection that cannot reach anything); add one with a real, reachable credential.
        reachable = _connect(service, conn, pid, label="acme",
                             env_key="BOSS_TEST_PR_REACHABLE", monkeypatch=monkeypatch)

    registry_id = _registry_row(engine, org_id, "acme/extra")

    def fake_probe(token, full_name, *, api_base="https://api.github.com", http=None):
        ok = token == "token-for-acme"
        state = "working" if ok else "no_access"
        check = {"ok": ok, "state": state, "at": "now"}
        return {"state": state, "default_branch": "main", "private": True,
                "expires_at": None, "retry_at": None,
                "checks": {"read": check, "write": dict(check), "pull_requests": dict(check)}}

    monkeypatch.setattr(setup_checks, "probe_repository", fake_probe)

    with engine.begin() as conn:
        result = project_repos.add(
            conn, settings, pid, registry_repo_id=registry_id, connection_id=None,
            actor="test")

    with engine.begin() as conn:
        used = conn.execute(
            text("SELECT connection_id FROM github_repository_bindings WHERE id = :b"),
            {"b": result["binding_id"]}).scalar()
    assert str(used) == str(reachable["id"])
