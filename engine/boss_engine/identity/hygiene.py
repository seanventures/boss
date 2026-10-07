"""Identity housekeeping the maintenance pass runs (identity/maintenance.py), each step idempotent:

  * API keys (IDENTITY.md §5.3): `api_key.expired` once for each key that expired; reminders 14 and
    3 days before a key expires (a banner for its owner on every page — posture.banners — and an
    email when SMTP is set); keys unused for `key_auto_revoke_unused_days` (policy; off by
    default) are revoked, audited, and their owner told;
  * the security log keeps BOSS_IDENTITY_AUDIT_RETENTION_DAYS (400 by default, never under 30):
    older events are pruned once a day through audit.prune, which writes a checkpoint so the
    chain still verifies (§8.1);
  * the egress log (§8.2) is partitioned by day (the next days' partitions are made ahead) and
    keeps 30 days;
  * a permission dropped from the catalogue is retired: taken out of custom roles, key scopes,
    agent ceilings, grants and open access requests, then deleted, with one
    `catalogue.permission_removed` event saying what it touched (§6.2).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("boss.identity.hygiene")

ACTOR = "sys_identity"                 # the actor of what this module does, in the security log
EXPIRY_NOTICE_DAYS = (14, 3)
EXPIRED_LOOKBACK = timedelta(days=7)
AUDIT_MIN_RETENTION_DAYS = 30
EGRESS_RETENTION_DAYS = 30
EGRESS_PARTITIONS_AHEAD = 3
#: Steps that need not run every 30-second pass: name -> seconds between runs (per process).
_EVERY = {"audit_retention": 6 * 3600, "egress_partitions": 3600, "key_hygiene": 300}
_last_run: dict[str, float] = {}
_last_lock = threading.Lock()


def due(step: str, *, force: bool = False) -> bool:
    if force:
        return True
    now = time.monotonic()
    with _last_lock:
        last = _last_run.get(step)
        if last is not None and now - last < _EVERY.get(step, 0):
            return False
        _last_run[step] = now
        return True


def forget_schedule() -> None:
    """Tests: run every step on the next pass."""
    with _last_lock:
        _last_run.clear()


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _system_event(db: Any, org_id: str, event_type: str, *, target_type: Optional[str] = None,
                  target_id: Optional[str] = None, details: Optional[dict] = None) -> None:
    from . import audit

    audit.record(audit.SecurityEvent(org_id=org_id, event_type=event_type,
                                     outcome=audit.Outcome.INFO, actor_principal_id=ACTOR,
                                     actor_kind="system", target_type=target_type,
                                     target_id=target_id, details=details or {}), db=db)


def _people_to_tell(db: Any, principal_id: str) -> list[str]:
    """The emails of a key's owner: the person, or a service account's owners who are people."""
    rows = db.execute(_sql(
        "SELECT u.email FROM identity_users u JOIN identity_principals p ON p.id = u.principal_id "
        "WHERE p.status = 'active' AND (u.principal_id = :p OR u.principal_id = ANY(COALESCE(("
        "SELECT owner_principal_ids FROM identity_service_accounts WHERE principal_id = :p), "
        "ARRAY[]::text[])))"), {"p": principal_id}).all()
    return sorted({r[0] for r in rows if r[0]})


def _tell(db: Any, principal_id: str, subject: str, body: str) -> int:
    from .authn_support import send_mail, smtp_configured

    if not smtp_configured():
        return 0
    sent = 0
    for email in _people_to_tell(db, principal_id):
        send_mail(email, subject, body)
        sent += 1
    return sent


# ---- API keys ------------------------------------------------------------------------------------

def key_hygiene(db: Any, *, org_id: str, now: Optional[datetime] = None) -> dict[str, int]:
    from .policy import get_policy

    now = now or datetime.now(timezone.utc)
    out = {"expired": 0, "reminded": 0, "auto_revoked": 0}
    # 1. api_key.expired, once per key, for keys that expired in the last week
    for kid, owner, name in db.execute(_sql(
            "SELECT k.id, k.principal_id, k.name FROM identity_api_credentials k JOIN "
            "identity_principals p ON p.id = k.principal_id WHERE p.org_id = :o AND k.revoked_at IS "
            "NULL AND k.expires_at <= :now AND k.expires_at > :since AND NOT EXISTS (SELECT 1 FROM "
            "identity_security_events e WHERE e.org_id = :o AND e.event_type = 'api_key.expired' "
            "AND e.target_id = k.id)"),
            {"o": org_id, "now": now, "since": now - EXPIRED_LOOKBACK}).all():
        _system_event(db, org_id, "api_key.expired", target_type="api_key", target_id=kid,
                      details={"owner": owner})
        out["expired"] += 1
    # 2. reminders 14 and 3 days ahead, once per key and threshold
    for days in EXPIRY_NOTICE_DAYS:
        for kid, owner, name, expires in db.execute(_sql(
                "SELECT k.id, k.principal_id, k.name, k.expires_at FROM identity_api_credentials k "
                "JOIN identity_principals p ON p.id = k.principal_id WHERE p.org_id = :o AND "
                "k.revoked_at IS NULL AND k.kind IN ('pat', 'sak') AND k.expires_at > :now AND "
                "k.expires_at <= :until AND NOT EXISTS (SELECT 1 FROM identity_security_events e "
                "WHERE e.org_id = :o AND e.event_type = 'api_key.expiry_notice' AND e.target_id = "
                "k.id AND e.details->>'days' = :d)"),
                {"o": org_id, "now": now, "until": now + timedelta(days=days),
                 "d": str(days)}).all():
            sent = _tell(db, owner, f"A BOSS API key expires in {days} days",
                         f"Your BOSS API key \"{name}\" stops working on "
                         f"{expires.date().isoformat()}. Make a new one in Account › API keys (or "
                         "Identity › Service accounts) and switch to it before then.")
            _system_event(db, org_id, "api_key.expiry_notice", target_type="api_key",
                          target_id=kid, details={"days": str(days), "owner": owner,
                                                  "emailed": sent})
            out["reminded"] += 1
    # 3. revoke keys unused for N days (policy; off unless set)
    unused = get_policy(org_id, db=db).key_auto_revoke_unused_days
    if unused:
        from . import api_keys
        from .principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

        actor = AuthContext(Principal(ACTOR, PrincipalKind.SYSTEM, org_id, PrincipalStatus.ACTIVE,
                                      "BOSS identity"), AuthMethod.SYSTEM)
        for kid, owner, name in db.execute(_sql(
                "SELECT k.id, k.principal_id, k.name FROM identity_api_credentials k JOIN "
                "identity_principals p ON p.id = k.principal_id WHERE p.org_id = :o AND "
                "k.revoked_at IS NULL AND k.kind IN ('pat', 'sak') AND COALESCE(k.last_used_at, "
                "k.created_at) < :before"),
                {"o": org_id, "before": now - timedelta(days=int(unused))}).all():
            if api_keys.revoke(db, actor=actor, key_id=kid, org_id=org_id):
                _tell(db, owner, "A BOSS API key was revoked",
                      f"Your BOSS API key \"{name}\" was not used for {unused} days, so BOSS "
                      "revoked it. Make a new one if you still need it.")
                out["auto_revoked"] += 1
    return out


# ---- the security log's retention ------------------------------------------------------------------

def retention_days() -> int:
    from ..config import get_settings

    raw = getattr(get_settings(), "identity_audit_retention_days", 400) or 400
    return max(AUDIT_MIN_RETENTION_DAYS, int(raw))


def audit_retention(db: Any, *, org_id: str, now: Optional[datetime] = None) -> int:
    """Prune events older than the retention, with the checkpoint audit.prune writes. Returns how
    many were pruned (0 when nothing is old enough)."""
    from . import audit

    now = now or datetime.now(timezone.utc)
    before = now - timedelta(days=retention_days())
    old = db.execute(_sql(
        "SELECT 1 FROM identity_security_events WHERE org_id = :o AND ts < :b AND "
        "NOT (event_type = ANY(:keep)) LIMIT 1"),
        {"o": org_id, "b": before, "keep": sorted(audit.KEEP_ON_PRUNE)}).first()
    if old is None:
        return 0
    return audit.prune(org_id, before=before, db=db, actor_principal_id=ACTOR,
                       actor_kind="system").pruned


# ---- the egress log: daily partitions, 30 days ------------------------------------------------------

def _partition(day: datetime) -> str:
    return f"identity_egress_log_{day:%Y%m%d}"


def egress_partitions(db: Any, *, now: Optional[datetime] = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    out = {"created": 0, "dropped": 0, "purged": 0}
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for ahead in range(1, EGRESS_PARTITIONS_AHEAD + 1):
        day = today + timedelta(days=ahead)
        name = _partition(day)
        if db.execute(_sql("SELECT to_regclass(:n)"), {"n": name}).scalar():
            continue
        try:
            with db.begin_nested():
                db.execute(_sql(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF identity_egress_log FOR VALUES "
                    f"FROM ('{day.isoformat()}') TO ('{(day + timedelta(days=1)).isoformat()}')"))
            out["created"] += 1
        except Exception as exc:  # noqa: BLE001 — rows then land in the default partition
            log.info("egress partition %s not created: %s", name, type(exc).__name__)
    cutoff = today - timedelta(days=EGRESS_RETENTION_DAYS)
    for (child,) in db.execute(_sql(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class "
            "parent ON parent.oid = i.inhparent WHERE parent.relname = 'identity_egress_log' AND "
            "c.relname ~ '^identity_egress_log_[0-9]{8}$'")).all():
        day = datetime.strptime(child[-8:], "%Y%m%d").replace(tzinfo=timezone.utc)
        if day + timedelta(days=1) <= cutoff:
            with db.begin_nested():
                db.execute(_sql(f"DROP TABLE IF EXISTS {child}"))
            out["dropped"] += 1
    out["purged"] = db.execute(_sql("DELETE FROM identity_egress_log WHERE ts < :c"),
                               {"c": cutoff}).rowcount or 0
    return out


# ---- permissions dropped from the catalogue -----------------------------------------------------------

def retire_removed_permissions(db: Any, *, org_id: str) -> list[str]:
    from .permissions_gen import PERMISSION_META

    gone = [r[0] for r in db.execute(_sql(
        "SELECT key FROM identity_permissions WHERE NOT (key = ANY(:known)) ORDER BY key"),
        {"known": sorted(PERMISSION_META)}).all()]
    for key in gone:
        touched = {
            "roles": db.execute(_sql("DELETE FROM identity_access_role_permissions WHERE "
                                     "permission_key = :k"), {"k": key}).rowcount,
            "keys": db.execute(_sql("UPDATE identity_api_credentials SET scopes = "
                                    "array_remove(scopes, :k) WHERE :k = ANY(scopes)"),
                               {"k": key}).rowcount,
            "agent_ceilings": db.execute(_sql("DELETE FROM identity_agent_ceilings WHERE "
                                              "permission_key = :k"), {"k": key}).rowcount,
            "grants": db.execute(_sql("DELETE FROM identity_delegation_grant_permissions WHERE "
                                      "permission_key = :k"), {"k": key}).rowcount,
            "access_requests": db.execute(_sql("DELETE FROM identity_access_requests WHERE "
                                               "permission_key = :k"), {"k": key}).rowcount,
        }
        db.execute(_sql("DELETE FROM identity_permissions WHERE key = :k"), {"k": key})
        _system_event(db, org_id, "catalogue.permission_removed", target_type="permission",
                      target_id=key, details=touched)
    if gone:
        from .migration import bump_epoch

        bump_epoch(db, org_id, "catalogue permissions retired")
    return gone
