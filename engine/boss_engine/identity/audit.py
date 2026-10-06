"""The security event log: append-only, hash-chained per org. Owned by stream S5.

FROZEN IN PHASE 0: SecurityEvent, the event-type list, the canonical form and the chain hash. Bodies
marked NotImplementedError are S5's (IDENTITY_BUILD.md §3). IDENTITY.md §8.

`record` writes in the CALLER'S transaction: if the event cannot be written, the change it describes
fails too (D22, A76). There is no write API; events come only from code paths.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterator, Mapping, Optional

from . import redaction
from .principal import AuthContext


class Stream(StrEnum):
    SECURITY = "security"
    LINEAGE = "lineage"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    INFO = "info"


#: Every event type (IDENTITY.md §8.2, plus the email sign-in and self sign-up events of
#: amendment A7). `record` refuses a type not listed here; adding one is a reviewed change.
EVENT_TYPES: frozenset[str] = frozenset({
    # sign-in
    "auth.login.succeeded", "auth.login.failed", "auth.mfa.succeeded", "auth.mfa.failed",
    "auth.passkey.succeeded", "auth.passkey.failed", "auth.locked_soft", "auth.new_device",
    "auth.step_up", "auth.logout",
    "auth.magic_link.requested", "auth.magic_link.used", "auth.magic_link.rejected",
    "auth.email_code.requested", "auth.email_code.succeeded", "auth.email_code.failed",
    "auth.signup.requested", "auth.signup.completed", "auth.signup.refused",
    # sessions
    "session.created", "session.rotated", "session.revoked",
    # credentials
    "password.changed", "password.reset_requested", "password.reset_completed", "mfa.enrolled",
    "mfa.removed", "recovery_codes.generated", "recovery_code.used", "passkey.added",
    "passkey.removed", "passkey.counter_regressed",
    # admin
    "user.invited", "user.updated", "user.disabled", "user.enabled", "user.deleted",
    "user.mfa_reset", "group.created", "group.updated", "group.deleted", "group.member_added",
    "group.member_removed", "access_role.created", "access_role.updated", "access_role.deleted",
    "role_binding.added", "role_binding.removed", "owner.added", "owner.removed",
    # keys
    "api_key.created", "api_key.rotated", "api_key.revoked", "api_key.expired",
    "api_key.first_used", "api_key.used_from_new_ip", "service_account.created",
    "service_account.updated", "service_account.disabled", "service_account.orphaned",
    # SSO and SCIM
    "sso.login", "sso.link", "sso.unlink", "sso.provider.created", "sso.provider.updated",
    "sso.provider.deleted", "scim.user.created", "scim.user.updated", "scim.user.deactivated",
    "scim.group.created", "scim.group.updated", "scim.group.deleted",
    # agents (stream = lineage for passport/grant/token events)
    "agent.passport.born", "agent.passport.sealed", "agent.passport.ceiling_changed",
    "agent.passport.sponsor_changed", "agent.passport.suspended", "agent.passport.resumed",
    "agent.passport.revoked", "agent.passport.reissued",
    "delegation.created", "delegation.used_first", "delegation.narrowed", "delegation.revoked",
    "delegation.expired", "delegation.suspended",
    "token.issued", "token.exchanged", "token.denied", "credential.handed_out",
    "egress.denied",
    # vault
    "vault.connection.linked", "vault.connection.refreshed", "vault.connection.needs_reauth",
    "vault.connection.revoked", "vault.decrypt_failed", "vault.master_key_rotated",
    # authorization
    "authz.denied", "authz.shadow_deny", "authz.allowed_critical", "authz.mode_changed",
    # policy and system
    "policy.changed", "bootstrap.claimed", "owner.recovered_by_host", "legacy.password_used",
    "legacy.api_key_used", "legacy.network_used", "legacy.claimed_actor_ignored",
    "legacy.path_retired", "catalogue.permission_removed", "audit.exported", "audit.verified",
    "audit.checkpoint",
})

#: hash of "no previous event" for the first row of an org's chain.
GENESIS_HASH = b"\x00" * 32

#: The columns that are hashed, in order (everything except id, prev_hash and hash).
HASHED_FIELDS = ("org_id", "seq", "ts", "stream", "event_type", "outcome", "actor_principal_id",
                 "actor_kind", "on_behalf_of", "grant_id", "chain", "token_jti", "credential_id",
                 "session_ref", "target_type", "target_id", "permission", "scope", "ip",
                 "user_agent", "request_id", "details")


@dataclass(frozen=True)
class SecurityEvent:
    org_id: str
    event_type: str
    outcome: Outcome
    stream: Stream = Stream.SECURITY
    actor_principal_id: Optional[str] = None
    actor_kind: Optional[str] = None
    on_behalf_of: Optional[str] = None
    grant_id: Optional[str] = None
    chain: Optional[list[str]] = None
    token_jti: Optional[str] = None
    credential_id: Optional[str] = None
    session_ref: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    permission: Optional[str] = None
    scope: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)   # structured; never a secret

    @classmethod
    def from_context(cls, ctx: AuthContext, event_type: str, outcome: Outcome, **fields: Any
                     ) -> "SecurityEvent":
        """Fill the actor columns from a verified context (never from a request body)."""
        rt = ctx.run_token
        return cls(
            org_id=ctx.principal.org_id, event_type=event_type, outcome=outcome,
            actor_principal_id=ctx.principal.id, actor_kind=ctx.principal.kind.value,
            on_behalf_of=ctx.on_behalf_of, grant_id=rt.grant_id if rt else None,
            chain=ctx.chain.to_boss_chain() or None, token_jti=rt.jti if rt else None,
            credential_id=ctx.credential.id if ctx.credential else None,
            session_ref=ctx.session.id if ctx.session else None,
            ip=ctx.ip, user_agent=ctx.user_agent, request_id=ctx.request_id, **fields)


@dataclass(frozen=True)
class RecordedEvent:
    id: int
    seq: int
    hash: bytes


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    org_id: str
    checked: int
    last_seq: int
    first_break_seq: Optional[int] = None
    verified_at: Optional[datetime] = None


def canonical_json(row: Mapping[str, Any]) -> bytes:
    """The bytes that are hashed: HASHED_FIELDS only, sorted keys, no whitespace, UTF-8,
    datetimes as ISO-8601 UTC with microseconds. Frozen: changing it breaks every chain."""
    def norm(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat(timespec="microseconds")
        if isinstance(v, StrEnum):
            return v.value
        return v

    body = {k: norm(row.get(k)) for k in HASHED_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def chain_hash(prev_hash: bytes, row: Mapping[str, Any]) -> bytes:
    """hash = SHA-256(prev_hash ‖ canonical_json(row))."""
    return hashlib.sha256(prev_hash + canonical_json(row)).digest()


def event_row(event: SecurityEvent, *, seq: int, ts: datetime) -> dict[str, Any]:
    """The row for `event` at position `seq` (what canonical_json hashes)."""
    row = asdict(event)
    row["details"] = dict(event.details)
    row.update(seq=seq, ts=ts)
    return row




# ==== stream S5: the write path, verification, prune, export ======================================

class AuditWriteRefused(ValueError):
    """`record` refused an event: an unknown type, a credential-named detail key, or a plaintext
    identifier on a sign-in failure (A79). The caller's change fails with it (D22)."""


