"""The decision function (IDENTITY.md §6.6, §15.1 PDP unit tests), against the real schema.

Each test builds its own org (identity_fixtures.IdentityWorld) and asks authz.check / explain /
effective_permissions directly. Every protection here is asserted from both sides: the allow that
must happen and the deny that must happen when the protection is the only thing in the way.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz
from boss_engine.identity.authz import DenyReason
from boss_engine.identity.permissions_gen import PERMISSION_META, ROLE_PERMISSIONS
from boss_engine.identity.principal import (
    AuthContext,
    AuthMethod,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    RunMode,
)
from boss_engine.identity.scope import ResolvedScope


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def at(world: IdentityWorld, project: str | None = None, **obj) -> ResolvedScope:
    if project is None:
        return ResolvedScope.org_level(world.org)
    return ResolvedScope.in_project(world.org, project, **obj)


def test_an_org_binding_reaches_every_project_and_a_project_binding_only_its_own(world):
    org_op, proj_op = world.person("org-op"), world.person("proj-op")
    world.bind(org_op, "operator")
    world.bind(proj_op, "operator", project=world.project_a)
    for project in (world.project_a, world.project_b):
        assert authz.check(world.ctx(org_op), "code_task:create", at(world, project)).allowed
    assert authz.check(world.ctx(proj_op), "code_task:create", at(world, world.project_a)).allowed
    denied = authz.check(world.ctx(proj_op), "code_task:create", at(world, world.project_b))
    assert not denied.allowed and denied.reason == DenyReason.MISSING_PERMISSION
    # an org-level ("any project") check needs an org binding
    assert not authz.check(world.ctx(proj_op), "code_task:create", at(world)).allowed


def test_nothing_bound_is_nothing_held_deny_by_default(world):
    nobody = world.person("nobody")
    for key in ("project:read", "code_task:read", "directory:read"):
        scope = at(world) if PERMISSION_META[key].org_only else at(world, world.project_a)
        assert not authz.check(world.ctx(nobody), key, scope).allowed


def test_every_built_in_role_holds_exactly_its_catalogue_set(world):
    for role in ("owner", "admin", "operator", "viewer", "auditor"):
        person = world.person(role)
        world.bind(person, role)
        held = authz.effective_permissions(person, at(world)).permissions
        assert held == ROLE_PERMISSIONS[role], role


def test_amendment_a3_org_only_permissions_are_inert_in_a_project_binding(world):
    padmin = world.person("project-admin")
    world.bind(padmin, "admin", project=world.project_a)
    assert not authz.check(world.ctx(padmin), "user:invite", at(world)).allowed
    held = authz.effective_permissions(padmin, at(world, world.project_a)).permissions
    assert "user:invite" not in held and "code_task:override" in held
    refused = authz.check(world.ctx(padmin), "user:invite", at(world, world.project_a))
    assert refused.reason == DenyReason.ORG_ONLY_AT_PROJECT


def test_groups_grant_and_explain_names_the_path(world):
    alice = world.person("alice")
    builders = world.group("builders", members=(alice,))
    binding = world.bind(builders, "operator", project=world.project_a, subject_type="group")
    decision = authz.check(world.ctx(alice), "workflow:publish", at(world, world.project_a))
    assert decision.allowed
    (why,) = decision.via
    assert (why.role_key, why.binding_id, why.via_group) == ("operator", binding, "builders")
    assert str(why) == f"role operator ← group builders ← binding at project:{world.project_a}"
    assert authz.explain(alice.id, "workflow:publish", at(world, world.project_a)) == (why,)
    assert authz.explain(alice.id, "workflow:publish", at(world, world.project_b)) == ()


def test_an_inactive_principal_and_a_disabled_row_are_refused(world):
    bob = world.person("bob")
    world.bind(bob, "owner")
    assert authz.check(world.ctx(bob), "project:read", at(world, world.project_a)).allowed
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": bob.id})
    # the context still says active (an in-flight request): the database decides
    refused = authz.check(world.ctx(bob), "project:read", at(world, world.project_a))
    assert refused.reason == DenyReason.PRINCIPAL_INACTIVE and refused.status == 401
    gone = Principal(bob.id, PrincipalKind.USER, world.org, PrincipalStatus.DISABLED, "bob")
    assert authz.check(world.ctx(gone), "project:read", at(world, world.project_a)).status == 401


def test_the_org_comes_from_the_principal_another_orgs_scope_is_not_found(world, engine):
    other = IdentityWorld.create(engine)
    owner = world.person("owner")
    world.bind(owner, "owner")
    foreign = ResolvedScope.in_project(other.org, other.project_a)
    refused = authz.check(world.ctx(owner), "project:read", foreign)
    assert refused.reason == DenyReason.NOT_FOUND and refused.status == 404
    # a project id from another org, presented under our org, is not found either
    smuggled = ResolvedScope.in_project(world.org, other.project_a)
    assert authz.check(world.ctx(owner), "project:read", smuggled).reason == DenyReason.NOT_FOUND


def test_an_object_the_caller_cannot_read_is_404_one_it_can_read_is_403(world):
    viewer, stranger = world.person("viewer"), world.person("stranger")
    world.bind(viewer, "viewer", project=world.project_a)
    run = at(world, world.project_a, object_kind="workflow_run", object_id="wr_1")
    seen = authz.check(world.ctx(viewer), "workflow_run:control", run)
    assert seen.reason == DenyReason.MISSING_PERMISSION and seen.status == 403
    assert seen.error_body() == {"error": "forbidden", "permission": "workflow_run:control"}
    unseen = authz.check(world.ctx(stranger), "workflow_run:control", run)
    assert unseen.reason == DenyReason.NOT_FOUND and unseen.error_body() == {"error": "not_found"}


def test_step_up_needs_a_fresh_second_factor_and_a_key_never_has_one(world):
    admin = world.person("admin")
    world.bind(admin, "admin")
    key = "code_task:override"
    assert PERMISSION_META[key].step_up
    assert authz.check(world.ctx(admin, mfa=True), key, at(world, world.project_a)).allowed
    stale = authz.check(world.ctx(admin, mfa=False), key, at(world, world.project_a))
    assert stale.reason == DenyReason.STEP_UP_REQUIRED and stale.status == 401
    # A26: a PAT calling a step-up permission it holds -> 401 step_up_required
    pat = world.key_ctx(admin, (key,))
    assert authz.check(pat, key, at(world, world.project_a)).reason == DenyReason.STEP_UP_REQUIRED
    # and a permission it does not hold is a 403, not a step-up prompt (no probing by step-up)
    assert authz.check(world.ctx(world.person("x"), mfa=False), key,
                       at(world, world.project_a)).reason == DenyReason.MISSING_PERMISSION


def test_a_key_is_its_owners_live_set_narrowed_by_its_scopes(world):
    op = world.person("op")
    binding = world.bind(op, "operator")
    key = world.key_ctx(op, ("code_task:read", "user:invite"))
    assert authz.check(key, "code_task:read", at(world, world.project_a)).allowed
    narrowed = authz.check(key, "code_task:create", at(world, world.project_a))
    assert not narrowed.allowed and narrowed.narrowed_by == ("key_scopes",)
    # a scope the owner never held grants nothing
    assert not authz.check(key, "user:invite", at(world)).allowed
    # A30: demoting the owner shrinks the key on the very next check
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": binding})
        authz.bump_epoch(world.org, db=db, reason="test")
    assert not authz.check(key, "code_task:read", at(world, world.project_a)).allowed


def test_a_revoked_or_project_limited_key_is_refused(world):
    op = world.person("op")
    world.bind(op, "operator")
    revoked = world.key_ctx(op, ("code_task:read",), revoked=True)
    assert authz.check(revoked, "code_task:read", at(world, world.project_a)).reason == \
        DenyReason.CREDENTIAL_INVALID
    limited = world.key_ctx(op, ("code_task:read",), project_ids=(world.project_a,))
    assert authz.check(limited, "code_task:read", at(world, world.project_a)).allowed
    assert authz.check(limited, "code_task:read", at(world, world.project_b)).reason == \
        DenyReason.OUT_OF_SCOPE
    assert authz.check(limited, "code_task:read", at(world)).reason == DenyReason.OUT_OF_SCOPE


def test_system_principals_hold_exactly_their_fixed_sets(world):
    worker = Principal("sys_worker", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE, "worker")
    ctx = AuthContext(worker, AuthMethod.SYSTEM)
    assert authz.check(ctx, "worker:lease", at(world)).allowed
    assert not authz.check(ctx, "code_task:create", at(world, world.project_a)).allowed
    # no person holds a system-only permission, not even an Owner
    owner = world.person("owner")
    world.bind(owner, "owner")
    assert not authz.check(world.ctx(owner), "worker:lease", at(world)).allowed


def test_an_unknown_permission_is_refused_not_raised(world):
    owner = world.person("owner")
    world.bind(owner, "owner")
    assert authz.check(world.ctx(owner), "workflow:launch", at(world)).reason == \
        DenyReason.UNKNOWN_PERMISSION


def test_agent_runs_are_the_live_intersection_and_never_hold_human_only_keys(world):
    alice = world.person("alice")
    world.bind(alice, "operator")
    forge = world.agent("forge", ceiling=("code_task:read", "code_task:create", "workflow_run:read"))
    ctx = world.run_ctx(alice, (forge,), grant_permissions=("code_task:read", "code_task:create"),
                        token_scope=("code_task:read", "code_task:create", "workflow_run:read"),
                        project_id=world.project_a)
    a = at(world, world.project_a)
    assert authz.check(ctx, "code_task:create", a).allowed
    # ceiling and token allow it, the grant does not
    narrowed = authz.check(ctx, "workflow_run:read", a)
    assert not narrowed.allowed and any("grant" in n for n in narrowed.narrowed_by)
    # A49: non-delegable keys are refused to an agent, whatever anyone holds
    for key in ("decision:approve", "role_binding:assign", "delegation:grant", "deployment:production"):
        assert authz.check(ctx, key, a).reason == DenyReason.NOT_DELEGABLE
    # the task binding pins the run to its project
    assert authz.check(ctx, "code_task:read", at(world, world.project_b)).reason == DenyReason.OUT_OF_SCOPE
    # live: demote the person and the run loses it on the next call
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_role_bindings WHERE subject_id = :p"), {"p": alice.id})
        authz.bump_epoch(world.org, db=db, reason="test")
    assert not authz.check(ctx, "code_task:create", a).allowed


def test_a_suspended_agent_or_revoked_grant_or_token_stops_the_run(world):
    alice = world.person("alice")
    world.bind(alice, "operator")
    forge = world.agent("forge", ceiling=("code_task:read",))
    ctx = world.run_ctx(alice, (forge,), grant_permissions=("code_task:read",),
                        token_scope=("code_task:read",), project_id=world.project_a)
    a = at(world, world.project_a)
    assert authz.check(ctx, "code_task:read", a).allowed
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_delegation_grants SET status = 'revoked' WHERE id = :g"),
                   {"g": ctx.run_token.grant_id})
    assert authz.check(ctx, "code_task:read", a).reason == DenyReason.GRANT_INACTIVE
    ctx2 = world.run_ctx(alice, (forge,), grant_permissions=("code_task:read",),
                         token_scope=("code_task:read",), project_id=world.project_a)
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_run_credentials SET revoked_at = now() WHERE jti = :j"),
                   {"j": ctx2.run_token.jti})
    assert authz.check(ctx2, "code_task:read", a).reason == DenyReason.CREDENTIAL_INVALID
    ctx3 = world.run_ctx(alice, (forge,), grant_permissions=("code_task:read",),
                         token_scope=("code_task:read",), project_id=world.project_a)
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'suspended' WHERE id = :a"), {"a": forge.id})
    assert authz.check(ctx3, "code_task:read", a).reason == DenyReason.CHAIN_INACTIVE


def test_every_agent_in_the_chain_narrows_not_only_the_current_one(world):
    alice = world.person("alice")
    world.bind(alice, "operator")
    atlas = world.agent("atlas", ceiling=("code_task:read",))
    forge = world.agent("forge", ceiling=("code_task:read", "code_task:create"))
    ctx = world.run_ctx(alice, (atlas, forge), grant_permissions=("code_task:read", "code_task:create"),
                        token_scope=("code_task:read", "code_task:create"), project_id=world.project_a)
    refused = authz.check(ctx, "code_task:create", at(world, world.project_a))
    assert not refused.allowed and any(atlas.id in n for n in refused.narrowed_by)
    assert not any(forge.id in n for n in refused.narrowed_by)


def test_matrix_agent_run_tokens_for_every_permission(world):
    """§15.5: for each permission x {delegable, not}, a run token whose chain's ceiling holds
    every delegable permission, acting for an Owner: allowed exactly when the key is delegable
    (and the Owner holds it); a non-delegable key is refused as not_delegable."""
    owner = world.person("owner")
    world.bind(owner, "owner")
    delegable = tuple(sorted(k for k, m in PERMISSION_META.items() if m.delegable))
    agent = world.agent("all-delegable", ceiling=delegable)
    ctx = world.run_ctx(owner, (agent,), grant_permissions=delegable,
                        token_scope=tuple(PERMISSION_META), project_id=None)
    wrong = []
    for key, meta in sorted(PERMISSION_META.items()):
        scope = (ResolvedScope.org_level(world.org) if meta.org_only or meta.system_only
                 else ResolvedScope.in_project(world.org, world.project_a))
        decision = authz.check(ctx, key, scope)
        expected = meta.delegable and key in ROLE_PERMISSIONS["owner"]
        if decision.allowed != expected or (not meta.delegable
                                            and decision.reason != DenyReason.NOT_DELEGABLE):
            wrong.append((key, decision.reason))
    assert not wrong, wrong
    assert len(delegable) > 30


def test_the_epoch_makes_a_binding_change_visible_on_the_next_decision(world):
    """A37 at the PDP: the cache is keyed by epoch, and the epoch is read on every decision."""
    op = world.person("op")
    binding = world.bind(op, "operator")
    a = at(world, world.project_a)
    assert authz.check(world.ctx(op), "code_task:create", a).allowed   # cached now
    cached_at = authz._epoch.read(world.org)                            # noqa: SLF001
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": binding})
        bumped = authz.bump_epoch(world.org, db=db, reason="test.demote")
    assert not authz.check(world.ctx(op), "code_task:create", a).allowed
    # The statement-level change-feed triggers (S5) bump the epoch too, so it is not an exact
    # count: what matters is that it moved past the cached decision's epoch.
    with world.engine.begin() as db:
        now = db.execute(text("SELECT epoch FROM identity_settings WHERE org_id = :o"),
                         {"o": world.org}).scalar()
    assert now >= bumped > cached_at


def test_an_expired_binding_grants_nothing(world):
    op = world.person("op")
    binding = world.bind(op, "operator")
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_role_bindings SET expires_at = :t WHERE id = :b"),
                   {"t": datetime.now(timezone.utc) - timedelta(seconds=1), "b": binding})
        authz.bump_epoch(world.org, db=db, reason="test")
    assert not authz.check(world.ctx(op), "code_task:read", at(world, world.project_a)).allowed


def test_may_spawn_only_allowed_children_a39(world):
    alice = world.person("alice")
    world.bind(alice, "operator")
    child_ok, child_no = world.agent("vera"), world.agent("rook")
    parent = world.agent("forge", ceiling=("code_task:read",), can_spawn=True,
                         allowed_children=(child_ok.id,))
    ctx = world.run_ctx(alice, (parent,), grant_permissions=("code_task:read",),
                        token_scope=("code_task:read",), project_id=world.project_a,
                        allow_subdelegation=True)
    assert authz.may_spawn(ctx, child_ok.id).allowed
    refused = authz.may_spawn(ctx, child_no.id)
    assert not refused.allowed and refused.narrowed_by == (f"passport:{parent.id}",)
    # the grant narrows too: subdelegation off -> no child at all
    no_sub = world.run_ctx(alice, (parent,), grant_permissions=("code_task:read",),
                           token_scope=("code_task:read",), project_id=world.project_a)
    assert not authz.may_spawn(no_sub, child_ok.id).allowed
    # a cycle (the parent again) is refused
    assert authz.may_spawn(ctx, parent.id).narrowed_by == ("cycle",)
    assert RunMode.OBO == ctx.run_token.mode


# ---- the seams to S5 (legacy bridges) and S3 (agent runs), through fakes of their interfaces ----

def test_legacy_system_principals_take_their_set_from_s5(world, monkeypatch):
    from boss_engine.identity import legacy

    monkeypatch.setattr(legacy, "system_permissions",
                        lambda pid, db=None: frozenset({"settings:read"}) if pid == "sys_legacy_x" else None,
                        raising=False)
    bridged = AuthContext(Principal("sys_legacy_x", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE,
                                    "legacy"), AuthMethod.LEGACY_API_KEY, legacy="env_api_key")
    assert authz.check(bridged, "settings:read", at(world, world.project_a)).allowed
    assert not authz.check(bridged, "settings:update", at(world, world.project_a)).allowed
    # a non-legacy system principal still uses the catalogue's fixed sets
    worker = AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE,
                                   "w"), AuthMethod.SYSTEM)
    assert authz.check(worker, "worker:lease", at(world)).allowed


def test_legacy_bridges_are_step_up_exempt_exactly_as_s5_says(world, monkeypatch):
    from boss_engine.identity import legacy

    op = world.person("bridged-operator")
    world.bind(op, "operator")
    bridged = AuthContext(op, AuthMethod.LEGACY_PASSWORD, legacy="shared_password")
    key = "workflow:publish"
    assert PERMISSION_META[key].step_up
    monkeypatch.setattr(legacy, "step_up_exempt", lambda ctx: False, raising=False)
    assert authz.check(bridged, key, at(world, world.project_a)).reason == DenyReason.STEP_UP_REQUIRED
    monkeypatch.setattr(legacy, "step_up_exempt", lambda ctx: bool(ctx.legacy) and ctx.session is None,
                        raising=False)
    assert authz.check(bridged, key, at(world, world.project_a)).allowed
    # a real session without a fresh factor is never exempt
    assert authz.check(world.ctx(op, mfa=False), key, at(world, world.project_a)).reason == \
        DenyReason.STEP_UP_REQUIRED


def test_agent_runs_are_decided_by_s3_and_people_are_not(world, monkeypatch):
    from boss_engine.identity import tokens

    calls = []

    def decide(claims, permission, scope, *, db):
        calls.append((claims.jti, permission))
        return authz.Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION, ("ceiling:agt_fake",))

    # authz delegates to the attribute it looks up on S3's (real, imported) module
    monkeypatch.setattr(tokens, "decide", decide)
    alice = world.person("alice")
    world.bind(alice, "operator")
    forge = world.agent("forge", ceiling=("code_task:read",))
    run = world.run_ctx(alice, (forge,), grant_permissions=("code_task:read",),
                        token_scope=("code_task:read",), project_id=world.project_a)
    refused = authz.check(run, "code_task:read", at(world, world.project_a))
    assert refused.narrowed_by == ("ceiling:agt_fake",) and calls == [(run.run_token.jti, "code_task:read")]
    # non-delegable keys never reach the token PDP; people never do
    assert authz.check(run, "decision:approve", at(world, world.project_a)).reason == DenyReason.NOT_DELEGABLE
    assert authz.check(world.ctx(alice), "code_task:read", at(world, world.project_a)).allowed
    assert len(calls) == 1
