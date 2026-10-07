"""Grant limits and last-Owner rules over HTTP (IDENTITY.md §6.11, §6.12; FX-D P1-21).

  * 7c  Raising an agent's Passport ceiling: a caller who does not hold every permission of the
        new ceiling is refused 403 naming it; one who does succeeds; an agent token never can.
  * 8d  A network-bound (trusted-network bridge) Owner does not count as an Owner remaining:
        with one real Owner and the network principal holding owner, demoting or disabling the
        real Owner is refused as last-Owner — by the API and by the database.
  * 9c  Removing a binding needs ⊇ the role: an Admin cannot remove an Owner's binding (even with
        another Owner left), nor a custom role's binding holding a permission the Admin lacks.
  * 6e  A project-limited API key, over HTTP through the guard: its scopes in its projects only.
  * 8b  Deleting the last Owner.
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
    place_objects,
    report,
    role_perms,
)
from boss_engine.identity import users
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity.scope import ScopeSource

client = TestClient(app)


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _ceiling(world, agent_id):
    with world.engine.connect() as db:
        return sorted(r[0] for r in db.execute(text(
            "SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"), {"a": agent_id}))


# ---- 7c: the Passport ceiling --------------------------------------------------------------------------

PASSPORT_PATHS = ["/v1/identity/agents/{id}/passport", "/v1/identity/agents/{id}"]
HELD, EXTRA = "code_task:read", "code_task:create"


@pytest.mark.parametrize("path", PASSPORT_PATHS)
def test_raising_a_ceiling_needs_every_permission_of_the_new_ceiling(world, path):
    assert PERMISSION_META[HELD].delegable and PERMISSION_META[EXTRA].delegable
    keeper_role = world.custom_role("passport_keeper",
                                    ["agent_passport:manage", "agent_passport:read", HELD])
    keeper, owner = world.person("keeper"), world.person("owner")
    world.bind(keeper, keeper_role)
    world.bind(owner, "owner")
    agent = world.agent("forge", ceiling=(HELD,))
    url = path.format(id=agent.id)
    with world.acting_as(world.ctx(keeper)):
        raised = client.patch(url, json={"ceiling": [HELD, EXTRA]})
        same = client.patch(url, json={"ceiling": [HELD]})
    assert raised.status_code == 403, raised.text
    assert raised.json().get("permission") == EXTRA, raised.text
    assert same.status_code == 200, same.text
    assert _ceiling(world, agent.id) == [HELD]
    with world.acting_as(world.ctx(owner)):
        by_owner = client.patch(url, json={"ceiling": [HELD, EXTRA]})
    assert by_owner.status_code == 200, by_owner.text
    assert _ceiling(world, agent.id) == sorted([HELD, EXTRA])
    # Lowering needs nothing beyond the permission itself: the keeper may take EXTRA away again.
    with world.acting_as(world.ctx(keeper)):
        lowered = client.patch(url, json={"ceiling": [HELD]})
    assert lowered.status_code == 200 and _ceiling(world, agent.id) == [HELD]


def test_an_agent_token_can_never_change_a_passport_ceiling(world):
    owner = world.person("owner")
    world.bind(owner, "owner")
    agent = world.agent("forge", ceiling=(HELD, "agent_passport:read"))
    run = world.run_ctx(owner, (agent,), grant_permissions=(HELD, "agent_passport:read"),
                        token_scope=(HELD, "agent_passport:read", "agent_passport:manage"))
    with world.acting_as(run):
        for path in PASSPORT_PATHS:
            res = client.patch(path.format(id=agent.id), json={"ceiling": [HELD]})
            assert res.status_code == 403, (path, res.text)
            res = client.patch(path.format(id=agent.id), json={"ceiling": []})
            assert res.status_code == 403, (path, res.text)
    assert _ceiling(world, agent.id) == sorted([HELD, "agent_passport:read"])


# ---- 8d: a network-bound Owner is not an Owner remaining ------------------------------------------------

def _real_person(world, name):
    with world.engine.begin() as db:
        pid = users.create_user(db, org_id=world.org, email=f"{name}-{world.org}@example.test",
                                display_name=name, status="active", email_verified=True, created_by="test")
    return Principal(pid, PrincipalKind.USER, world.org, PrincipalStatus.ACTIVE, name)


def _network_ctx(principal):
    return AuthContext(principal, AuthMethod.NETWORK, legacy="network")


def test_a_network_bound_owner_does_not_keep_the_real_owner_removable(world):
    real_owner = _real_person(world, "real-owner")
    real_binding = world.bind(real_owner, "owner")
    network = world.person("network-operator", network_bound=True)
    world.bind(network, "owner")
    world.claimed()
    with world.acting_as(_network_ctx(network)):
        demote = client.delete(f"/v1/identity/role-bindings/{real_binding}")
        disable = client.post(f"/v1/identity/users/{real_owner.id}/disable")
    assert demote.status_code == 409 and demote.json()["error"] == "last_owner", demote.text
    assert disable.status_code == 409 and disable.json()["error"] == "last_owner", disable.text
    with world.engine.connect() as db:
        assert db.execute(text("SELECT status FROM identity_principals WHERE id = :p"),
                          {"p": real_owner.id}).scalar() == "active"
    # ...and the database agrees: the trigger does not count the network principal either.
    with pytest.raises(Exception, match="identity_last_owner"):
        with world.engine.begin() as db:
            db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": real_binding})


def test_with_a_second_real_owner_the_same_demotion_is_allowed(world):
    """The control: the refusal above is the last-Owner rule, not the network caller."""
    real_owner = _real_person(world, "real-owner")
    real_binding = world.bind(real_owner, "owner")
    second = _real_person(world, "second-owner")
    world.bind(second, "owner")
    network = world.person("network-operator", network_bound=True)
    world.bind(network, "owner")
    world.claimed()
    with world.acting_as(_network_ctx(network)):
        demote = client.delete(f"/v1/identity/role-bindings/{real_binding}")
    assert demote.status_code == 204, demote.text


# ---- 9c: removing a binding needs ⊇ the role ---------------------------------------------------------------

def test_an_admin_cannot_remove_an_owners_binding_even_with_another_owner_left(world):
    first, second, admin, op = (world.person(n) for n in ("first", "second", "admin", "op"))
    first_binding = world.bind(first, "owner")
    world.bind(second, "owner")
    world.bind(admin, "admin")
    op_binding = world.bind(op, "operator")
    world.claimed()
    with world.acting_as(world.ctx(admin)):
        refused = client.delete(f"/v1/identity/role-bindings/{first_binding}")
        control = client.delete(f"/v1/identity/role-bindings/{op_binding}")
    assert refused.status_code == 403 and refused.json().get("permission") == "owner:assign", refused.text
    assert control.status_code == 204, control.text
    with world.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE id = :b"),
                          {"b": first_binding}).scalar() == 1


def test_an_admin_cannot_remove_a_binding_of_a_role_holding_more_than_they_do(world):
    admin, carol = world.person("admin"), world.person("carol")
    world.bind(admin, "admin")
    stronger = world.custom_role("policy_keeper", ["security_policy:manage", "project:read"])
    binding = world.bind(carol, stronger)
    with world.acting_as(world.ctx(admin)):
        refused = client.delete(f"/v1/identity/role-bindings/{binding}")
    assert refused.status_code == 403, refused.text
    assert "security_policy:manage" in refused.json().get("missing", []), refused.text


# ---- 6e: a project-limited key over HTTP ---------------------------------------------------------------------

def _key_expect(access, project, limited_to):
    meta = PERMISSION_META[access.permission]
    if meta.system_only:
        return 403
    if access.scope.source == ScopeSource.ORG:
        if not meta.org_only:
            return 403            # a project-limited key names no project at an org check
    elif meta.org_only:
        return 403                # A3: org-only at a project
    elif project not in limited_to:
        return 403                # outside the key's projects (OUT_OF_SCOPE, never 404 to the owner)
    return 401 if meta.step_up else 200


def test_a_project_limited_key_holds_its_scopes_in_its_projects_only(world, monkeypatch):
    mirror = TestClient(identity_matrix.stub_app(app))
    owner = world.person("owner")
    world.bind(owner, "owner")
    key = world.key_ctx(owner, tuple(sorted(role_perms("owner"))), project_ids=(world.project_a,))
    wrong = []
    with world.acting_as(key):
        for project in (world.project_a, world.project_b):
            place_objects(monkeypatch, world.org, project)
            for method, path, access, _ in PROJECT_OPS + OBJECT_OPS + (ORG_OPS if project == world.project_a else []):
                want = _key_expect(access, project, {world.project_a})
                got = mirror.request(**identity_matrix.request_for(method, path, access, project))
                if got.status_code != want:
                    wrong.append(f"{method} {path} in {project[:2]} [{access.permission}]: want {want} "
                                 f"got {got.status_code} {got.text[:80]}")
    assert not wrong, report(wrong, "project-limited key")


# ---- 8b: deleting the last Owner -----------------------------------------------------------------------------

def test_the_last_owner_cannot_be_deleted_and_a_former_owner_can(world):
    only = _real_person(world, "only-owner")
    world.bind(only, "owner")
    world.claimed()
    with world.acting_as(world.ctx(only)):
        own = client.delete(f"/v1/identity/users/{only.id}")
    assert own.status_code == 409, own.text           # active: disable first; and disabling is refused
    with world.acting_as(world.ctx(only)):
        disable = client.post(f"/v1/identity/users/{only.id}/disable")
    assert disable.status_code == 409 and disable.json()["error"] == "last_owner", disable.text
    # A disabled former Owner is deletable while a real Owner remains (the control).
    former = _real_person(world, "former-owner")
    world.bind(former, "owner")
    with world.acting_as(world.ctx(only)):
        assert client.post(f"/v1/identity/users/{former.id}/disable").status_code == 200
        deleted = client.delete(f"/v1/identity/users/{former.id}")
    assert deleted.status_code == 200, deleted.text
    with world.engine.connect() as db:
        owners = db.execute(text("SELECT count(*) FROM identity_role_bindings b JOIN identity_principals p "
                                 "ON p.id = b.subject_id WHERE b.org_id = :o AND b.role_id = 'arl_owner' "
                                 "AND p.status = 'active'"), {"o": world.org}).scalar()
    assert owners == 1
