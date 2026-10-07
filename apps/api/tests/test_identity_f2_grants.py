"""F2 authorization fixes (security test id/integration @9070c34c): granting and taking over.

C1  an invitation's groups are a grant: the ceiling runs at create AND at accept (with the
    inviter as they are then), and the binding ceiling is re-run at accept.
H4  every group-membership delete keeps an Owner: the deferred trigger on identity_group_members
    and the app checks in SCIM and OIDC group sync.
H6  any credential/MFA/email/reset/session action on a person needs actor ⊇ target.
M2  the service-account key ceiling compares per scope.
M3  nobody raises their own access through a group.
M4  a binding that expires leaves the warm cache at its expiry, with no write and no epoch bump.
LOW one Owner lock, expiring Owners are not "an Owner remains", one "claimed".

Each test fails without its fix: the request that must be refused is refused for that reason, and
the neighbouring one that differs only in the protected detail succeeds.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld, _id
from boss_engine.identity import api_keys, authz, bindings, groups, invitations, scim, social
from boss_engine.identity.principal import AuthContext, AuthMethod
from boss_engine.identity.roles import AccessError
from boss_engine.identity.scope import ResolvedScope

client = TestClient(app)


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


def holds(world: IdentityWorld, principal, permission: str, project=None) -> bool:
    scope = ResolvedScope.in_project(world.org, project) if project else ResolvedScope.org_level(world.org)
    with world.engine.connect() as db:
        return permission in authz.effective_permissions(principal, scope, db=db).permissions


def members(world: IdentityWorld, gid: str) -> set[str]:
    with world.engine.connect() as db:
        return {r[0] for r in db.execute(text("SELECT principal_id FROM identity_group_members "
                                              "WHERE group_id = :g"), {"g": gid})}


def bind_expiring(world: IdentityWorld, subject, role_id: str, expires_at: datetime) -> str:
    bid = _id("rbd")
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                        "scope_type, scope_id, granted_by, expires_at) VALUES (:id, :o, 'principal', :s, :r, "
                        "'org', :o, 'test', :e)"),
                   {"id": bid, "o": world.org, "s": subject.id, "r": role_id, "e": expires_at})
        authz.bump_epoch(world.org, db=db, reason="test")
    return bid


# ---- C1: invitation groups ---------------------------------------------------------------------------

def _owner_group_world(world):
    owner, admin = world.person("owner"), world.person("admin")
    world.bind(owner, "owner")
    world.bind(admin, "admin")
    founders = world.group("founders")
    world.bind(founders, "owner", subject_type="group")
    plain = world.group("readers")
    world.bind(plain, "viewer", subject_type="group")
    return owner, admin, founders, plain


def test_c1_an_admin_cannot_invite_into_an_owner_group_over_http(world):
    owner, admin, founders, plain = _owner_group_world(world)
    with world.acting_as(world.ctx(admin)):
        refused = client.post("/v1/identity/users", json={"email": f"x{_id('e')}@evil.test",
                                                          "group_ids": [founders]})
        allowed = client.post("/v1/identity/users", json={"email": f"y{_id('e')}@ok.test",
                                                          "group_ids": [plain]})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"] == "groups_exceed_inviter"
    assert "owner:assign" in refused.text
    assert allowed.status_code == 201, allowed.text


def test_c1_the_testers_poc_no_longer_yields_an_owner(world):
    """poc_invite_escalation.py, end to end at the module level: create is refused, so nobody
    is ever accepted into the Owner group."""
    owner, admin, founders, _plain = _owner_group_world(world)
    with pytest.raises(invitations.InvitationRefused) as exc:
        with world.engine.begin() as db:
            invitations.create(db, inviter=world.ctx(admin), email="attacker@evil.test",
                               bindings=[], group_ids=[founders])
    assert exc.value.code == "groups_exceed_inviter"
    assert members(world, founders) == set()


def _open_invitation(world, inviter, *, bindings_=(), group_ids=()):
    with world.engine.begin() as db:
        inv, _tok = invitations.create(db, inviter=world.ctx(inviter), email=f"i{_id('e')}@ok.test",
                                       bindings=list(bindings_), group_ids=list(group_ids))
    return inv


def _accept(world, inv):
    with world.engine.begin() as db:
        row = db.execute(text("SELECT * FROM identity_invitations WHERE id = :i"), {"i": inv.id}).first()
        return invitations.accept(db, invitations._row(row), display_name="New", password_hash="x",
                                  email_verified=True)


def test_c1_accept_reruns_the_group_ceiling_against_the_inviter_as_they_are_now(world):
    owner, admin, founders, plain = _owner_group_world(world)
    second = world.person("second-owner")
    obid = world.bind(second, "owner")
    inv = _open_invitation(world, second, group_ids=[founders])     # fine when made
    with world.engine.begin() as db:                                  # then the inviter is demoted
        db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": obid})
        authz.bump_epoch(world.org, db=db, reason="test")
    with pytest.raises(invitations.InvitationRefused) as exc:
        _accept(world, inv)
    assert exc.value.code == "groups_exceed_inviter"
    with world.engine.connect() as db:                                # nothing was written
        assert db.execute(text("SELECT accepted_at FROM identity_invitations WHERE id = :i"),
                          {"i": inv.id}).scalar() is None
    assert members(world, founders) == set()
    ok = _open_invitation(world, owner, group_ids=[founders])        # a live Owner's invitation works
    pid = _accept(world, ok)
    assert pid in members(world, founders)


def test_c1_accept_reruns_the_binding_ceiling_and_refuses_a_disabled_inviter(world):
    owner, admin, _founders, _plain = _owner_group_world(world)
    demoted = world.person("demoted")
    abid = world.bind(demoted, "admin")
    inv = _open_invitation(world, demoted, bindings_=[{"role_key": "operator", "scope_type": "org"}])
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": abid})
        authz.bump_epoch(world.org, db=db, reason="test")
    with pytest.raises(invitations.InvitationRefused) as exc:
        _accept(world, inv)
    assert exc.value.code == "roles_exceed_inviter"

    disabled = world.person("gone")
    world.bind(disabled, "admin")
    inv2 = _open_invitation(world, disabled, bindings_=[{"role_key": "viewer", "scope_type": "org"}])
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": disabled.id})
    with pytest.raises(invitations.InvitationRefused) as exc2:
        _accept(world, inv2)
    assert exc2.value.code == "invitation_invalid"


# ---- H4: last Owner on every membership delete ---------------------------------------------------------

def _owner_only_through_group(world):
    alice = world.person("alice")
    gid = world.group("owners", members=(alice,), source="scim")
    world.bind(gid, "owner", subject_type="group")
    return alice, gid


def test_h4_the_trigger_refuses_deleting_the_last_owners_group_membership(world):
    alice, gid = _owner_only_through_group(world)
    with pytest.raises(Exception, match="identity_last_owner"):
        with world.engine.begin() as db:
            db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g"), {"g": gid})
    bob = world.person("bob")
    world.bind(bob, "owner")
    with world.engine.begin() as db:                                  # another Owner: allowed
        db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g"), {"g": gid})
    assert members(world, gid) == set()


def test_h4_scim_replace_and_patch_cannot_remove_the_last_owner(world):
    alice, gid = _owner_only_through_group(world)
    sys_ctx = world.ctx(world.person("scim"), method=AuthMethod.API_KEY)
    for change in (lambda db: scim.replace_group(db, sys_ctx, gid, {"members": []}),
                   lambda db: scim.patch_group(db, sys_ctx, gid, {"Operations": [
                       {"op": "remove", "path": f'members[value eq "{alice.id}"]'}]}),
                   lambda db: scim.patch_group(db, sys_ctx, gid, {"Operations": [
                       {"op": "remove", "path": "members"}]}),
                   lambda db: scim.delete_group(db, sys_ctx, gid)):
        with pytest.raises(scim.ScimError) as exc:
            with world.engine.begin() as db:
                change(db)
        assert exc.value.status == 409
    assert members(world, gid) == {alice.id}


def test_h4_scim_cannot_deactivate_the_last_owner(world):
    alice = world.person("alice")
    world.bind(alice, "owner")
    sys_ctx = world.ctx(world.person("scim"), method=AuthMethod.API_KEY)
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES (:p, :e)"),
                   {"p": alice.id, "e": f"{alice.id}@t.test"})
        row = scim._user_row(db, world.org, alice.id)
    with pytest.raises(scim.ScimError) as exc:
        with world.engine.begin() as db:
            scim._set_active(db, sys_ctx, row, False)
    assert exc.value.status == 409


def test_h4_oidc_group_sync_keeps_the_last_owners_membership(world):
    alice = world.person("alice")
    gid = world.group("idp-owners", members=(), source="oidc")
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source) "
                        "VALUES (:g, :p, 'oidc')"), {"g": gid, "p": alice.id})
    world.bind(gid, "owner", subject_type="group")

    class _P:
        id, org_id, groups_claim = _id("idp"), world.org, "groups"

    class _I:
        groups: list = []

    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_providers (id, org_id, protocol, name, issuer, client_id, groups_claim) "
                        "VALUES (:i, :o, 'oidc', 'idp', :iss, 'c', 'groups')"),
                   {"i": _P.id, "o": world.org, "iss": f"https://{_P.id}.test"})
        db.execute(text("INSERT INTO identity_provider_group_mappings (provider_id, external_group, group_id) "
                        "VALUES (:p, 'owners', :g)"), {"p": _P.id, "g": gid})
    with world.engine.begin() as db:                                  # commits: no trigger failure
        social.sync_groups(db, principal_id=alice.id, provider=_P, ident=_I)
    assert members(world, gid) == {alice.id}


def test_h4_manual_group_removal_uses_the_same_guard(world):
    owner = world.person("owner")
    gid = world.group("owners", members=(owner,))
    world.bind(gid, "owner", subject_type="group")
    with pytest.raises(AccessError) as exc:
        with world.engine.begin() as db:
            groups.remove_member(db, world.ctx(owner), gid, owner.id)
    assert exc.value.error == "last_owner"


# ---- H6: actor ⊇ target ------------------------------------------------------------------------------

def test_h6_an_admin_cannot_take_over_a_more_privileged_non_owner(world):
    owner, admin = world.person("owner"), world.person("admin")
    world.bind(owner, "owner")
    world.bind(admin, "admin")
    vip = world.person("vip")
    vip_role = world.custom_role("vip", ("project:read", "owner:assign"))
    world.bind(vip, vip_role)
    peer = world.person("peer")
    world.bind(peer, "operator")
    with world.engine.begin() as db:
        for p in (vip, peer):
            db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES (:p, :e)"),
                       {"p": p.id, "e": f"{p.id}@t.test"})
    with world.acting_as(world.ctx(admin)):
        refused = {
            "reset-mfa": client.post(f"/v1/identity/users/{vip.id}/reset-mfa"),
            "reset-link": client.post(f"/v1/identity/users/{vip.id}/reset-link"),
            "email": client.patch(f"/v1/identity/users/{vip.id}", json={"email": "mine@evil.test"}),
            "sessions": client.post(f"/v1/identity/users/{vip.id}/revoke-sessions"),
            "disable": client.post(f"/v1/identity/users/{vip.id}/disable"),
        }
        allowed = client.post(f"/v1/identity/users/{peer.id}/reset-mfa")
    for name, r in refused.items():
        assert r.status_code == 403, (name, r.text)
        assert r.json().get("reason") == "target_exceeds_actor", (name, r.text)
    assert allowed.status_code == 200, allowed.text
    with world.engine.connect() as db:
        assert db.execute(text("SELECT email FROM identity_users WHERE principal_id = :p"),
                          {"p": vip.id}).scalar() == f"{vip.id}@t.test"


# ---- M2: per-scope key ceiling -----------------------------------------------------------------------

def test_m2_holding_a_permission_in_one_project_does_not_cover_an_org_wide_service_account(world):
    creator = world.person("creator")
    world.bind(creator, "admin", project=world.project_a)
    world.bind(creator, "viewer")
    sa = world.principal("bot", kind=__import__("boss_engine.identity.principal",
                                                fromlist=["PrincipalKind"]).PrincipalKind.SERVICE_ACCOUNT)
    world.bind(sa, "operator")                         # operator org-wide
    with pytest.raises(api_keys.KeyRefused) as exc:
        with world.engine.begin() as db:
            api_keys.create(db, creator=world.ctx(creator), owner=sa, kind="sak", name="k",
                            scopes=["project:read"], expires_in_days=30)
    assert exc.value.code == "owner_exceeds_creator"
    sa2 = world.principal("bot2", kind=sa.kind)
    world.bind(sa2, "operator", project=world.project_a)
    with world.engine.begin() as db:                   # same role, inside the creator's project: fine
        api_keys.create(db, creator=world.ctx(creator), owner=sa2, kind="sak", name="k",
                        scopes=["project:read"], expires_in_days=30)


# ---- M3: no self-raise via groups --------------------------------------------------------------------

def test_m3_making_your_expiring_access_permanent_through_a_group_is_refused(world):
    owner = world.person("owner")
    world.bind(owner, "owner")
    temp = world.person("temp-admin")
    bind_expiring(world, temp, "arl_admin", datetime.now(timezone.utc) + timedelta(hours=1))
    admins = world.group("admins")
    world.bind(admins, "operator", subject_type="group")
    with pytest.raises(AccessError) as exc:
        with world.engine.begin() as db:
            groups.add_member(db, world.ctx(temp), admins, temp.id)
    assert exc.value.extra.get("reason") == "self_binding"
    other = world.person("other")
    with world.engine.begin() as db:                   # the same group for someone else: fine
        groups.add_member(db, world.ctx(temp), admins, other.id)

    mine = world.group("mine", members=(temp,))
    with pytest.raises(AccessError) as exc2:
        with world.engine.begin() as db:
            bindings.add_binding(db, world.ctx(temp), subject_type="group", subject_id=mine,
                                 role_id="arl_operator", scope_type="org")
    assert exc2.value.extra.get("reason") == "self_binding"
    theirs = world.group("theirs", members=(other,))
    with world.engine.begin() as db:
        bindings.add_binding(db, world.ctx(temp), subject_type="group", subject_id=theirs,
                             role_id="arl_operator", scope_type="org")


# ---- M4: expiry by time --------------------------------------------------------------------------------

def test_m4_an_expired_binding_leaves_the_warm_cache_without_any_write(world):
    p = world.person("p")
    bind_expiring(world, p, "arl_operator", datetime.now(timezone.utc) + timedelta(seconds=2))
    assert holds(world, p, "code_task:create")               # cached now
    import time
    time.sleep(2.5)                                           # no write, no epoch bump
    assert not holds(world, p, "code_task:create")
    ctx = world.ctx(p)
    decision = authz.check(ctx, "code_task:create", ResolvedScope.in_project(world.org, world.project_a))
    assert not decision.allowed


# ---- LOW: expiring Owners, one lock, one "claimed" -----------------------------------------------------

def test_an_expiring_owner_binding_does_not_count_as_an_owner_remaining(world):
    a, b = world.person("a"), world.person("b")
    abid = world.bind(a, "owner")
    bind_expiring(world, b, "arl_owner", datetime.now(timezone.utc) + timedelta(days=1))
    with world.engine.connect() as db:
        assert bindings.active_owners(db, world.org, without_binding=abid) == set()
        assert b.id in bindings.owner_ids(db, world.org)      # still an Owner right now
    with pytest.raises(Exception, match="identity_last_owner"):
        with world.engine.begin() as db:
            db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": abid})


def test_every_last_owner_path_takes_the_one_owner_lock():
    from apps.api.app.routers import identity_people

    src = inspect.getsource(identity_people)
    assert "pg_advisory_xact_lock" not in src and "bindings.lock_owners" in src
    schema = open("db/schema.sql").read()
    assert "hashtext('identity_owner:' || v_org)" in schema
    assert bindings.OWNER_LOCK == "identity_owner:{org}"


def test_an_org_with_an_owner_is_claimed_by_every_definition(engine):
    from boss_engine.identity import claim, migration

    w = IdentityWorld.create(engine)                           # no bootstrap.claimed event
    with engine.connect() as db:
        assert not claim.is_claimed(db, w.org) and migration.claimed_at(db, w.org) is None
    owner = w.person("owner")
    w.bind(owner, "owner")
    with engine.connect() as db:
        assert claim.is_claimed(db, w.org) and migration.claimed_at(db, w.org) is not None
