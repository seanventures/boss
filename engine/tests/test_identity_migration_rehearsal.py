"""The migration rehearsal (IDENTITY.md §12, §15.2 J2/J14, IDENTITY_BUILD.md S5 "M1–M4").

Two installs shaped like today's, upgraded with NO env change:

  goldeneye  network-trusted console (built with BOSS_CONSOLE_AUTH=network; the API is NOT told),
             the console's BOSS_API_KEY bound to `ceo`, the worker key bound to `system`, the
             default members, agent roles. Ops go through the tailnet ops API; nobody reads logs.
  boss-1     a password install (BOSS_CONSOLE_PASSWORD), an extra MCP key bound to `cto`, an OIDC
             bearer caller, SCIM users/groups and a raw SCIM bearer token, break-glass grants.

Each walk: upgrade (nothing locks out) -> the claim works -> every legacy path keeps working in its
window, as the role the decisions give it -> each stops at its cutoff -> history stays intact and
the audit chain verifies.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.identity import audit, legacy, migration, posture, shadow_report
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.legacy import (
    LEGACY_CONSOLE_PRINCIPAL,
    LEGACY_SHARED_PRINCIPAL,
    LEGACY_WORKER_PRINCIPAL,
    NETWORK_OPERATOR_PRINCIPAL,
    LegacyKind,
)
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    CONSOLE_KEY,
    CONSOLE_PASSWORD,
    MCP_KEY,
    NOW,
    ORG,
    PROJECT,
    SCIM_TOKEN,
    WORKER_KEY,
    add_break_glass,
    add_scim,
    community_install,
    creds,
    db,
    events,
    fake_check,
    fake_claim,
    s5_engine,
    settings,
)

GOLDENEYE_WEB = "172.18.0.5"           # the console container: a trusted proxy
TAILNET_VISITOR = "100.101.102.103"


def goldeneye_settings(**extra):
    return settings(legacy_console_api_key=CONSOLE_KEY,
                    api_key_principal_map={CONSOLE_KEY: "ceo", WORKER_KEY: "system"},
                    trusted_proxies="127.0.0.0/8,::1/128,172.18.0.0/16", **extra)


def console_request():
    return creds(method="api_key", member="ceo", key=CONSOLE_KEY, ip=GOLDENEYE_WEB)


def worker_request():
    return creds(method="api_key", member="system", key=WORKER_KEY, ip="172.18.0.6")


def network_request(ip=TAILNET_VISITOR, peer=GOLDENEYE_WEB):
    # after S4: the network-trusted console forwards the visitor with no key and says so
    return creds(ip=ip, peer=peer, headers={"x-boss-console-auth": "network"})


def allowed(db, ctx, *perms, project=PROJECT, now=NOW):
    return {p: fake_check(db, ctx, p, project=project, now=now).allowed for p in perms}


# ---- goldeneye -----------------------------------------------------------------------------------

def test_goldeneye_upgrades_claims_by_passkey_and_the_network_bridge_ends(db):
    cfg = goldeneye_settings()
    community_install(db)

    # M0: the first request after the upgrade maps everything; nothing locks out.
    console = legacy.bridge(console_request(), db=db, now=NOW, settings=cfg)
    assert console.principal.id == LEGACY_CONSOLE_PRINCIPAL and console.legacy == "console_api_key"
    every_capability = sorted(set().union(*migration.CAPABILITY_PERMISSIONS.values()))
    assert all(allowed(db, console, *every_capability).values())     # today's full reach
    assert allowed(db, console, "settings:update", "deployment:production") == {
        "settings:update": True, "deployment:production": True}      # step-up: a bridge cannot
    worker = legacy.bridge(worker_request(), db=db, now=NOW, settings=cfg)
    assert worker.principal.id == LEGACY_WORKER_PRINCIPAL
    assert all(allowed(db, worker, "worker:lease", "secret:lease", "code_task:create",
                       *every_capability).values())

    # the network bridge (no env change: the console says so, from a trusted proxy)
    visitor = legacy.bridge(network_request(), db=db, now=NOW, settings=cfg)
    assert visitor.principal.id == NETWORK_OPERATOR_PRINCIPAL and visitor.principal.network_bound
    assert visitor.ip == TAILNET_VISITOR
    assert allowed(db, visitor, "code_task:create", "settings:update", "deployment:production",
                   "workflow:publish") == dict.fromkeys(
        ["code_task:create", "settings:update", "deployment:production", "workflow:publish"], True)
    assert allowed(db, visitor, "user:invite", "owner:assign", "security_policy:manage") == \
        dict.fromkeys(["user:invite", "owner:assign", "security_policy:manage"], False)
    # nobody else can claim to be the console: not a client, not a public address
    assert legacy.bridge(network_request(peer=TAILNET_VISITOR), db=db, now=NOW,
                         settings=cfg) is None
    assert legacy.bridge(network_request(ip="8.8.4.4"), db=db, now=NOW, settings=cfg) is None
    banner = posture.banners(visitor, db=db, now=NOW)
    assert banner[0]["kind"] == "network_bridge" and "Claim it" in banner[0]["message"]
    legacy.note_claimed_actor(visitor, "ceo", db=db)      # an action, attributed with its IP
    attributed = events(db, "legacy.claimed_actor_ignored")
    assert attributed[-1]["actor_principal_id"] == NETWORK_OPERATOR_PRINCIPAL
    assert str(attributed[-1]["ip"]) == TAILNET_VISITOR
    shadow_report.flush(db, settings=cfg)
    paths = {p["kind"]: p for p in posture.legacy_paths(ORG, db=db, now=NOW)}
    assert paths["network_mode"]["status"] == "critical" and paths["network_mode"]["use_count"] >= 1

    # M1: claim through the network bridge — passkey, origin bound, no setup token, no env
    claim = fake_claim(db, name="Param", email="param@example.com", via="network_bridge",
                       now=NOW, origin="https://goldeneye.tail1234.ts.net", cfg=cfg)
    assert claim.cutoff == NOW + timedelta(days=30)
    from boss_engine.identity.policy import get_policy
    assert get_policy(ORG, db=db).public_origin == "https://goldeneye.tail1234.ts.net"
    assert legacy.resolve_actor(ORG, "ceo", db=db)["label"] == "Param (was: ceo)"
    seats = db.execute(text("SELECT seat_key FROM identity_approval_seats WHERE principal_id = "
                            ":p"), {"p": claim.principal_id}).scalars().all()
    assert "ceo" in seats
    assert fake_check(db, claim.ctx, "security_policy:manage").allowed

    # in the window: the network bridge is Operator (D25's role), the key and worker unchanged
    day1 = NOW + timedelta(days=1)
    visitor = legacy.bridge(network_request(), db=db, now=day1, settings=cfg)
    assert allowed(db, visitor, "code_task:create", "deployment:create", "workflow:publish",
                   now=day1) == dict.fromkeys(["code_task:create", "deployment:create",
                                               "workflow:publish"], True)
    assert allowed(db, visitor, "settings:update", "deployment:production", "user:invite",
                   now=day1) == dict.fromkeys(["settings:update", "deployment:production",
                                               "user:invite"], False)
    assert legacy.bridge(console_request(), db=db, now=day1, settings=cfg) is not None
    day29 = NOW + timedelta(days=29)
    assert legacy.bridge(network_request(), db=db, now=day29, settings=cfg) is not None
    assert fake_check(db, visitor, "code_task:create", project=PROJECT, now=day29).allowed

    # the console keeps polling with the shared key, as it does today, right up to the date
    day30 = NOW + timedelta(days=30, hours=12)
    assert legacy.bridge(console_request(), db=db, now=day30, settings=cfg) is not None

    # the cutoff: the network bridge refuses; its principal is disabled; history keeps its name
    day31 = NOW + timedelta(days=31)
    with pytest.raises(AuthnError) as refused:
        legacy.bridge(network_request(), db=db, now=day31, settings=cfg)
    assert refused.value.code == "legacy_credential_retired"
    status = db.execute(text("SELECT status, display_name FROM identity_principals WHERE id = :p"),
                        {"p": NETWORK_OPERATOR_PRINCIPAL}).first()
    assert tuple(status) == ("disabled", "Network operator")
    assert not fake_check(db, visitor, "code_task:create", project=PROJECT, now=day31).allowed
    assert events(db, "legacy.claimed_actor_ignored")[0]["actor_principal_id"] == \
        NETWORK_OPERATOR_PRINCIPAL
    assert events(db, "legacy.path_retired")[-1]["details"]["path"] == "network_mode"

    # the console key: still used and nobody has a session yet -> retirement waits (no lockout)
    still = legacy.bridge(console_request(), db=db, now=day31, settings=cfg)
    assert still.principal.id == LEGACY_CONSOLE_PRINCIPAL
    shadow_report.flush(db, settings=cfg)
    deferred = migration.retire_due(ORG, db=db, now=day31)
    assert deferred and not deferred[0].retired and "deferred" in deferred[0].reason
    # once the Owner signs in with a session (S4's console), the key goes at its date
    db.execute(text(
        "INSERT INTO identity_sessions (id, token_hash, principal_id, auth_methods, mfa_at, "
        "last_seen_at, idle_expires_at, absolute_expires_at) VALUES ('ses_x', 'h', :p, "
        "ARRAY['passkey'], :t, :t, :t2, :t2)"),
        {"p": claim.principal_id, "t": day31, "t2": day31 + timedelta(hours=8)})
    legacy.invalidate_cache()
    with pytest.raises(AuthnError):
        legacy.bridge(console_request(), db=db, now=day31, settings=cfg)
    assert legacy.system_permissions(LEGACY_CONSOLE_PRINCIPAL, db=db) == frozenset()
    # the worker's env key has no cutoff: it keeps working until it is rotated (M5)
    assert legacy.bridge(worker_request(), db=db, now=day31, settings=cfg) is not None

    # history is intact and verifiable
    assert audit.verify_chain(ORG, db=db).ok
    assert events(db, "bootstrap.claimed")[0]["details"]["via"] == "network_bridge"


def test_goldeneye_owner_can_end_the_network_bridge_early_with_require_sign_in(db):
    cfg = goldeneye_settings()
    community_install(db)
    first = legacy.bridge(network_request(), db=db, now=NOW, settings=cfg)
    legacy.note_claimed_actor(first, "executive_console", db=db)
    claim = fake_claim(db, name="Param", email="param@example.com", via="network_bridge",
                       now=NOW, cfg=cfg)
    later = NOW + timedelta(hours=2)
    assert legacy.bridge(network_request(), db=db, now=later, settings=cfg) is not None
    migration.retire_path(ORG, LegacyKind.NETWORK_MODE, db=db, now=later,
                          actor_principal_id=claim.principal_id, actor_ctx=claim.ctx,
                          reason="ended by an Owner (Require sign-in)")
    with pytest.raises(AuthnError):
        legacy.bridge(network_request(), db=db, now=later, settings=cfg)
    retired = events(db, "legacy.path_retired")[-1]
    assert retired["actor_principal_id"] == claim.principal_id
    # no way back: extending a retired bridge is refused
    with pytest.raises(ValueError):
        migration.extend_path(ORG, LegacyKind.NETWORK_MODE, later + timedelta(days=5), db=db,
                              actor_ctx=claim.ctx, now=later)
    assert events(db, "legacy.claimed_actor_ignored")[0]["actor_principal_id"] == \
        NETWORK_OPERATOR_PRINCIPAL
    assert audit.verify_chain(ORG, db=db).ok


def test_goldeneye_without_any_upgrade_step_the_bridge_is_today_and_shadow_refuses_nothing(db):
    """Rollback guarantee: before the claim nothing is retired, nothing expires; the bridges are
    exactly today's operator, so BOSS_AUTHZ_MODE=shadow (the default) changes nothing visible."""
    cfg = goldeneye_settings()
    community_install(db)
    for when in (NOW, NOW + timedelta(days=400)):
        assert legacy.bridge(console_request(), db=db, now=when, settings=cfg) is not None
        assert legacy.bridge(network_request(), db=db, now=when, settings=cfg) is not None
    assert migration.retire_due(ORG, db=db, now=NOW + timedelta(days=400)) == []
    # and nobody can end the network bridge before the claim: it is the only way to claim
    with pytest.raises(ValueError, match="claim"):
        migration.retire_path(ORG, LegacyKind.NETWORK_MODE, db=db, now=NOW,
                              actor_principal_id="sys_host_cli", reason="too early")
    assert legacy.bridge(network_request(), db=db, now=NOW, settings=cfg) is not None


