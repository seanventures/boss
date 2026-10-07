"""Custom access roles end to end over HTTP (IDENTITY.md §6.9, §6.10; FX-D P1-21 items 2, 2a, 2b).

  * The custom-role sweep: an Owner creates a custom role through the API from a chosen subset of
    the catalogue (org-wide, project, step-up and critical permissions mixed), binds it to a person
    through the API, and every operation of the app is sent through the real enforcement point:
    allowed iff the role holds the operation's permission (bound at org; and again bound at a
    project, where org-only permissions are inert and other projects get nothing).
  * Deleting a custom role while any binding (direct or through a group) exists is refused;
    after unbinding it deletes; a built-in role never deletes.
  * Owner and Auditor (org-only bindable), and a custom role made only of org-only permissions,
    cannot be bound at a project.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.identity_fxd_support import (
    OBJECT_OPS,
    ORG_OPS,
    PROJECT_OPS,
    expect,
    place_objects,
    report,
    sweep,
)
from boss_engine.identity.permissions_gen import PERMISSION_META

client = TestClient(app)

#: Every third non-system catalogue permission: a deterministic subset that mixes every kind.
SUBSET = sorted(k for k in sorted(PERMISSION_META) if not PERMISSION_META[k].system_only)[::3]


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _owner(world: IdentityWorld, name: str = "owner"):
    owner = world.person(name)
    world.bind(owner, "owner")
    return owner


def _create_role(world, owner, key, permissions, **extra):
    critical = [p for p in permissions if PERMISSION_META[p].critical]
    with world.acting_as(world.ctx(owner)):
        return client.post("/v1/identity/access-roles", json={
            "key": key, "name": key.title(), "permissions": list(permissions),
            "confirm_critical": critical, **extra})


def _bind(world, caller, subject_id, role_id, *, project=None, subject_type="principal"):
    path = (f"/v1/identity/projects/{project}/role-bindings" if project
            else "/v1/identity/role-bindings")
    with world.acting_as(world.ctx(caller)):
        return client.post(path, json={"subject_type": subject_type, "subject_id": subject_id,
                                       "role_id": role_id})


def test_the_subset_mixes_every_kind_of_permission():
    metas = [PERMISSION_META[k] for k in SUBSET]
    assert any(m.org_only for m in metas) and any(not m.org_only for m in metas)
    assert any(m.step_up for m in metas) and any(m.critical for m in metas)
    assert any(m.delegable for m in metas) and len(SUBSET) > 50


@pytest.mark.parametrize("bound_at", ["org", "project"])
def test_a_custom_role_grants_exactly_its_permissions_on_every_route(mirror, world, monkeypatch, bound_at):
    owner = _owner(world)
    made = _create_role(world, owner, f"subset_{bound_at}", SUBSET)
    assert made.status_code == 201, made.text
    assert sorted(made.json()["permissions"]) == SUBSET
    person = world.person(f"custom-{bound_at}")
    bound = _bind(world, owner, person.id, made.json()["id"],
                  project=world.project_a if bound_at == "project" else None)
    assert bound.status_code == 201, bound.text
    org_held = set(SUBSET) if bound_at == "org" else set()
    project_held = {world.project_a: set(SUBSET)} if bound_at == "project" else {}

    wrong = []
    with world.acting_as(world.ctx(person)):
        for project in (world.project_a, world.project_b):
            place_objects(monkeypatch, world.org, project)
            want = lambda access, p=project: expect(access, org_held=org_held,  # noqa: E731
                                                     project_held=project_held, project=p)
            wrong += sweep(mirror, world, PROJECT_OPS + OBJECT_OPS, want, project=project)
        want_org = lambda access: expect(access, org_held=org_held, project_held=project_held)  # noqa: E731
        wrong += sweep(mirror, world, ORG_OPS, want_org, project=world.project_a)
    allowed = {o[2].permission for o in PROJECT_OPS + OBJECT_OPS + ORG_OPS
               if o[2].permission in SUBSET}
    assert len(allowed) > 30 and not wrong, report(wrong, f"custom role bound at {bound_at}")


def test_a_bound_custom_role_cannot_be_deleted_until_every_binding_is_gone(world):
    owner = _owner(world)
    made = _create_role(world, owner, "deletable", ["code_task:read", "project:read"])
    role_id = made.json()["id"]
    person = world.person("holder")
    direct = _bind(world, owner, person.id, role_id)
    assert direct.status_code == 201
    with world.acting_as(world.ctx(owner)):
        group = client.post("/v1/identity/groups", json={"key": "readers", "name": "Readers"}).json()["id"]
    via_group = _bind(world, owner, group, role_id, project=world.project_a, subject_type="group")
    assert via_group.status_code == 201, via_group.text

    with world.acting_as(world.ctx(owner)):
        both = client.delete(f"/v1/identity/access-roles/{role_id}")
        assert client.delete(f"/v1/identity/role-bindings/{direct.json()['id']}").status_code == 204
        group_only = client.delete(f"/v1/identity/access-roles/{role_id}")
        assert client.delete(f"/v1/identity/projects/{world.project_a}/role-bindings/"
                             f"{via_group.json()['id']}").status_code == 204
        gone = client.delete(f"/v1/identity/access-roles/{role_id}")
        after = client.get(f"/v1/identity/access-roles/{role_id}")
    assert both.status_code == 409 and both.json()["error"] == "role_in_use", both.text
    assert both.json()["bindings"] == 2
    assert group_only.status_code == 409 and group_only.json()["bindings"] == 1
    assert gone.status_code == 204, gone.text
    assert after.status_code == 404


@pytest.mark.parametrize("role_id", ["arl_owner", "arl_admin", "arl_operator", "arl_viewer", "arl_auditor"])
def test_a_built_in_role_never_deletes(world, role_id):
    owner = _owner(world)
    with world.acting_as(world.ctx(owner)):
        res = client.delete(f"/v1/identity/access-roles/{role_id}")
        still = client.get(f"/v1/identity/access-roles/{role_id}")
    assert res.status_code == 409 and res.json()["error"] == "builtin_read_only"
    assert still.status_code == 200


def test_deleting_a_custom_role_needs_every_permission_it_holds(world):
    owner, admin = _owner(world), world.person("admin")
    world.bind(admin, "admin")
    made = _create_role(world, owner, "policy_keepers", ["security_policy:manage", "project:read"])
    assert made.status_code == 201, made.text
    with world.acting_as(world.ctx(admin)):
        refused = client.delete(f"/v1/identity/access-roles/{made.json()['id']}")
    assert refused.status_code == 403 and "security_policy:manage" in refused.json().get("missing", []), \
        refused.text
    with world.acting_as(world.ctx(owner)):
        assert client.delete(f"/v1/identity/access-roles/{made.json()['id']}").status_code == 204


# ---- 2b: org-only roles at a project -----------------------------------------------------------------

def _bindings_of(world, subject_id):
    with world.engine.connect() as db:
        return db.execute(text("SELECT role_id, scope_type FROM identity_role_bindings WHERE subject_id = :s"),
                          {"s": subject_id}).all()


@pytest.mark.parametrize("role_id", ["arl_owner", "arl_auditor"])
def test_owner_and_auditor_cannot_be_bound_at_a_project(world, role_id):
    owner, dana = _owner(world), world.person("dana")
    refused = _bind(world, owner, dana.id, role_id, project=world.project_a)
    control = _bind(world, owner, dana.id, "arl_viewer", project=world.project_a)
    assert refused.status_code == 422 and refused.json()["error"] == "scope_not_bindable", refused.text
    assert control.status_code == 201, control.text
    assert _bindings_of(world, dana.id) == [("arl_viewer", "project")]


ORG_ONLY = sorted(k for k, m in PERMISSION_META.items() if m.org_only and not m.system_only
                  and not m.critical)[:4]


def test_a_custom_role_of_only_org_permissions_cannot_be_bound_at_a_project(world):
    owner, erin = _owner(world), world.person("erin")
    assert ORG_ONLY and all(PERMISSION_META[k].org_only for k in ORG_ONLY)
    # Made project-bindable: refused at creation (A3 — it would do nothing there).
    project_bindable = _create_role(world, owner, "org_readers_any", ORG_ONLY)
    assert project_bindable.status_code == 422 and project_bindable.json()["error"] == "org_only_role"
    # Made org-only: created, bindable org-wide, refused at a project.
    org_role = _create_role(world, owner, "org_readers", ORG_ONLY, bindable_scopes=["org"])
    assert org_role.status_code == 201, org_role.text
    at_project = _bind(world, owner, erin.id, org_role.json()["id"], project=world.project_a)
    org_wide = _bind(world, owner, erin.id, org_role.json()["id"])
    assert at_project.status_code == 422 and at_project.json()["error"] == "scope_not_bindable"
    assert org_wide.status_code == 201, org_wide.text
    # Editing a project-bindable role down to only org permissions is refused too.
    mixed = _create_role(world, owner, "mixed", ORG_ONLY + ["code_task:read"])
    assert mixed.status_code == 201, mixed.text
    with world.acting_as(world.ctx(owner)):
        narrowed = client.patch(f"/v1/identity/access-roles/{mixed.json()['id']}",
                                json={"permissions": ORG_ONLY})
    assert narrowed.status_code == 422 and narrowed.json()["error"] == "org_only_role", narrowed.text


def test_the_binding_api_itself_refuses_an_org_only_role_at_a_project(world):
    owner, finn = _owner(world), world.person("finn")
    stored = world.custom_role("imported_org_only", ORG_ONLY)   # bindable_scopes {org, project}
    res = _bind(world, owner, finn.id, stored, project=world.project_a)
    assert res.status_code == 422, res.text
    assert _bindings_of(world, finn.id) == []


def test_the_database_refuses_an_org_only_role_bound_at_a_project(world, engine):
    """R2 #8, the backstop: any writer (an import, a script, a future code path) that inserts a
    project binding of a role of only org-wide permissions is refused by the database itself."""
    import sqlalchemy

    finn = world.person("finn-sql")
    stored = world.custom_role("sql_org_only", ORG_ONLY)
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="identity_org_only_role"):
        world.bind(finn, stored, project=world.project_a)
    assert _bindings_of(world, finn.id) == []
    # org-wide it binds, and a role with any project permission still binds at a project
    world.bind(finn, stored)
    mixed = world.custom_role("sql_mixed", ORG_ONLY + ["code_task:read"])
    world.bind(finn, mixed, project=world.project_a)
