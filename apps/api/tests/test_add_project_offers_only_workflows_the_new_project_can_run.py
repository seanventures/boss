"""ADD PROJECT OFFERS ONLY WORKFLOWS THE NEW PROJECT CAN RUN (BOSSAPP-946, finding 3).

The local-first demo opened Add project from boss-dogfood, whose own "HelpDesk Delivery (D1)" was
listed and pre-picked; Create was refused "No active workflow 'helpdesk_delivery_d1' to run",
because the new project is seeded with the catalogue, not with another project's workflows.

The list (the preview's boxes and Helm's choices) is now what the NEW project will have: the
catalogue entries every project is seeded with. The default is one of them.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.routers import add_project as route
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services.project_experience import add_project

SPEC = {"entry_step": "build", "steps": [
    {"id": "build", "kind": "capability", "next": [], "binding": {"capability": "build", "inputs": {
        "title": {"from_input": "title"}, "instructions": {"from_input": "directive"}}}}]}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("wf-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


def _own_workflow(engine, project_id: str, key: str, name: str) -> None:
    did, vid = f"wfd_{uuid.uuid4().hex[:12]}", f"wfv_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                        "status, namespace, is_catalogue, created_by) VALUES (:i, :p, :k, :n, "
                        "'active', 'product', false, 'test')"),
                   {"i": did, "p": project_id, "k": key, "n": name})
        db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                        "version, status, spec, content_hash, created_by) VALUES (:v, :p, :d, 1, "
                        "'published', CAST(:s AS JSONB), :h, 'test')"),
                   {"v": vid, "p": project_id, "d": did, "s": json.dumps(SPEC),
                    "h": uuid.uuid4().hex})
        db.execute(text("UPDATE workflow_definitions SET active_version_id = :v WHERE id = :d"),
                   {"v": vid, "d": did})


def test_another_projects_own_workflow_is_never_offered_for_a_new_project(client, world, engine,
                                                                         monkeypatch):
    with _owner(world):   # a seeded project: the catalogue, installed
        home = client.post("/v1/projects", json={"name": f"Dogfood {uuid.uuid4().hex[:6]}"}).json()["id"]
    _own_workflow(engine, home, "helpdesk_delivery_d1", "HelpDesk Delivery (D1)")
    with engine.begin() as db:   # the open project itself still runs its own
        assert "helpdesk_delivery_d1" in {w["key"] for w in add_project.workflows(db, home)}

    with _owner(world):
        view = client.get(f"/v1/projects/{home}/add-project/preview", params={"mode": "idea"}).json()
    keys = {w["key"] for w in view["workflows"]}
    assert keys and "helpdesk_delivery_d1" not in keys
    assert keys <= add_project.new_project_workflow_keys()
    assert view["default_workflow"] in keys

    # Helm picks from the same list.
    seen = {}

    def understand(_db, _settings, _pid, **kwargs):
        seen["keys"] = {w["key"] for w in kwargs["workflows"]}
        return {"status": "ok", "brief": {}, "picks": {}}

    monkeypatch.setattr(add_project, "understand", understand)
    route._understand(home, route.UnderstandBody(words="a help desk"), "test")
    assert seen["keys"] == keys

    # Every workflow offered is one the new project has: Create never answers "No active
    # workflow" for a pick from the list.
    with engine.begin() as db:
        fresh = db.execute(text("SELECT workflow_key FROM workflow_definitions WHERE "
                                "project_id = :p AND status = 'active' AND "
                                "active_version_id IS NOT NULL"), {"p": home}).scalars().all()
    assert keys <= set(fresh)
    for key in keys:
        with engine.begin() as db:
            assert route._version_of(db, home, key)[1], key


def test_the_name_add_project_prefills_is_one_no_project_has(client, world):
    """Finding 9: Add project pre-filled "HelpDesk AI", already taken. The name it fills is the
    engine's answer: the name when free, else the first free "name 2", "name 3" …"""
    name = f"HelpDesk {uuid.uuid4().hex[:6]}"
    with _owner(world):
        home = client.post("/v1/projects", json={"name": name}).json()["id"]
        taken = client.get(f"/v1/projects/{home}/add-project/name", params={"name": name}).json()
        assert client.post("/v1/projects", json={"name": f"{name} 2"}).status_code == 201
        next_free = client.get(f"/v1/projects/{home}/add-project/name", params={"name": name}).json()
        free = client.get(f"/v1/projects/{home}/add-project/name",
                          params={"name": f"Fresh {uuid.uuid4().hex[:6]}"}).json()
    assert taken == {"name": name, "free": f"{name} 2", "taken": True}
    assert next_free["free"] == f"{name} 3"
    assert free["taken"] is False and free["free"] == free["name"]
    # And Create accepts the name it offered.
    with _owner(world):
        assert client.post("/v1/projects", json={"name": next_free["free"]}).status_code == 201
