"""The security event log: A73–A79 (IDENTITY.md §8, §14.8, §15.3).

A73 every admin change and denial produces exactly one event, with an actor
A74 UPDATE/DELETE as the app is refused (the append-only trigger)
A75 a tampered row: verify reports the first break
A76 a forced event-write failure rolls the change back (same transaction)
A77 no route writes audit rows from a request body
A78 pruning writes a checkpoint, keeps bootstrap.claimed, and the chain still verifies
A79 a failed sign-in for an unknown identifier stores no plaintext identifier
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from boss_engine.identity import audit, cli, legacy, migration, policy, shadow_report
from boss_engine.identity.audit import AuditWriteRefused, Outcome, SecurityEvent
from boss_engine.identity.authz import DenyReason
from boss_engine.identity.enforcement import AuthzMode, Outcome as GuardOutcome, ShadowLedger
from boss_engine.identity.legacy import LegacyKind
from engine.tests.identity_s5_support import written_days_ago
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    CONSOLE_KEY,
    NOW,
    ORG,
    REPO,
    community_install,
    creds,
    db,
    events,
    fake_claim,
    s5_engine,
    settings,
)


def ev(event_type="user.updated", **kw):
    kw.setdefault("actor_principal_id", "usr_alice")
    kw.setdefault("actor_kind", "user")
    return SecurityEvent(org_id=ORG, event_type=event_type, outcome=Outcome.SUCCESS, **kw)


def claimed(db):
    community_install(db)
    return fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW,
                      cfg=settings())


def count(db, event_type):
    return len(events(db, event_type))


# ---- A73 -----------------------------------------------------------------------------------------

def test_a73_every_admin_change_and_denial_writes_exactly_one_event_with_an_actor(db):
    owner = claimed(db)
    cfg = settings(legacy_console_api_key=CONSOLE_KEY, api_key_principal_map={CONSOLE_KEY: "ceo"},
                   legacy_console_auth="network")
    migration.seed(db, settings=cfg, now=NOW)
    changes = [
        ("policy.changed", lambda: policy.put_policy(ORG, {"session_idle_minutes": 60}, db=db,
                                                     actor=owner.ctx)),
        ("policy.changed", lambda: migration.extend_path(ORG, LegacyKind.NETWORK_MODE,
                                                         NOW + timedelta(days=40), db=db,
                                                         actor_ctx=owner.ctx, now=NOW)),
        ("legacy.path_retired", lambda: migration.retire_path(
            ORG, LegacyKind.NETWORK_MODE, db=db, now=NOW, actor_principal_id=owner.principal_id,
            actor_ctx=owner.ctx, reason="test")),
        ("policy.changed", lambda: shadow_report.explain_denial(
            ORG, {"router_key": "main", "method": "GET", "path_template": "/v1/x",
                  "reason": "unauthenticated", "note": "probes"}, db=db, ctx=owner.ctx)),
        ("owner.recovered_by_host", lambda: cli.recover_owner("param@example.com", db=db,
                                                              now=NOW)),
        ("audit.exported", lambda: audit.export(ORG, db=db, ctx=owner.ctx)),
    ]
    for event_type, change in changes:
        before = count(db, event_type)
        change()
        after = events(db, event_type)
        assert len(after) == before + 1, event_type
        assert after[-1]["actor_principal_id"], event_type
    # a policy write that changes nothing is not a change: no event
    before = count(db, "policy.changed")
    policy.put_policy(ORG, {"session_idle_minutes": 60}, db=db, actor=owner.ctx)
    assert count(db, "policy.changed") == before


def test_a73_shadow_denials_are_one_event_per_new_route_reason_and_kind_per_day(db):
    community_install(db)
    ledger = ShadowLedger()
    deny = GuardOutcome(False, DenyReason.MISSING_PERMISSION, "settings:update", "permit",
                        "user")
    for _ in range(5):
        ledger.record(router_key="setup", method="PUT", path_template="/v1/settings",
                      outcome=deny, mode=AuthzMode.SHADOW)
    shadow_report.flush(db, ledger=ledger, settings=settings())
    assert count(db, "authz.shadow_deny") == 1
    for _ in range(3):
        ledger.record(router_key="setup", method="PUT", path_template="/v1/settings",
                      outcome=deny, mode=AuthzMode.SHADOW)
    shadow_report.flush(db, ledger=ledger, settings=settings())
    assert count(db, "authz.shadow_deny") == 1               # same key, same day: counted
    total = db.execute(text("SELECT count FROM identity_authz_shadow WHERE path_template = "
                            "'/v1/settings'")).scalar()
    assert total == 8
    ledger.record(router_key="setup", method="PUT", path_template="/v1/settings",
                  outcome=GuardOutcome(False, DenyReason.UNAUTHENTICATED, "settings:update"),
                  mode=AuthzMode.SHADOW)
    shadow_report.flush(db, ledger=ledger, settings=settings())
    shadow = events(db, "authz.shadow_deny")
    assert len(shadow) == 2 and shadow[-1]["details"]["reason"] == "unauthenticated"
    assert shadow[0]["actor_kind"] == "user" and shadow[0]["permission"] == "settings:update"


def test_a73_bridged_use_and_claimed_actors_are_recorded_once_a_day_not_per_request(db):
    community_install(db)
    cfg = settings(legacy_console_api_key=CONSOLE_KEY, api_key_principal_map={CONSOLE_KEY: "ceo"})
    for _ in range(4):
        ctx = legacy.bridge(creds(method="api_key", member="ceo", key=CONSOLE_KEY), db=db,
                            now=NOW, settings=cfg)
        legacy.note_claimed_actor(ctx, "ceo", db=db)
    used = events(db, "legacy.api_key_used")
    assert len(used) == 1 and used[0]["actor_principal_id"] == "sys_legacy_console"
    assert count(db, "legacy.claimed_actor_ignored") == 1
    legacy.flush_usage(db)
    uses = db.execute(text("SELECT use_count FROM identity_legacy_mappings WHERE legacy_kind = "
                           "'console_api_key'")).scalar()
    assert uses == 4
    # the principal's own id or a legacy id it took over is not a "different" actor
    legacy.forget_claimed_actor_notes()
    legacy.note_claimed_actor(ctx, "sys_legacy_console", db=db)
    legacy.note_claimed_actor(ctx, "executive_console", db=db)
    assert count(db, "legacy.claimed_actor_ignored") == 1


# ---- A74 -----------------------------------------------------------------------------------------

def test_a74_the_log_is_append_only(db):
    community_install(db)
    with written_days_ago(40):
        audit.record(ev(), db=db)
    for statement in ("UPDATE identity_security_events SET outcome = 'failure'",
                      "DELETE FROM identity_security_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            with db.begin_nested():
                db.execute(text(statement))
    # the prune switch is transaction-local and switched off again by prune itself
    audit.prune(ORG, before=datetime.now(timezone.utc) - timedelta(days=30, seconds=1), db=db,
                actor_principal_id="usr_alice")
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(text("DELETE FROM identity_security_events"))


# ---- A75 -----------------------------------------------------------------------------------------

def _tamper(db, statement, **params):
    db.execute(text("ALTER TABLE identity_security_events DISABLE TRIGGER "
                    "identity_security_events_append_only"))
    db.execute(text(statement), params)
    db.execute(text("ALTER TABLE identity_security_events ENABLE TRIGGER "
                    "identity_security_events_append_only"))


@pytest.mark.parametrize("statement, first_break", [
    ("UPDATE identity_security_events SET details = '{\"diff\": \"edited\"}' WHERE seq = 3", 3),
    ("UPDATE identity_security_events SET actor_principal_id = 'usr_mallory' WHERE seq = 4", 4),
    ("DELETE FROM identity_security_events WHERE seq = 3", 4),
    ("UPDATE identity_security_events SET hash = prev_hash WHERE seq = 5", 5),
])
def test_a75_a_tampered_chain_reports_its_first_break(db, statement, first_break):
    community_install(db)
    for i in range(6):
        audit.record(ev(target_id=f"usr_{i}", details={"n": i}, ip="100.64.0.7",
                        chain=["agt_a#run_1"] if i % 2 else None), db=db)
    assert audit.verify_chain(ORG, db=db).ok
    _tamper(db, statement)
    result = audit.verify_chain(ORG, db=db)
    assert not result.ok and result.first_break_seq == first_break


def test_a75_verification_survives_the_database_round_trip_of_every_column(db):
    community_install(db)
    audit.record(ev("token.exchanged", on_behalf_of="usr_alice", grant_id="dlg_1",
                    chain=["agt_atlas#run_1", "agt_forge#run_2"], token_jti="rt_1",
                    credential_id="key_1", session_ref="ses_1", target_type="code_task",
                    target_id="ct_1", permission="code_task:create", scope="project:p1",
                    ip="2001:DB8::1", user_agent="ü-agent/1", request_id="req_1",
                    details={"nested": {"list": [1, 2.5, None, "é"]}, "big": 2 ** 53}), db=db)
    result = audit.verify_chain(ORG, db=db)
    assert result.ok and result.checked == 1


# ---- A76 -----------------------------------------------------------------------------------------

def test_a76_a_failed_event_write_rolls_the_change_back(db, monkeypatch):
    owner = claimed(db)
    before = db.execute(text("SELECT policy, epoch FROM identity_settings WHERE org_id = :o"),
                        {"o": ORG}).first()

    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(audit, "record", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        with db.begin_nested():
            policy.put_policy(ORG, {"password_min_length": 20}, db=db, actor=owner.ctx)
    after = db.execute(text("SELECT policy, epoch FROM identity_settings WHERE org_id = :o"),
                       {"o": ORG}).first()
    assert tuple(after) == tuple(before)
    monkeypatch.undo()
    # and a refused event (here: a credential-named detail) is a failure too, never swallowed
    with pytest.raises(AuditWriteRefused):
        audit.record(ev(details={"client_secret": "x"}), db=db)
    with pytest.raises(AuditWriteRefused):
        audit.record(ev("user.renamed"), db=db)
    with pytest.raises(AuditWriteRefused):
        audit.record(ev(), db=None)


def test_a76_a_failed_event_write_leaves_the_bridge_running(db, monkeypatch):
    owner = claimed(db)
    cfg = settings(legacy_console_auth="network")
    migration.seed(db, settings=cfg, now=NOW)
    monkeypatch.setattr(audit, "record", lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("no")))
    with pytest.raises(RuntimeError):
        with db.begin_nested():
            migration.retire_path(ORG, LegacyKind.NETWORK_MODE, db=db, now=NOW,
                                  actor_principal_id=owner.principal_id, actor_ctx=owner.ctx,
                                  reason="test")
    status = db.execute(text("SELECT status FROM identity_principals WHERE id = "
                             "'usr_network_operator'")).scalar()
    retired = db.execute(text("SELECT retired_at FROM identity_legacy_mappings WHERE "
                              "legacy_kind = 'network_mode'")).scalar()
    assert status == "active" and retired is None


# ---- A77 -----------------------------------------------------------------------------------------

def test_a77_only_the_audit_module_inserts_security_events():
    """Shipping code writes the chain only through identity/audit.py. Test files are not shipping
    code (fixtures and tamper tests write raw rows on purpose); the schema's one INSERT is the
    SECURITY DEFINER identity_audit_checkpoint, which only audit.py calls."""
    hits = subprocess.run(
        ["git", "grep", "-l", "-i", "-E", r"insert\s+into\s+identity_security_events", "--",
         "*.py", "*.ts", "*.tsx", "*.sql"], cwd=REPO, capture_output=True, text=True).stdout.split()
    shipping = {h for h in hits if "/tests/" not in h and not h.startswith("tests/")}
    allowed = {"engine/boss_engine/identity/audit.py", "db/schema.sql"}
    assert shipping <= allowed, shipping - allowed
    schema = (REPO / "db" / "schema.sql").read_text()
    inserts = [m.start() for m in re.finditer(r"(?i)insert\s+into\s+identity_security_events",
                                              schema)]
    owner = schema.rfind("CREATE OR REPLACE FUNCTION identity_audit_checkpoint", 0, inserts[0])
    assert len(inserts) == 1 and owner != -1, "the schema writes the chain outside the checkpoint"
    assert schema.find("CREATE OR REPLACE FUNCTION", owner + 1) > inserts[0]
    callers = subprocess.run(["git", "grep", "-l", "identity_audit_checkpoint(", "--", "*.py"],
                             cwd=REPO, capture_output=True, text=True).stdout.split()
    assert {c for c in callers if "/tests/" not in c} == {"engine/boss_engine/identity/audit.py"}


def test_a77_no_audit_route_accepts_an_event_and_a_body_cannot_forge_one(db, monkeypatch):
    from fastapi.testclient import TestClient

    from apps.api.app.routers import identity_admin
    from engine.tests.test_identity_audit_admin_api import app_for

    owner = claimed(db)
    client = TestClient(app_for(db, monkeypatch, owner.ctx))
    forged = {"event_type": "owner.added", "actor_principal_id": "usr_mallory",
              "outcome": "success", "details": {"x": 1}}
    for path in ("/v1/identity/audit/verify", "/v1/identity/audit/export"):
        assert client.post(path, json=forged).status_code == 200
    assert count(db, "owner.added") == 0
    assert not any(e["actor_principal_id"] == "usr_mallory" for e in events(db))
    router = identity_admin.build_identity_admin_router(start_background=False)
    writes = sorted(f"{m} {r.path}" for r in router.routes for m in r.methods
                    if m in ("POST", "PUT", "PATCH") and "/audit" in r.path)
    assert writes == ["POST /v1/identity/audit/export", "POST /v1/identity/audit/prune",
                      "POST /v1/identity/audit/verify"]
    source = Path(identity_admin.__file__).read_text()
    assert not re.search(r"SecurityEvent\([^)]*payload", source)
    assert not re.search(r"event_type\s*=\s*payload", source)


# ---- A78 -----------------------------------------------------------------------------------------

def test_a78_prune_writes_a_checkpoint_keeps_the_claim_and_the_chain_still_verifies(db):
    with written_days_ago(60):
        owner = claimed(db)
        for i in range(5):
            audit.record(ev(target_id=f"usr_{i}"), db=db)
    total = len(events(db))
    cut = datetime.now(timezone.utc) - timedelta(days=30, seconds=1)
    result = audit.prune(ORG, before=cut, db=db, actor_principal_id=owner.principal_id,
                         ctx=owner.ctx)
    left = events(db)
    assert result.pruned == total - 1 and result.retained == 1
    assert [e["event_type"] for e in left] == ["bootstrap.claimed", "audit.checkpoint"]
    checkpoint = left[-1]["details"]
    assert checkpoint["pruned_through_seq"] == total and checkpoint["pruned_count"] == total - 1
    verified = audit.verify_chain(ORG, db=db)
    assert verified.ok and verified.last_seq == total + 1
    # the last-Owner protection still sees the claim
    assert migration.claimed_at(db, ORG) is not None
    # life goes on, a second prune, still verifiable
    with written_days_ago(31):
        for i in range(3):
            audit.record(ev(target_id=f"usr_more_{i}"), db=db)
    audit.prune(ORG, before=datetime.now(timezone.utc) - timedelta(days=30, seconds=1), db=db,
                actor_principal_id=owner.principal_id)
    assert audit.verify_chain(ORG, db=db).ok
    # and a kept row cannot be edited without the checkpoint noticing
    _tamper(db, "UPDATE identity_security_events SET details = '{}' WHERE event_type = "
                "'bootstrap.claimed'")
    assert not audit.verify_chain(ORG, db=db).ok


def test_a78_export_carries_the_events_the_verification_and_the_anchor(db):
    owner = claimed(db)
    bundle = audit.export(ORG, db=db, ctx=owner.ctx)
    assert bundle["verification"]["ok"] and bundle["anchor"]["seq"] == len(bundle["events"])
    # offline re-verification from the bundle alone
    prev = audit.GENESIS_HASH
    key = audit.chain_key(ORG, db=db)            # a keyed chain re-verifies with the org's chain key
    for row in bundle["events"]:
        stored = dict(row, ts=datetime.fromisoformat(row["ts"]))
        alg = row["hash_alg"]
        assert audit.chain_hash(prev, stored, alg=alg, key=key if alg == 2 else None).hex() == row["hash"]
        prev = bytes.fromhex(row["hash"])
    anchor = audit.daily_anchor(ORG, db=db, project_id="boss-dogfood")
    again = audit.daily_anchor(ORG, db=db, project_id="boss-dogfood")
    assert anchor == again or anchor["seq"] <= again["seq"]
    assert db.execute(text("SELECT count(*) FROM events WHERE event_type = "
                           "'identity.audit_anchor'")).scalar() == 1


# ---- A79 -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["alice@example.com", "Hunter2-my-password!"])
def test_a79_a_failed_sign_in_stores_no_plaintext_identifier(db, typed):
    community_install(db)
    for key in ("identifier", "email", "username"):
        with pytest.raises(AuditWriteRefused, match="identifier_hmac"):
            audit.record(SecurityEvent(org_id=ORG, event_type="auth.login.failed",
                                       outcome=Outcome.FAILURE, ip="100.64.0.9",
                                       details={key: typed, "reason": "unknown"}), db=db)
    hashed = audit.identifier_hmac(typed)
    assert hashed.startswith("hmac:") and typed.lower() not in hashed
    assert audit.identifier_hmac(typed.upper() + "  ") == hashed
    audit.record(SecurityEvent(org_id=ORG, event_type="auth.login.failed",
                               outcome=Outcome.FAILURE, ip="100.64.0.9",
                               details={"identifier": hashed, "reason": "unknown"}), db=db)
    dump = db.execute(text("SELECT string_agg(row_to_json(e)::text, '') FROM "
                           "identity_security_events e")).scalar()
    assert typed not in dump and typed.lower() not in dump
