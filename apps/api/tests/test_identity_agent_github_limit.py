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
