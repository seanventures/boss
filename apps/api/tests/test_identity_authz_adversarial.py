"""A31–A39 (IDENTITY.md §15.3, Authorization), through the real API and the real enforcement point.

Each test is written so that removing the protection it names makes it fail: the request that
must be refused is refused for THAT reason (the error body names it), and the neighbouring request
that differs only in the protected detail succeeds.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz
from boss_engine.identity.bindings import remove_binding
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity.roles import AccessError
from boss_engine.identity.scope import ResolvedScope

client = TestClient(app)


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def bindings_of(world: IdentityWorld, principal_id: str) -> list[tuple[str, str]]:
    with world.engine.connect() as db:
        return [(r[0], r[1]) for r in db.execute(text(
            "SELECT role_id, scope_type FROM identity_role_bindings WHERE subject_id = :p"),
            {"p": principal_id}).all()]


def test_a31_an_admin_assigning_owner_is_refused(world):
    admin, bob = world.person("admin"), world.person("bob")
    world.bind(admin, "admin")
    with world.acting_as(world.ctx(admin)):
        refused = client.post("/v1/identity/role-bindings",
                              json={"subject_type": "principal", "subject_id": bob.id, "role_id": "arl_owner"})
        allowed = client.post("/v1/identity/role-bindings",
                              json={"subject_type": "principal", "subject_id": bob.id, "role_id": "arl_operator"})
    assert refused.status_code == 403 and refused.json()["permission"] == "owner:assign"
    assert allowed.status_code == 201, allowed.text
    assert bindings_of(world, bob.id) == [("arl_operator", "org")]


def test_a32_a_custom_role_holding_owner_assign_created_by_an_admin_is_refused(world):
    admin = world.person("admin")
    world.bind(admin, "admin")
    with world.acting_as(world.ctx(admin)):
        refused = client.post("/v1/identity/access-roles", json={
            "key": "kingmaker", "name": "Kingmaker", "permissions": ["owner:assign", "project:read"],
            "confirm_critical": ["owner:assign"]})
        allowed = client.post("/v1/identity/access-roles", json={
            "key": "readers", "name": "Readers", "permissions": ["project:read", "code_task:read"]})
    assert refused.status_code == 403
    assert refused.json()["reason"] == "grant_ceiling" and refused.json()["missing"] == ["owner:assign"]
    assert allowed.status_code == 201, allowed.text


def test_a33_nobody_edits_their_own_bindings(world):
    admin, other = world.person("admin"), world.person("other")
    world.bind(admin, "admin")
    with world.acting_as(world.ctx(admin)):
        self_grant = client.post("/v1/identity/role-bindings", json={
            "subject_type": "principal", "subject_id": admin.id, "role_id": "arl_viewer"})
        other_grant = client.post("/v1/identity/role-bindings", json={
            "subject_type": "principal", "subject_id": other.id, "role_id": "arl_viewer"})
    assert self_grant.status_code == 403 and self_grant.json()["reason"] == "self_binding"
    assert other_grant.status_code == 201


def test_a34_adding_yourself_to_a_group_whose_roles_exceed_yours_is_refused(world):
    admin = world.person("admin")
    world.bind(admin, "admin")
    owners = world.group("owners")
    world.bind(owners, "owner", subject_type="group")
    operators = world.group("operators")
    world.bind(operators, "operator", subject_type="group")
    with world.acting_as(world.ctx(admin)):
        refused = client.post(f"/v1/identity/groups/{owners}/members", json={"principal_id": admin.id})
        allowed = client.post(f"/v1/identity/groups/{operators}/members", json={"principal_id": admin.id})
    assert refused.status_code == 403 and refused.json()["reason"] == "grant_ceiling"
    assert "owner:assign" in refused.json()["missing"]
    assert allowed.status_code == 200, allowed.text
    held = authz.effective_permissions(admin, ResolvedScope.org_level(world.org)).permissions
    assert "owner:assign" not in held


def test_a35_editing_a_bound_custom_role_to_add_a_permission_you_lack_is_refused(world):
    owner, admin, carol = world.person("owner"), world.person("admin"), world.person("carol")
    world.bind(owner, "owner")
    world.bind(admin, "admin")
    with world.acting_as(world.ctx(owner)):
        made = client.post("/v1/identity/access-roles", json={
            "key": "shippers", "name": "Shippers", "permissions": ["project:read"]})
        assert made.status_code == 201, made.text
        role_id = made.json()["id"]
        bound = client.post("/v1/identity/role-bindings", json={
            "subject_type": "principal", "subject_id": carol.id, "role_id": role_id})
        assert bound.status_code == 201
    with world.acting_as(world.ctx(admin)):
        refused = client.patch(f"/v1/identity/access-roles/{role_id}", json={
            "permissions": ["project:read", "user:delete"], "confirm_critical": ["user:delete"]})
        allowed = client.patch(f"/v1/identity/access-roles/{role_id}", json={
            "permissions": ["project:read", "code_task:read"]})
    assert refused.status_code == 403 and refused.json()["missing"] == ["user:delete"]
    assert allowed.status_code == 200 and allowed.json()["permissions"] == ["code_task:read", "project:read"]
    held = authz.effective_permissions(carol, ResolvedScope.in_project(world.org, world.project_a)).permissions
    assert "user:delete" not in held and "code_task:read" in held


@pytest.mark.parametrize("field, value", [("role", "owner"), ("org_id", "org_evil"),
                                          ("status", "disabled"), ("kind", "system")])
def test_a36_a_patch_naming_a_field_it_may_not_set_is_422_and_changes_nothing(world, field, value):
    owner = world.person("owner")
    world.bind(owner, "owner")
    with world.acting_as(world.ctx(owner)):
        role = client.post("/v1/identity/access-roles", json={
            "key": f"r{abs(hash(field)) % 10_000}", "name": "R", "permissions": ["project:read"]}).json()
        group = world.group(f"g-{field}")
        role_patch = client.patch(f"/v1/identity/access-roles/{role['id']}",
                                  json={"name": "Renamed", field: value})
        group_patch = client.patch(f"/v1/identity/groups/{group}", json={"name": "Renamed", field: value})
        after = client.get(f"/v1/identity/access-roles/{role['id']}").json()
    assert role_patch.status_code == 422 and group_patch.status_code == 422
    assert after["name"] == "R" and after["version"] == role["version"]
    with world.engine.connect() as db:
        assert db.execute(text("SELECT name FROM identity_groups WHERE id = :g"), {"g": group}).scalar() == f"g-{field}"


def test_a37_after_a_demotion_the_next_request_is_denied(world):
    op = world.person("op")
    binding = world.bind(op, "operator")
    route = f"/v1/identity/projects/{world.project_a}/role-bindings"
    viewer_route = "/v1/identity/catalogue"
    with world.acting_as(world.ctx(op)):
        assert client.get(f"/v1/code-tasks?project_id={world.project_a}").status_code not in (401, 403)
        with world.engine.begin() as db:
            db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": binding})
            authz.bump_epoch(world.org, db=db, reason="test.demote")
        after = client.get(f"/v1/code-tasks?project_id={world.project_a}")
        assert client.get(viewer_route).status_code == 200      # still signed in, just holds nothing
        assert client.get(route).status_code == 403
    assert after.status_code == 403 and after.json() == {"error": "forbidden", "permission": "code_task:read"}


def test_a38_two_concurrent_owner_removals_leave_an_owner(world):
    first, second = world.person("first"), world.person("second")
    b1, b2 = world.bind(first, "owner"), world.bind(second, "owner")
    world.claimed()
    console = AuthContext(Principal("sys_legacy_console", PrincipalKind.SYSTEM, world.org,
                                    PrincipalStatus.ACTIVE, "legacy console"), AuthMethod.LEGACY_API_KEY)
    barrier, outcomes = threading.Barrier(2), []

    def remove(binding_id: str) -> None:
        barrier.wait()
        try:
            with world.engine.begin() as db:
                remove_binding(db, console, binding_id)
            outcomes.append("removed")
        except AccessError as error:
            outcomes.append(error.error)

    threads = [threading.Thread(target=remove, args=(b,)) for b in (b1, b2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["last_owner", "removed"]
    with world.engine.connect() as db:
        left = db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE org_id = :o "
                               "AND role_id = 'arl_owner'"), {"o": world.org}).scalar()
    assert left == 1


def test_a38_the_database_refuses_the_last_owner_even_around_the_api(world):
    only = world.person("only")
    binding = world.bind(only, "owner")
    world.claimed()
    with pytest.raises(Exception, match="identity_last_owner"):
        with world.engine.begin() as db:
            db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": binding})


def test_a38_an_owner_removed_through_the_api_needs_another_owner_first(world):
    owner, other = world.person("owner"), world.person("other")
    mine = world.bind(owner, "owner")
    theirs = world.bind(other, "owner")
    with world.acting_as(world.ctx(other)):
        assert client.delete(f"/v1/identity/role-bindings/{mine}").status_code == 204
    with world.acting_as(world.ctx(owner)):   # owner no longer holds owner:assign
        assert client.delete(f"/v1/identity/role-bindings/{theirs}").status_code == 403


def test_a39_an_agent_run_spawning_a_child_outside_allowed_children_is_denied(world):
    alice = world.person("alice")
    world.bind(alice, "operator")
    vera, rook = world.agent("vera"), world.agent("rook")
    forge = world.agent("forge", ceiling=("code_task:read",), can_spawn=True, allowed_children=(vera.id,))
    ctx = world.run_ctx(alice, (forge,), grant_permissions=("code_task:read",),
                        token_scope=("code_task:read",), project_id=world.project_a,
                        allow_subdelegation=True)
    assert authz.may_spawn(ctx, vera.id).allowed
    assert not authz.may_spawn(ctx, rook.id).allowed


def test_a_project_admin_manages_only_their_projects_bindings(world):
    padmin, dana = world.person("padmin"), world.person("dana")
    world.bind(padmin, "admin", project=world.project_a)
    with world.acting_as(world.ctx(padmin)):
        here = client.post(f"/v1/identity/projects/{world.project_a}/role-bindings", json={
            "subject_type": "principal", "subject_id": dana.id, "role_id": "arl_operator"})
        there = client.post(f"/v1/identity/projects/{world.project_b}/role-bindings", json={
            "subject_type": "principal", "subject_id": dana.id, "role_id": "arl_operator"})
        org_wide = client.post("/v1/identity/role-bindings", json={
            "subject_type": "principal", "subject_id": dana.id, "role_id": "arl_operator"})
        # an Admin role at a project carries only its project-scoped permissions there (A3)
        admin_here = client.post(f"/v1/identity/projects/{world.project_a}/role-bindings", json={
            "subject_type": "principal", "subject_id": dana.id, "role_id": "arl_admin"})
    assert here.status_code == 201, here.text
    assert there.status_code == 403 and org_wide.status_code == 403
    assert admin_here.status_code == 201, admin_here.text
    binding = here.json()["id"]
    with world.acting_as(world.ctx(padmin)):
        wrong = client.delete(f"/v1/identity/projects/{world.project_b}/role-bindings/{binding}")
        smuggled = client.delete(f"/v1/identity/projects/{world.project_a}/role-bindings/{binding}x")
    assert wrong.status_code == 403 and smuggled.status_code == 404


def test_built_in_roles_are_read_only_and_unknown_keys_are_refused(world):
    owner = world.person("owner")
    world.bind(owner, "owner")
    with world.acting_as(world.ctx(owner)):
        builtin = client.patch("/v1/identity/access-roles/arl_viewer", json={"permissions": ["project:read"]})
        unknown = client.post("/v1/identity/access-roles", json={
            "key": "typo", "name": "Typo", "permissions": ["workflow:*"]})
        system = client.post("/v1/identity/access-roles", json={
            "key": "machine", "name": "Machine", "permissions": ["worker:lease"]})
        critical = client.post("/v1/identity/access-roles", json={
            "key": "eraser", "name": "Eraser", "permissions": ["user:delete"],
            "bindable_scopes": ["org"]})
    assert builtin.status_code == 409 and builtin.json()["error"] == "builtin_read_only"
    assert unknown.status_code == 422 and unknown.json()["error"] == "unknown_permission"
    assert system.status_code == 422
    assert critical.status_code == 422 and critical.json()["critical"] == ["user:delete"]


def test_the_identity_admin_api_is_denied_to_a_viewer(world):
    viewer, bob = world.person("viewer"), world.person("bob")
    world.bind(viewer, "viewer")
    with world.acting_as(world.ctx(viewer)):
        assert client.get("/v1/identity/access-roles").status_code == 200
        assert client.get("/v1/identity/role-bindings").status_code == 403
        assert client.post("/v1/identity/role-bindings", json={
            "subject_type": "principal", "subject_id": bob.id, "role_id": "arl_viewer"}).status_code == 403
        assert client.get(f"/v1/identity/principals/{bob.id}/effective").status_code == 403
