"""The security event log: append-only, hash-chained per org. Owned by stream S5.

FROZEN IN PHASE 0: SecurityEvent, the event-type list, the canonical form and the chain hash. Bodies
marked NotImplementedError are S5's (IDENTITY_BUILD.md §3). IDENTITY.md §8.

`record` writes in the CALLER'S transaction: if the event cannot be written, the change it describes
fails too (D22, A76). There is no write API; events come only from code paths.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Optional

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


def record(event: SecurityEvent, *, db: Any) -> RecordedEvent:
    """Append one event in the caller's transaction (per-org advisory lock for seq). Refuses an
    event type not in EVENT_TYPES and any `details` key matching the redaction pattern."""
    raise NotImplementedError("identity.audit.record is stream S5's")


def verify_chain(org_id: str, *, db: Any, from_seq: Optional[int] = None) -> ChainVerification:
    """Walk the chain and report the first break (A75). Checkpoints from pruning are honoured."""
    raise NotImplementedError("identity.audit.verify_chain is stream S5's")


def redact(value: Any) -> Any:
    """The ONE redaction filter (IDENTITY.md §8.3), also installed at the logging root: drops
    Authorization/Cookie/X-Boss-CSRF, keys matching token|secret|password|code|assertion|credential,
    BOSS key shapes and JWT shapes (A80)."""
    raise NotImplementedError("identity.audit.redact is stream S5's")