#: Events about a sign-in attempt whose identifier may be someone's typo'd password (A79): the
#: identifier is stored only as `identifier_hmac(...)`.
IDENTIFIER_EVENTS = frozenset({
    "auth.login.failed", "auth.mfa.failed", "auth.passkey.failed", "auth.locked_soft",
    "auth.magic_link.requested", "auth.magic_link.rejected", "auth.email_code.requested",
    "auth.email_code.failed", "auth.signup.requested", "auth.signup.refused",
    "password.reset_requested",
})
IDENTIFIER_KEYS = frozenset({"identifier", "email", "username", "user_name", "login", "user",
                             "account", "principal_name"})
HMAC_PREFIX = "hmac:"

#: Rows a prune never deletes: the last-Owner trigger reads `bootstrap.claimed` (A9), and earlier
#: checkpoints carry the hashes that keep older retained rows verifiable.
KEEP_ON_PRUNE = frozenset({"bootstrap.claimed", "audit.checkpoint", "owner.recovered_by_host"})

_PROCESS_HMAC_KEY = secrets.token_bytes(32)
_INSERT_COLUMNS = HASHED_FIELDS + ("prev_hash", "hash")
_TEXT_COLUMNS_TO_REDACT = ("user_agent", "target_type", "target_id", "request_id", "scope",
                           "permission", "session_ref", "credential_id", "token_jti")


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _hmac_key() -> bytes:
    try:
        from ..config import get_settings
        configured = get_settings().identity_hmac_key
    except Exception:  # noqa: BLE001
        configured = ""
    return configured.encode("utf-8") if configured else _PROCESS_HMAC_KEY


