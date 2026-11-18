"""THE RAIL'S INSIDER PROBE ASKS THE WHOLE INSTALL (review of #200).

Self-Fixes and Landed slices show on an install where BOSS has fixed itself. The first probe read
the open project's list, so on an operator's laptop 15 of 18 projects hid both. GET
/v1/self-fixes/exists answers for every project of the org.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("insider-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def test_a_project_with_none_answers_yes_when_another_project_has_one(world, client, engine):
    with world.acting_as(world.ctx(world.owner)):
        quiet = client.post("/v1/projects", json={"name": f"Quiet {uuid.uuid4().hex[:5]}"}).json()["id"]
        dogfood = client.post("/v1/projects", json={"name": f"Dogfood {uuid.uuid4().hex[:5]}"}).json()["id"]
        before = client.get(f"/v1/self-fixes/exists?project_id={quiet}")
    assert before.status_code == 200, before.text
    assert before.json() == {"exists": False}
    with engine.begin() as db:
        db.execute(text("INSERT INTO self_fixes (id, project_id, signature, class_signature, status, "
                        "confidence, created_by) VALUES (:i, :p, 'deploy:port_held:8000', "
                        "'deploy:port_held', 'proposed', 'high', 'test')"),
                   {"i": f"sfx_{uuid.uuid4().hex[:8]}", "p": dogfood})
    with world.acting_as(world.ctx(world.owner)):
        own = client.get(f"/v1/self-fixes?project_id={quiet}")
        after = client.get(f"/v1/self-fixes/exists?project_id={quiet}")
    assert own.status_code == 200 and own.json()["count"] == 0
    assert after.status_code == 200 and after.json() == {"exists": True}
