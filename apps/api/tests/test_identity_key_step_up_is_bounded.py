"""Two authz rules settled with FX-A (FX-B is the authz owner):

1. A service account's own key may use step-up permissions within its scopes (IDENTITY.md §4.6,
   FX-A) — but it is BOUNDED: no key, of any kind, ever uses an identity-administration
   permission (who exists, who may do what, credentials, sessions, Passports, delegation,
   providers, the policy, the audit export, approval rules, owner:assign) without a person's fresh
   second factor; none of those is delegable to an agent; and a critical allow is audited.
2. An org's MIGRATION roles (legacy_<hash>, legacy_bridge) are `system` only so nobody edits or
   binds them by hand: a binding of one grants what it holds (a legacy service account keeps its
   member's reach until the cutoff; the network bridge is "Owner minus identity writes" before the
   claim). The catalogue's FIXED system roles (system_worker, …) never grant through a binding.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz
from boss_engine.identity.authz import KEY_NEVER_STEPS_UP
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.principal import AuthContext, AuthMethod, CredentialRef, PrincipalKind
from boss_engine.identity.scope import ResolvedScope

IDENTITY_WRITES = ("owner:assign", "role_binding:assign", "access_role:manage", "user:invite",
                   "user:update", "user:disable", "user:delete", "user:reset_mfa", "group:manage",
                   "group:manage_members", "service_account:manage", "service_account_key:manage",
                   "api_key:revoke", "identity_provider:manage", "security_policy:manage",
                   "agent_passport:manage", "delegation:grant", "approval_rule:manage")


def test_the_bound_covers_every_identity_write_and_none_of_them_is_delegable():
    assert set(IDENTITY_WRITES) <= KEY_NEVER_STEPS_UP
    every_identity_step_up = {k for k, m in PERMISSION_META.items()
                              if m.step_up and m.category == "Identity and access"}
    assert every_identity_step_up <= KEY_NEVER_STEPS_UP
    assert not [k for k in KEY_NEVER_STEPS_UP if PERMISSION_META[k].delegable]


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _sak_ctx(world, sa, scopes):
    key_id = f"key_{uuid.uuid4().hex[:8]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
                        "created_by) VALUES (:id, :p, 'sak', 'ci', :h, :s, 'test')"),
                   {"id": key_id, "p": sa.id, "h": uuid.uuid4().hex, "s": list(scopes)})
    cred = CredentialRef(id=key_id, kind="sak", principal_id=sa.id, scopes=frozenset(scopes))
    return AuthContext(sa, AuthMethod.API_KEY, credential=cred)


def test_a_service_account_key_never_uses_an_identity_write_however_it_is_bound(world):
    sa = world.principal("ci-bot", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(sa, "owner")
    ctx = _sak_ctx(world, sa, sorted(PERMISSION_META))
    org = ResolvedScope.org_level(world.org)
    for key in IDENTITY_WRITES:
        scope = org if PERMISSION_META[key].scope == "org" else ResolvedScope.in_project(world.org, world.project_a)
        decision = authz.check(ctx, key, scope)
        assert not decision.allowed and decision.error_body().get("error") == "step_up_required", key


def test_a_critical_allow_is_audited(world):
    owner = world.person("crit-owner")
    world.bind(owner, "owner")
    with world.engine.begin() as db:
        before = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                                 "event_type = 'authz.allowed_critical'"), {"o": world.org}).scalar()
    assert authz.check(world.ctx(owner), "role_binding:assign", ResolvedScope.org_level(world.org)).allowed
    with world.engine.begin() as db:
        after = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                                "event_type = 'authz.allowed_critical'"), {"o": world.org}).scalar()
    assert after == before + 1


def _system_role(world, key, permissions, *, org_scoped: bool):
    rid = f"arl_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_access_roles (id, org_id, key, name, builtin, system, "
                        "bindable_scopes) VALUES (:id, :o, :k, :k, false, true, ARRAY['org','project'])"),
                   {"id": rid, "o": world.org if org_scoped else None, "k": f"{key}_{rid[-6:]}"})
        for p in permissions:
            db.execute(text("INSERT INTO identity_access_role_permissions (role_id, permission_key) "
                            "VALUES (:r, :p)"), {"r": rid, "p": p})
    return rid


def test_a_migration_role_grants_what_it_holds_and_a_fixed_system_role_grants_nothing(world):
    org = ResolvedScope.org_level(world.org)
    legacy_sa = world.principal("legacy-keys", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(legacy_sa, _system_role(world, "legacy", ("code_task:read", "project:read"), org_scoped=True))
    assert "code_task:read" in authz.effective_permissions(world.ctx(legacy_sa), org).permissions
    person = world.person("fixed-system")
    world.bind(person, _system_role(world, "system_fake", ("code_task:read",), org_scoped=False))
    assert "code_task:read" not in authz.effective_permissions(world.ctx(person), org).permissions


def test_nobody_binds_a_migration_role_by_hand(world):
    from boss_engine.identity import bindings
    from boss_engine.identity.roles import AccessError

    owner, target = world.person("bind-owner"), world.person("bind-target")
    world.bind(owner, "owner")
    rid = _system_role(world, "legacy", ("code_task:read",), org_scoped=True)
    with world.engine.begin() as db, pytest.raises(AccessError) as refused:
        bindings.add_binding(db, world.ctx(owner), subject_type="principal", subject_id=target.id,
                             role_id=rid, scope_type="org")
    assert refused.value.status == 404