def identifier_hmac(identifier: str) -> str:
    """The only form in which a sign-in identifier for a failed or unknown attempt is stored
    (IDENTITY.md §8.3, A79): HMAC-SHA-256 of the trimmed, lowercased value."""
    digest = hmac.new(_hmac_key(), (identifier or "").strip().lower().encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return HMAC_PREFIX + digest


def _lock_key(org_id: str) -> int:
    raw = hashlib.sha256(f"identity_security_events:{org_id}".encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


def _json_roundtrip(value: Any) -> Any:
    """What JSONB will give back, so the hashed form equals the stored form."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _norm_ip(ip: Optional[str]) -> Optional[str]:
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(str(ip).strip()))
    except ValueError:
        return None


def prepare(event: SecurityEvent) -> SecurityEvent:
    """Validate and redact an event before it is hashed. Raises AuditWriteRefused."""
    if not isinstance(event, SecurityEvent):
        raise AuditWriteRefused("not a SecurityEvent")
    if event.event_type not in EVENT_TYPES:
        raise AuditWriteRefused(f"unknown security event type {event.event_type!r}")
    if not event.org_id:
        raise AuditWriteRefused("a security event needs an org")
    try:
        outcome, stream = Outcome(event.outcome), Stream(event.stream)
    except ValueError as exc:
        raise AuditWriteRefused(str(exc)) from None
    details = dict(event.details or {})
    named = redaction.sensitive_keys_in(details)
    if named:
        raise AuditWriteRefused(f"details may not carry credential-named keys: {named}")
    if event.event_type in IDENTIFIER_EVENTS:
        for key, value in details.items():
            if (str(key).lower() in IDENTIFIER_KEYS and value is not None
                    and not (isinstance(value, str) and value.startswith(HMAC_PREFIX))):
                raise AuditWriteRefused(
                    f"{event.event_type}: {key!r} must be stored as identifier_hmac(...) (A79)")
    details = _json_roundtrip(redaction.redact(details))
    fields: dict[str, Any] = {name: getattr(event, name) for name in event.__dataclass_fields__}
    fields.update(outcome=outcome, stream=stream, details=details, ip=_norm_ip(event.ip),
                  chain=list(event.chain) if event.chain else None)
    if not fields.get("request_id"):
        from .request_ids import current_request_id
        fields["request_id"] = current_request_id.get()
    for column in _TEXT_COLUMNS_TO_REDACT:
        value = fields.get(column)
        if isinstance(value, str):
            fields[column] = redaction.redact_text(value)[:1000]
    return SecurityEvent(**fields)


def record(event: SecurityEvent, *, db: Any) -> RecordedEvent:
    """Append one event in the caller's transaction (per-org advisory lock for seq). Refuses an
    event type not in EVENT_TYPES and any `details` key matching the redaction pattern.

    Nothing here is caught: if the event cannot be written the caller's transaction fails, and
    with it the change the event describes (D22, A76)."""
    if db is None:
        raise AuditWriteRefused("audit.record writes in the caller's transaction: pass db=")
    clean = prepare(event)
    db.execute(_sql("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(clean.org_id)})
    last = db.execute(_sql(
        "SELECT seq, hash FROM identity_security_events WHERE org_id = :o "
        "ORDER BY seq DESC LIMIT 1"), {"o": clean.org_id}).first()
    seq = int(last[0]) + 1 if last else 1
    prev_hash = bytes(last[1]) if last else GENESIS_HASH
    ts = datetime.now(timezone.utc)
    row = event_row(clean, seq=seq, ts=ts)
    row["details"] = clean.details
    digest = chain_hash(prev_hash, row)
    params = {k: row.get(k) for k in HASHED_FIELDS}
    params.update(outcome=clean.outcome.value, stream=clean.stream.value,
                  chain=None if row.get("chain") is None else json.dumps(row["chain"]),
                  details=json.dumps(row["details"], ensure_ascii=False),
                  prev_hash=prev_hash, hash=digest)
    inserted = db.execute(_sql(
        "INSERT INTO identity_security_events (" + ", ".join(_INSERT_COLUMNS) + ") VALUES ("
        + ", ".join(f"CAST(:{c} AS JSONB)" if c in ("chain", "details")
                    else f"CAST(:{c} AS INET)" if c == "ip" else f":{c}"
                    for c in _INSERT_COLUMNS) + ") RETURNING id"), params).first()
    return RecordedEvent(id=int(inserted[0]), seq=seq, hash=digest)


# ---- reading rows back -------------------------------------------------------------------------

_SELECT = ("SELECT id, " + ", ".join(HASHED_FIELDS) + ", prev_hash, hash "
           "FROM identity_security_events")


def row_from_db(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A stored row in the shape `canonical_json` hashed it."""
    row = dict(raw)
    ts = row.get("ts")
    if isinstance(ts, datetime):
        row["ts"] = ts.astimezone(timezone.utc)
    ip = row.get("ip")
    row["ip"] = None if ip is None else str(ip).split("/")[0]
    for key in ("chain", "details"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    if row.get("details") is None:
        row["details"] = {}
    for key in ("prev_hash", "hash"):
        if row.get(key) is not None:
            row[key] = bytes(row[key])
    return row


def _iter_rows(db: Any, org_id: str, *, from_seq: int = 0, batch: int = 2000
               ) -> Iterator[dict[str, Any]]:
    after = from_seq - 1
    while True:
        rows = db.execute(_sql(_SELECT + " WHERE org_id = :o AND seq > :after ORDER BY seq "
                               "LIMIT :n"), {"o": org_id, "after": after, "n": batch}
                          ).mappings().all()
        if not rows:
            return
        for raw in rows:
            yield row_from_db(raw)
        after = int(rows[-1]["seq"])


def _checkpoints(db: Any, org_id: str) -> list[dict[str, Any]]:
    rows = db.execute(_sql(
        "SELECT seq, details FROM identity_security_events WHERE org_id = :o "
        "AND event_type = 'audit.checkpoint' ORDER BY seq"), {"o": org_id}).mappings().all()
    out = []
    for r in rows:
        d = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"] or "{}")
        if "pruned_through_seq" in d:
            out.append({"seq": int(r["seq"]), **d})
    return out


def verify_chain(org_id: str, *, db: Any, from_seq: Optional[int] = None) -> ChainVerification:
    """Walk the chain and report the first break (A75). Checkpoints from pruning are honoured."""
    checkpoints = _checkpoints(db, org_id)
    pruned_through = max((int(c["pruned_through_seq"]) for c in checkpoints), default=0)
    anchor_hash: dict[int, bytes] = {}
    retained: dict[int, str] = {}
    for c in checkpoints:
        anchor_hash[int(c["pruned_through_seq"])] = bytes.fromhex(c["last_pruned_hash"])
        retained.update({int(k): v for k, v in (c.get("retained") or {}).items()})

    expected_seq: Optional[int] = None
    expected_prev: Optional[bytes] = None
    if from_seq and from_seq > 1:
        before = db.execute(_sql("SELECT hash FROM identity_security_events WHERE org_id = :o "
                                 "AND seq = :s"), {"o": org_id, "s": from_seq - 1}).first()
        if before is not None:
            expected_seq, expected_prev = from_seq, bytes(before[0])
        elif from_seq - 1 in anchor_hash:
            expected_seq, expected_prev = from_seq, anchor_hash[from_seq - 1]

    checked = 0
    last_seq = 0
    for row in _iter_rows(db, org_id, from_seq=from_seq or 0):
        seq = int(row["seq"])
        last_seq = seq
        checked += 1
        broken = chain_hash(row["prev_hash"], row) != row["hash"]
        if not broken and seq <= pruned_through and expected_prev is None:
            # a row kept by a prune: it must be exactly the one the checkpoint recorded
            broken = retained.get(seq) != row["hash"].hex()
            if not broken:
                continue
        elif not broken:
            if expected_prev is None:
                if seq == 1:
                    broken = row["prev_hash"] != GENESIS_HASH
                else:
                    broken = anchor_hash.get(seq - 1) != row["prev_hash"]
            else:
                broken = seq != expected_seq or row["prev_hash"] != expected_prev
        if broken:
            return ChainVerification(False, org_id, checked, seq, first_break_seq=seq,
                                     verified_at=datetime.now(timezone.utc))
        expected_seq, expected_prev = seq + 1, row["hash"]
    return ChainVerification(True, org_id, checked, last_seq,
                             verified_at=datetime.now(timezone.utc))


def head(org_id: str, *, db: Any) -> Optional[tuple[int, bytes]]:
    """The chain head (seq, hash): what a daily anchor publishes."""
    row = db.execute(_sql("SELECT seq, hash FROM identity_security_events WHERE org_id = :o "
                          "ORDER BY seq DESC LIMIT 1"), {"o": org_id}).first()
    return (int(row[0]), bytes(row[1])) if row else None


# ---- prune (Owner only, step-up; §8.1, A78) ----------------------------------------------------

@dataclass(frozen=True)
class PruneResult:
    pruned: int
    pruned_through_seq: Optional[int]
    checkpoint_seq: Optional[int]
    retained: int


def prune(org_id: str, *, before: datetime, db: Any, actor_principal_id: str,
          actor_kind: Optional[str] = None, ctx: Optional[AuthContext] = None) -> PruneResult:
    """Delete the events older than `before`, after writing an `audit.checkpoint` that holds the
    hash of the pruned range, so the chain still verifies. `bootstrap.claimed` and earlier
    checkpoints are kept (KEEP_ON_PRUNE). The DELETE is allowed only inside this transaction."""
    db.execute(_sql("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(org_id)})
    newest_kept = db.execute(_sql(
        "SELECT min(seq) FROM identity_security_events WHERE org_id = :o AND ts >= :b"),
        {"o": org_id, "b": before}).scalar()
    upper = db.execute(_sql(
        "SELECT max(seq) FROM identity_security_events WHERE org_id = :o"
        + (" AND seq < :n" if newest_kept is not None else "")),
        {"o": org_id, "n": newest_kept}).scalar()
    if upper is None:
        return PruneResult(0, None, None, 0)
    rows = db.execute(_sql(
        "SELECT seq, hash, event_type FROM identity_security_events WHERE org_id = :o "
        "AND seq <= :u ORDER BY seq"), {"o": org_id, "u": upper}).all()
    doomed = [r for r in rows if r[2] not in KEEP_ON_PRUNE]
    if not doomed:
        return PruneResult(0, None, None, len(rows))
    digest = hashlib.sha256(b"".join(bytes(r[1]) for r in rows)).hexdigest()
    last_hash = bytes(rows[-1][1]).hex()
    retained = {str(r[0]): bytes(r[1]).hex() for r in rows if r[2] in KEEP_ON_PRUNE}
    details = {"pruned_from_seq": int(rows[0][0]), "pruned_through_seq": int(upper),
               "pruned_count": len(doomed), "last_pruned_hash": last_hash,
               "range_digest": digest, "retained": retained,
               "before": before.astimezone(timezone.utc).isoformat()}
    if ctx is not None:
        event = SecurityEvent.from_context(ctx, "audit.checkpoint", Outcome.INFO, details=details)
    else:
        event = SecurityEvent(org_id=org_id, event_type="audit.checkpoint", outcome=Outcome.INFO,
                              actor_principal_id=actor_principal_id, actor_kind=actor_kind,
                              details=details)
    checkpoint = record(event, db=db)
    db.execute(_sql("SELECT set_config('boss.identity_audit_prune', 'on', true)"))
    try:
        db.execute(_sql(
            "DELETE FROM identity_security_events WHERE org_id = :o AND seq <= :u "
            "AND event_type <> ALL(:keep)"),
            {"o": org_id, "u": upper, "keep": sorted(KEEP_ON_PRUNE)})
    finally:
        db.execute(_sql("SELECT set_config('boss.identity_audit_prune', 'off', true)"))
    return PruneResult(len(doomed), int(upper), checkpoint.seq, len(retained))


# ---- read API and export -------------------------------------------------------------------------

def to_public(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as the API and export show it (hashes in hex, ts ISO)."""
    out = {k: row.get(k) for k in ("id",) + HASHED_FIELDS}
    ts = row.get("ts")
    out["ts"] = ts.isoformat() if isinstance(ts, datetime) else ts
    for key in ("prev_hash", "hash"):
        value = row.get(key)
        out[key] = bytes(value).hex() if value is not None else None
    return out


def export(org_id: str, *, db: Any, ctx: Optional[AuthContext] = None,
           actor_principal_id: Optional[str] = None, actor_kind: Optional[str] = None,
           since: Optional[datetime] = None, until: Optional[datetime] = None,
           limit: int = 100_000) -> dict[str, Any]:
    """The export bundle: every event in range (oldest first), the chain verification and the
    head anchor, so a reader can re-verify offline. Writes `audit.exported` in the same
    transaction (no export without its record)."""
    verification = verify_chain(org_id, db=db)
    where = ["org_id = :o"]
    params: dict[str, Any] = {"o": org_id, "n": int(limit)}
    if since:
        where.append("ts >= :since")
        params["since"] = since
    if until:
        where.append("ts < :until")
        params["until"] = until
    rows = db.execute(_sql(_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY seq LIMIT :n"),
                      params).mappings().all()
    events = [to_public(row_from_db(r)) for r in rows]
    top = head(org_id, db=db)
    details = {"count": len(events), "since": since.isoformat() if since else None,
               "until": until.isoformat() if until else None, "chain_ok": verification.ok,
               "head_seq": top[0] if top else None}
    event = (SecurityEvent.from_context(ctx, "audit.exported", Outcome.SUCCESS, details=details)
             if ctx is not None else
             SecurityEvent(org_id=org_id, event_type="audit.exported", outcome=Outcome.SUCCESS,
                           actor_principal_id=actor_principal_id, actor_kind=actor_kind,
                           details=details))
    record(event, db=db)
    return {
        "org_id": org_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "format": "boss-identity-audit/1",
        "hash": "sha256(prev_hash || canonical_json(row)); canonical_json = HASHED_FIELDS, sorted "
                "keys, no whitespace, UTF-8, ts ISO-8601 UTC with microseconds",
        "verification": {"ok": verification.ok, "checked": verification.checked,
                         "last_seq": verification.last_seq,
                         "first_break_seq": verification.first_break_seq},
        "anchor": {"seq": top[0], "hash": top[1].hex()} if top else None,
        "events": events,
    }


def daily_anchor(org_id: str, *, db: Any, project_id: Optional[str] = None,
                 now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Publish today's chain head into the activity log (`events`), once per day (§8.1). A later
    truncation of the chain is visible against the anchors."""
    top = head(org_id, db=db)
    if top is None:
        return None
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    if project_id is None:
        try:
            from ..config import get_settings
            project_id = get_settings().default_project_id
        except Exception:  # noqa: BLE001
            return None
    exists = db.execute(_sql("SELECT 1 FROM projects WHERE id = :p"), {"p": project_id}).first()
    if not exists:
        return None
    already = db.execute(_sql(
        "SELECT 1 FROM events WHERE project_id = :p AND event_type = 'identity.audit_anchor' "
        "AND data->>'day' = :d AND data->>'org_id' = :o"),
        {"p": project_id, "d": day, "o": org_id}).first()
    anchor = {"org_id": org_id, "day": day, "seq": top[0], "hash": top[1].hex()}
    if already:
        return anchor
    from .ids import new_id
    db.execute(_sql(
        "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data) "
        "VALUES (:id, :p, 'identity.audit_anchor', 'sys_identity', 'identity_security_events', "
        ":o, CAST(:data AS JSONB))"),
        {"id": new_id("evt"), "p": project_id, "o": org_id, "data": json.dumps(anchor)})
    return anchor


def last_verified(org_id: str, *, db: Any) -> Optional[dict[str, Any]]:
    """The most recent `audit.verified` event (posture: "the audit chain verified at")."""
    row = db.execute(_sql(
        "SELECT ts, outcome, details FROM identity_security_events WHERE org_id = :o "
        "AND event_type = 'audit.verified' ORDER BY seq DESC LIMIT 1"), {"o": org_id}
    ).mappings().first()
    if not row:
        return None
    return {"at": row["ts"], "ok": row["outcome"] == Outcome.SUCCESS.value,
            "details": row["details"]}


def redact(value: Any) -> Any:
    """The ONE redaction filter (IDENTITY.md §8.3), also installed at the logging root: drops
    Authorization/Cookie/X-Boss-CSRF, keys matching token|secret|password|code|assertion|credential,
    BOSS key shapes and JWT shapes (A80). Implementation: identity/redaction.py."""
    return redaction.redact(value)


# Any process that can write a security event redacts its logs too.
redaction.install()