# ---- boss-1: a password install with SSO, SCIM and extra keys -----------------------------------

def boss1_settings(**extra):
    values = dict(legacy_console_api_key=CONSOLE_KEY, legacy_console_password=CONSOLE_PASSWORD,
                  api_key_principal_map={CONSOLE_KEY: "ceo", WORKER_KEY: "system",
                                         MCP_KEY: "cto"},
                  enterprise_scim_bearer_token_values=(SCIM_TOKEN,))
    values.update(extra)
    return settings(**values)


def test_boss1_password_install_claims_with_the_shared_password_and_every_path_keeps_its_window(db):
    cfg = boss1_settings()
    community_install(db)
    add_scim(db)
    live_grant = add_break_glass(db, "cao", ["infra_provision"], NOW + timedelta(hours=2))
    add_break_glass(db, "cao", ["*"], NOW - timedelta(hours=1))          # expired: not mapped

    # before the claim the shared password is the claim credential, nothing else
    assert legacy.check_shared_password("wrong", db=db, settings=cfg, now=NOW).status == "invalid"
    assert legacy.check_shared_password(CONSOLE_PASSWORD, db=db, settings=cfg,
                                        now=NOW).status == "claim"

    # an MCP key bound to a HUMAN member acts as that member's legacy service account, with
    # exactly that member's reach (not the placeholder, which is pending: no lockout)
    mcp = legacy.bridge(creds(method="api_key", member="cto", key=MCP_KEY), db=db, now=NOW,
                        settings=cfg)
    assert mcp.principal.kind.value == "service_account" and mcp.credential.kind == "legacy_env"
    assert allowed(db, mcp, "workflow_run:start", "infra:provision", "github:manage",
                   "code_task:read") == dict.fromkeys(
        ["workflow_run:start", "infra:provision", "github:manage", "code_task:read"], True)
    assert allowed(db, mcp, "budget:manage", "user:invite", "workflow:publish") == \
        dict.fromkeys(["budget:manage", "user:invite", "workflow:publish"], False)
    oidc = legacy.bridge(creds(method="oidc", member="cto"), db=db, now=NOW, settings=cfg)
    assert oidc.principal.id == mcp.principal.id and oidc.method.value == "oidc_bearer"
    raw = db.execute(text("SELECT count(*) FROM identity_api_credentials WHERE token_hash = :k "
                          "OR name LIKE '%' || :k || '%'"), {"k": MCP_KEY}).scalar()
    assert raw == 0                                   # stored as a hash, never the key

    # break-glass: a time-boxed binding; the expired grant maps to nothing
    bindings = db.execute(text("SELECT expires_at FROM identity_role_bindings WHERE reason = :r"),
                          {"r": f"break_glass:{live_grant}"}).scalars().all()
    assert bindings == [NOW + timedelta(hours=2)]
    assert db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE reason LIKE "
                           "'break_glass:%'")).scalar() == 1

    # SCIM: a pending person with her email, in a scim group with her legacy access; the raw
    # bearer token still provisions (as sys_scim_legacy) until rotated
    dana = db.execute(text("SELECT p.status, u.email FROM identity_users u JOIN "
                           "identity_principals p ON p.id = u.principal_id WHERE lower(u.email) = "
                           "'dana@example.com'")).first()
    assert tuple(dana) == ("pending", "dana@example.com")
    group = db.execute(text("SELECT g.source, count(m.*) FROM identity_groups g JOIN "
                            "identity_group_members m ON m.group_id = g.id WHERE g.name = "
                            "'Builders' GROUP BY g.source")).first()
    assert tuple(group) == ("scim", 1)
    scim = legacy.scim_bearer(SCIM_TOKEN, db=db)
    assert scim is not None and fake_check(db, scim, "scim:provision").allowed
    assert legacy.scim_bearer("not-the-token", db=db) is None

    # M1: claim with the shared password
    claim = fake_claim(db, name="Param", email="param@example.com", via="legacy_password",
                       now=NOW, cfg=cfg)
    result = legacy.check_shared_password(CONSOLE_PASSWORD, db=db, settings=cfg, now=NOW)
    assert (result.status, result.principal_id) == ("ok", LEGACY_SHARED_PRINCIPAL)
    shared = legacy.bridge(creds(method="legacy_password"), db=db, now=NOW, settings=cfg)
    assert shared.principal.id == LEGACY_SHARED_PRINCIPAL
    assert allowed(db, shared, "code_task:create", "deployment:create", "decision:approve") == \
        dict.fromkeys(["code_task:create", "deployment:create", "decision:approve"], True)
    assert allowed(db, shared, "settings:update", "user:invite", "deployment:production") == \
        dict.fromkeys(["settings:update", "user:invite", "deployment:production"], False)

    # the countdown banner shows from 14 days before
    assert posture.banners(shared, db=db, now=NOW + timedelta(days=5)) == []
    banner = posture.banners(shared, db=db, now=NOW + timedelta(days=17))
    assert banner[0]["kind"] == "legacy_password" and "13 days" in banner[0]["message"]

    # an Owner extends the window (D25: step-up, audited) -> still works past day 30
    migration.extend_path(ORG, LegacyKind.SHARED_PASSWORD, NOW + timedelta(days=40), db=db,
                          actor_ctx=claim.ctx, now=NOW + timedelta(days=25))
    day31 = NOW + timedelta(days=31)
    assert legacy.check_shared_password(CONSOLE_PASSWORD, db=db, settings=cfg,
                                        now=day31).status == "ok"
    assert fake_check(db, shared, "code_task:create", project=PROJECT, now=day31).allowed

    # then it stops: refused, principal disabled, its sessions ended
    day41 = NOW + timedelta(days=41)
    assert legacy.check_shared_password(CONSOLE_PASSWORD, db=db, settings=cfg,
                                        now=day41).status == "retired"
    with pytest.raises(AuthnError):
        legacy.bridge(creds(method="legacy_password"), db=db, now=day41, settings=cfg)
    assert not fake_check(db, shared, "code_task:create", project=PROJECT, now=day41).allowed

    # env keys have no date: the MCP key works until it is rotated or removed from the env
    assert legacy.bridge(creds(method="api_key", member="cto", key=MCP_KEY), db=db, now=day41,
                         settings=cfg) is not None
    removed = boss1_settings(api_key_principal_map={CONSOLE_KEY: "ceo", WORKER_KEY: "system"})
    migration.seed(db, settings=removed, now=day41)
    legacy.invalidate_cache()
    assert legacy.bridge(creds(method="api_key", member="cto", key=MCP_KEY), db=db, now=day41,
                         settings=removed) is None

    assert audit.verify_chain(ORG, db=db).ok


