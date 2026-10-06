"""The access-administration API's read side the console builds on (S4): role versions, a
binding's group and where it comes from, who can grant a permission, and asking for access."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

client = TestClient(app)


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def test_a_role_version_is_readable_by_number(world):
    owner = world.person("owner")
    world.bind(owner, "owner")
    with world.acting_as(world.ctx(owner)):
        role = client.post("/v1/identity/access-roles", json={
            "key": "readers", "name": "Readers", "permissions": ["project:read"]}).json()
        client.patch(f"/v1/identity/access-roles/{role['id']}",
                     json={"permissions": ["project:read", "code_task:read"]})
        v1 = client.get(f"/v1/identity/access-roles/{role['id']}/versions/1")
        v2 = client.get(f"/v1/identity/access-roles/{role['id']}/versions/2")
        missing = client.get(f"/v1/identity/access-roles/{role['id']}/versions/9")
    assert v1.json()["permissions"] == ["project:read"]
    assert v2.json()["permissions"] == ["code_task:read", "project:read"]
    assert missing.status_code == 404


def test_a_group_binding_names_its_group_and_provider(world):
    owner, alice = world.person("owner"), world.person("alice")
    world.bind(owner, "owner")
    okta = world.group("builders", members=(alice,), source="scim")
    world.bind(okta, "operator", subject_type="group")
    with world.acting_as(world.ctx(owner)):
        listed = client.get(f"/v1/identity/role-bindings?subject_id={okta}").json()["bindings"]
    assert listed[0]["via_group"] == {"id": okta, "name": "builders", "provider": "scim"}


def test_grantors_are_the_people_who_could_grant_it_and_nobody_else(world):
    owner, admin, op, viewer = (world.person(n) for n in ("Olive", "Adam", "Opal", "Vic"))
    world.bind(owner, "owner")
    world.bind(admin, "admin")
    world.bind(op, "operator")
    world.bind(viewer, "viewer")
    with world.acting_as(world.ctx(viewer)):
        deploy = client.get("/v1/identity/grantors?permission=deployment:create").json()
        owners = client.get("/v1/identity/grantors?permission=owner:assign").json()
        asked = client.post("/v1/identity/access-requests",
                            json={"permission": "deployment:create", "reason": "ship the fix"})
        smuggled = client.post("/v1/identity/access-requests",
                               json={"permission": "deployment:create", "principal_id": owner.id})
    assert [g["display_name"] for g in deploy["grantors"]] == ["Adam", "Olive"]
    assert [g["display_name"] for g in owners["grantors"]] == ["Olive"]
    assert asked.status_code == 202 and asked.json()["grantors"] == deploy["grantors"]
    assert smuggled.status_code == 422      # nobody asks on someone else's behalf


def test_a13_an_access_request_is_stored_once_recorded_and_listed_for_admins(world):
    """Amendment A13 (integration): the request is durable, de-duplicated while open, audited as
    access.requested, and visible to whoever can read bindings — not to the requester's peers."""
    from sqlalchemy import text

    owner, viewer = world.person("Olive"), world.person("Vic")
    world.bind(owner, "owner")
    world.bind(viewer, "viewer")
    with world.acting_as(world.ctx(viewer)):
        first = client.post("/v1/identity/access-requests",
                            json={"permission": "deployment:create", "reason": "ship the fix"})
        again = client.post("/v1/identity/access-requests", json={"permission": "deployment:create"})
        peers = client.get("/v1/identity/access-requests")
    assert first.status_code == 202 and first.json()["status"] == "requested"
    assert again.json()["id"] == first.json()["id"] and again.json()["already_open"] is True
    assert peers.status_code == 403
    with world.acting_as(world.ctx(owner)):
        listed = client.get("/v1/identity/access-requests").json()["requests"]
    assert [(r["requester_principal_id"], r["permission_key"]) for r in listed] == [
        (viewer.id, "deployment:create")]
    with world.engine.begin() as db:
        events = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                                 "event_type = 'access.requested'"), {"o": world.org}).scalar()
    assert events == 1
