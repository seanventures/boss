"""An agent's GitHub limit (its Passport's `external_ceiling`) is editable from the console:

  * PATCH /v1/identity/agents/{id}/passport {"external_ceiling": ...} by someone holding
    agent_passport:manage (Owner, Admin); an Operator is refused;
  * never above what the BOSS GitHub App asks GitHub for (github_setup.APP_PERMISSIONS): a raise
    past it is 422 above_github_connection and nothing is stored;
  * every change is lineage, with the before, the after and the keys it widened.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

H = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


def _client():
    return TestClient(app, raise_server_exceptions=False)


def _limit(world, agent_id):
    with world.engine.begin() as db:
        return db.execute(text("SELECT external_ceiling FROM identity_agent_passports WHERE agent_id = :a"),
                          {"a": agent_id}).scalar() or {}


def test_an_admin_sets_an_agents_github_limit_and_it_is_lineage(world):
    admin = world.person("gh-admin")
    world.bind(admin, "admin")
    agent = world.agent("gh-reviewer", ceiling=("github:read",), sponsor=admin)
    client = _client()
    with world.acting_as(world.ctx(admin)):
        r = client.patch(f"/v1/identity/agents/{agent.id}/passport",
                         json={"external_ceiling": {"github": {"contents": "read", "pull_requests": "write"}}}, headers=H)
        assert r.status_code == 200, r.text
        lineage = client.get(f"/v1/identity/agents/{agent.id}/lineage").json()["events"]
    assert _limit(world, agent.id) == {"github": {"contents": "read", "pull_requests": "write"}}
    changed = [e for e in lineage if e["event"] == "agent.passport.ceiling_changed"]
    assert changed, lineage


def test_a_raise_above_the_github_app_is_refused_and_nothing_is_stored(world):
    owner = world.person("gh-owner")
    world.bind(owner, "owner")
    agent = world.agent("gh-planner", ceiling=("github:read",), sponsor=owner)
    client = _client()
    with world.acting_as(world.ctx(owner)):
        r = client.patch(f"/v1/identity/agents/{agent.id}/passport",
                         json={"external_ceiling": {"github": {"contents": "read", "issues": "write"}}}, headers=H)
    assert r.status_code == 422 and r.json()["error"] == "above_github_connection", r.text
    assert r.json().get("key") == "issues"
    assert _limit(world, agent.id) == {}


def test_an_operator_cannot_change_an_agents_github_limit(world):
    operator = world.person("gh-operator")
    world.bind(operator, "operator")
    agent = world.agent("gh-builder", ceiling=("github:read",), sponsor=operator)
    client = _client()
    with world.acting_as(world.ctx(operator)):
        r = client.patch(f"/v1/identity/agents/{agent.id}/passport",
                         json={"external_ceiling": {"github": {"contents": "write"}}}, headers=H)
    assert r.status_code == 403, r.text
    assert _limit(world, agent.id) == {}


# ---- rc6: BOSS's own repo-reader Passport is not editable by people ---------------------------------

def _repo_reader(world):
    from boss_engine.identity import runs

    with world.engine.begin() as db:
        # One system reader per install: an earlier test's world may hold it; bring it into this one.
        db.execute(text("UPDATE identity_principals SET org_id = :o, status = 'active' WHERE id = :a"),
                   {"o": world.org, "a": runs.SYSTEM_REPO_READER})
        assert runs.system_repo_reader(db, project_id=world.project_a) is not None
    return runs.SYSTEM_REPO_READER


def _reader_row(world, agent_id):
    with world.engine.begin() as db:
        row = db.execute(text("SELECT external_ceiling, version, can_spawn, max_depth FROM identity_agent_passports "
                              "WHERE agent_id = :a"), {"a": agent_id}).mappings().first()
        ceiling = sorted(db.execute(text("SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"),
                                    {"a": agent_id}).scalars())
    return dict(row), ceiling


def test_the_system_repo_reader_is_a_system_passport():
    from boss_engine.identity import passports, runs

    assert runs.SYSTEM_REPO_READER in passports.SYSTEM_PASSPORTS


@pytest.mark.parametrize("body", [
    {"external_ceiling": {"github": {"contents": "write", "metadata": "read"}}},
    {"external_ceiling": {"github": {"contents": "read"}}},          # even a narrowing
    {"ceiling": ["repository:read", "github:read", "repository:write"]},
    {"ceiling": []},
    {"can_spawn": True},
    {"max_depth": 2},
])
def test_no_person_edits_the_system_repo_readers_passport(world, body):
    owner = world.person("rr-owner")
    world.bind(owner, "owner")
    reader = _repo_reader(world)
    before = _reader_row(world, reader)
    client = _client()
    with world.acting_as(world.ctx(owner)):
        for path in (f"/v1/identity/agents/{reader}/passport", f"/v1/identity/agents/{reader}"):
            r = client.patch(path, json=body, headers=H)
            assert r.status_code == 409, r.text
            assert r.json()["error"] == "system_passport"
            assert "BOSS manages this agent" in r.json()["message"]
    assert _reader_row(world, reader) == before


def test_a_person_can_still_stop_the_system_repo_reader(world):
    owner = world.person("rr-owner2")
    world.bind(owner, "owner")
    reader = _repo_reader(world)
    client = _client()
    with world.acting_as(world.ctx(owner)):
        r = client.post(f"/v1/identity/agents/{reader}/suspend", json={"reason": "pause"}, headers=H)
        assert r.status_code == 200, r.text
        r = client.post(f"/v1/identity/agents/{reader}/resume", json={"reason": "back"}, headers=H)
        assert r.status_code == 200, r.text