def test_the_seed_is_idempotent_and_changes_nothing_on_a_second_boot(db):
    cfg = boss1_settings()
    community_install(db)
    add_scim(db)
    first = migration.seed(db, settings=cfg, now=NOW)
    counts = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in (
        "identity_principals", "identity_role_bindings", "identity_legacy_mappings",
        "identity_api_credentials", "identity_access_roles", "identity_approval_seats",
        "identity_groups", "identity_group_members", "identity_users")}
    second = migration.seed(db, settings=cfg, now=NOW)
    assert second.principals_created == 0 and second.env_keys == 0 and first.env_keys == 2
    assert counts == {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in counts}
    placeholders = db.execute(text("SELECT status, legacy_member_ids FROM identity_principals "
                                   "WHERE kind = 'user' AND legacy_member_ids <> '{}'")).all()
    assert {tuple(r[1])[0] for r in placeholders} >= {"ceo", "cto", "cao", "vp_agents",
                                                      "release_manager"}
    assert all(r[0] == "pending" for r in placeholders)
    agents = db.execute(text("SELECT id, status FROM identity_principals WHERE kind = 'agent' "
                             "ORDER BY id")).all()
    assert [s for _, s in agents] == ["suspended", "active", "active"]  # atlas paused


def test_every_capability_maps_into_the_catalogue():
    from boss_engine.identity.permissions_gen import PERMISSION_META
    assert len(migration.CAPABILITY_PERMISSIONS) == 21
    for cap, perms in migration.CAPABILITY_PERMISSIONS.items():
        assert perms, cap
        for perm in perms:
            assert perm in PERMISSION_META and not PERMISSION_META[perm].system_only, (cap, perm)
    assert migration.capability_permissions(["*"]) == frozenset().union(
        *migration.CAPABILITY_PERMISSIONS.values())
    assert migration.capability_permissions(["no_such_capability"]) == frozenset()
    assert migration.nearest_builtin_role(migration.member_permissions(["decision_resolve",
                                                                        "audit_export"])) == "admin"


