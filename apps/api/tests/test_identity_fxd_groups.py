"""Groups end to end over HTTP, through the real app, the real guard and the real PDP
(IDENTITY.md §6.8, §6.11; FX-D P1-21 items 3, 3a, 3b, 11b).

  * A manual group made through the API, a role bound to it, a member added: that member's next
    request needing the role's permission is served; removing the member refuses the very next
    request; deleting the group takes its bindings with it.
  * Adding ANOTHER person to a group needs every permission the group's bindings carry (3b).
  * IdP groups (SCIM, OIDC) are read-only in BOSS: the manual groups API refuses to touch their
    members, name or existence (11b) — while binding a role to one is how they are mapped.
  * Changing membership is granting, so it is a critical permission (FX-B).

The SCIM- and OIDC-synced halves are in test_identity_fxd_idp_groups.py (they need the S1 sign-in
world).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.permissions_gen import PERMISSION_META

client = TestClient(app)


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _owner(world):
    owner = world.person("owner")
    world.bind(owner, "owner")
    return owner


#: A route declared at PROJECT scope (agent:read @ project(query=project_id)) that Operator and
#: Viewer hold: a project binding reaches it only in its own project.
PROJECT_ROUTE, PROJECT_PERMISSION = "/v1/agent-groups", "agent:read"


def _tasks(world, project):
    return client.get(PROJECT_ROUTE, params={"project_id": project})


def test_a_manual_group_grants_its_role_to_members_and_only_while_they_are_members(world):
    owner, member = _owner(world), world.person("member")
    with world.acting_as(world.ctx(member)):
        before = _tasks(world, world.project_a)
    assert before.status_code == 403 and before.json() == {"error": "forbidden", "permission": PROJECT_PERMISSION}

    with world.acting_as(world.ctx(owner)):
        made = client.post("/v1/identity/groups", json={"key": "builders", "name": "Builders"})
        assert made.status_code == 201, made.text
        group = made.json()["id"]
        bound = client.post(f"/v1/identity/projects/{world.project_a}/role-bindings", json={
            "subject_type": "group", "subject_id": group, "role_id": "arl_operator"})
        assert bound.status_code == 201, bound.text
    with world.acting_as(world.ctx(member)):
        assert _tasks(world, world.project_a).status_code == 403    # bound, but not yet a member

    with world.acting_as(world.ctx(owner)):
        added = client.post(f"/v1/identity/groups/{group}/members", json={"principal_id": member.id})
    assert added.status_code == 200, added.text
    with world.acting_as(world.ctx(member)):
        granted = _tasks(world, world.project_a)
        elsewhere = _tasks(world, world.project_b)
        org_route = client.get("/v1/identity/role-bindings")
    assert granted.status_code == 200, granted.text
    assert elsewhere.status_code == 403            # the group's binding is at project A only
    assert org_route.status_code == 403

    with world.acting_as(world.ctx(owner)):
        removed = client.delete(f"/v1/identity/groups/{group}/members/{member.id}")
    assert removed.status_code == 200, removed.text
    with world.acting_as(world.ctx(member)):
        after = _tasks(world, world.project_a)
    assert after.status_code == 403 and after.json()["permission"] == PROJECT_PERMISSION

    with world.acting_as(world.ctx(owner)):
        assert client.post(f"/v1/identity/groups/{group}/members",
                           json={"principal_id": member.id}).status_code == 200
    with world.acting_as(world.ctx(member)):
        assert _tasks(world, world.project_a).status_code == 200
    with world.acting_as(world.ctx(owner)):
        deleted = client.delete(f"/v1/identity/groups/{group}")
        gone = client.get(f"/v1/identity/groups/{group}")
    assert deleted.status_code == 204, deleted.text
    assert gone.status_code == 404
    with world.acting_as(world.ctx(member)):
        assert _tasks(world, world.project_a).status_code == 403
    with world.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE subject_id = :g"),
                          {"g": group}).scalar() == 0


def test_a_group_bound_org_wide_reaches_every_project_and_the_org_routes_of_its_role(world):
    owner, member = _owner(world), world.person("member")
    with world.acting_as(world.ctx(owner)):
        group = client.post("/v1/identity/groups", json={"key": "auditors", "name": "Auditors"}).json()["id"]
        assert client.post("/v1/identity/role-bindings", json={
            "subject_type": "group", "subject_id": group, "role_id": "arl_viewer"}).status_code == 201
        assert client.post(f"/v1/identity/groups/{group}/members",
                           json={"principal_id": member.id}).status_code == 200
    with world.acting_as(world.ctx(member)):
        assert _tasks(world, world.project_a).status_code == 200
        assert _tasks(world, world.project_b).status_code == 200
        assert client.get("/v1/identity/access-roles").status_code == 200       # viewer: access_role:read
        assert client.get("/v1/identity/groups").status_code == 403             # not viewer's


# ---- 3b: adding someone else is granting ----------------------------------------------------------------

def test_adding_another_person_to_a_group_needs_every_permission_its_bindings_carry(world):
    admin, dana = world.person("admin"), world.person("dana")
    world.bind(admin, "admin")
    owners = world.group("owners-g")
    world.bind(owners, "owner", subject_type="group")
    keepers = world.group("keepers-g")
    world.bind(keepers, world.custom_role("policy_keeper", ["security_policy:manage", "project:read"]),
               subject_type="group")
    project_admins = world.group("padmins-b")
    world.bind(project_admins, "admin", project=world.project_b, subject_type="group")
    operators = world.group("operators-g")
    world.bind(operators, "operator", subject_type="group")
    with world.acting_as(world.ctx(admin)):
        to_owners = client.post(f"/v1/identity/groups/{owners}/members", json={"principal_id": dana.id})
        to_keepers = client.post(f"/v1/identity/groups/{keepers}/members", json={"principal_id": dana.id})
        to_padmins = client.post(f"/v1/identity/groups/{project_admins}/members", json={"principal_id": dana.id})
        to_operators = client.post(f"/v1/identity/groups/{operators}/members", json={"principal_id": dana.id})
    assert to_owners.status_code == 403 and to_owners.json()["reason"] == "grant_ceiling", to_owners.text
    assert to_keepers.status_code == 403 and to_keepers.json()["missing"] == ["security_policy:manage"]
    assert to_padmins.status_code == 200, to_padmins.text       # an org Admin covers Admin at project B
    assert to_operators.status_code == 200, to_operators.text
    with world.engine.connect() as db:
        groups = {r[0] for r in db.execute(text(
            "SELECT group_id FROM identity_group_members WHERE principal_id = :p"), {"p": dana.id})}
    assert groups == {project_admins, operators}


def test_removing_another_person_from_a_group_needs_the_same(world):
    """Removing a member changes their access too: an Admin cannot take someone out of a group
    that carries more than the Admin holds."""
    admin, erin = world.person("admin"), world.person("erin")
    world.bind(admin, "admin")
    keepers = world.group("keepers-r", members=(erin,))
    world.bind(keepers, world.custom_role("policy_keeper", ["security_policy:manage", "project:read"]),
               subject_type="group")
    with world.acting_as(world.ctx(admin)):
        refused = client.delete(f"/v1/identity/groups/{keepers}/members/{erin.id}")
    assert refused.status_code == 403 and "security_policy:manage" in refused.json()["missing"]


# ---- 11b: IdP groups are read-only here --------------------------------------------------------------------

@pytest.mark.parametrize("source", ["scim", "oidc"])
def test_the_manual_groups_api_refuses_to_edit_an_idp_group(world, source):
    owner, member, outsider = _owner(world), world.person("member"), world.person("outsider")
    group = world.group(f"idp-{source}", members=(member,), source=source)
    with world.acting_as(world.ctx(owner)):
        add = client.post(f"/v1/identity/groups/{group}/members", json={"principal_id": outsider.id})
        remove = client.delete(f"/v1/identity/groups/{group}/members/{member.id}")
        rename = client.patch(f"/v1/identity/groups/{group}", json={"name": "Renamed"})
        delete = client.delete(f"/v1/identity/groups/{group}")
        mapped = client.post("/v1/identity/role-bindings", json={
            "subject_type": "group", "subject_id": group, "role_id": "arl_viewer"})
    for res in (add, remove, rename, delete):
        assert res.status_code == 409 and res.json()["error"] == "idp_group_read_only", res.text
    assert mapped.status_code == 201, mapped.text     # mapping an IdP group to a role is allowed
    with world.engine.connect() as db:
        members = {r[0] for r in db.execute(text(
            "SELECT principal_id FROM identity_group_members WHERE group_id = :g"), {"g": group})}
        name = db.execute(text("SELECT name FROM identity_groups WHERE id = :g"), {"g": group}).scalar()
    assert members == {member.id} and name == f"idp-{source}"


def test_changing_group_membership_is_a_critical_permission():
    # Was a strict xfail until fx-b made group:manage_members critical (merged 2deaa445).
    assert PERMISSION_META["role_binding:assign"].critical
    assert PERMISSION_META["group:manage_members"].critical