def test_a_network_bridge_first_seen_after_the_claim_still_has_its_window(db):
    """goldeneye claimed by setup link before its console forwarded visitors (S4 landing later):
    the first network request after the claim must map to Operator with the claim's clock, not
    to a pending principal (a lockout found while building the rehearsal)."""
    cfg = goldeneye_settings()
    community_install(db)
    fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW, cfg=cfg)
    later = NOW + timedelta(days=3)
    visitor = legacy.bridge(network_request(), db=db, now=later, settings=cfg)
    assert visitor.principal.active and visitor.principal.id == NETWORK_OPERATOR_PRINCIPAL
    assert fake_check(db, visitor, "code_task:create", project=PROJECT, now=later).allowed
    assert not fake_check(db, visitor, "settings:update", project=PROJECT, now=later).allowed
    paths = {p["kind"]: p for p in posture.legacy_paths(ORG, db=db, now=later)}
    assert paths["network_mode"]["retire_after"] is not None


IDENTITY_WRITES = ("user:invite", "user:update", "user:disable", "group:manage_members",
                   "access_role:manage", "role_binding:assign", "owner:assign",
                   "service_account_key:manage", "api_key:revoke", "security_policy:manage",
                   "identity_provider:manage", "delegation:grant")


def test_the_console_key_loses_identity_writes_at_the_claim_and_everything_at_the_cutoff(db):
    """Operator decision 2026-10-06: BOSS_API_KEY is the full Owner set only until the first
    Owner is claimed; from the claim, real Owners manage people, so the shared key keeps
    everything EXCEPT identity writes until claim + 30 days, then nothing."""
    cfg = goldeneye_settings()
    community_install(db)
    before = legacy.bridge(console_request(), db=db, now=NOW, settings=cfg)
    assert all(allowed(db, before, *IDENTITY_WRITES).values())        # today's reach, pre-claim
    assert legacy.system_permissions(LEGACY_CONSOLE_PRINCIPAL, db=db) == \
        migration.LEGACY_OWNER_SET

    claim = fake_claim(db, name="Param", email="param@example.com", via="network_bridge",
                       now=NOW, cfg=cfg)
    day1 = NOW + timedelta(days=1)
    after = legacy.bridge(console_request(), db=db, now=day1, settings=cfg)
    assert after.principal.id == LEGACY_CONSOLE_PRINCIPAL
    assert not any(allowed(db, after, *IDENTITY_WRITES, now=day1).values())
    still = ("settings:update", "deployment:production", "code_task:create", "workflow:publish",
             "budget:manage", "user:read", "role_binding:read", "audit:read",
             "delegation:read_all")
    assert all(allowed(db, after, *still, now=day1).values())
    granted = legacy.system_permissions(LEGACY_CONSOLE_PRINCIPAL, db=db)
    assert granted == migration.CONSOLE_AFTER_CLAIM_SET
    assert {p for p in migration.LEGACY_OWNER_SET - granted
            if PERMISSION_META[p].category != migration.IDENTITY_CATEGORY} == set()
    # the real Owner holds what the key lost: no lockout
    assert all(fake_check(db, claim.ctx, p, now=NOW).allowed for p in IDENTITY_WRITES
               if p != "delegation:grant")
    # the other legacy system principals (worker, metatron, coo) are not narrowed by the claim
    assert legacy.system_permissions(LEGACY_WORKER_PRINCIPAL, db=db) == migration.WORKER_LEGACY_SET

    # at the cutoff (once the Owner has a session) the key holds nothing
    db.execute(text(
        "INSERT INTO identity_sessions (id, token_hash, principal_id, auth_methods, mfa_at, "
        "last_seen_at, idle_expires_at, absolute_expires_at) VALUES ('ses_y', 'hy', :p, "
        "ARRAY['passkey'], :t, :t, :t2, :t2)"),
        {"p": claim.principal_id, "t": NOW + timedelta(days=31),
         "t2": NOW + timedelta(days=32)})
    migration.retire_due(ORG, db=db, now=NOW + timedelta(days=31))
    assert legacy.system_permissions(LEGACY_CONSOLE_PRINCIPAL, db=db) == frozenset()
