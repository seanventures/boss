"""The legacy bridges: today's shared credentials and claimed actors, each mapped to a real,
named principal so nothing locks out during migration. Owned by stream S5.

FROZEN IN PHASE 0: the bridge kinds, the reserved principal ids and the signatures. IDENTITY.md §12
and the operator decisions in §16 (OD1, OD2):

  - the shared console password: before the first Owner claims, it is the CLAIM credential; after,
    it maps to `usr_legacy_shared` with the Operator role for 30 days, then stops working (OD1);
  - BOSS_API_KEY: `sys_legacy_console` (Owner-equivalent fixed set) until the same cutoff;
  - BOSS_API_KEYS + BOSS_API_KEY_PRINCIPALS: hashed into identity_api_credentials(kind=legacy_env);
  - BOSS_CONSOLE_AUTH=network: a MIGRATION BRIDGE ONLY (OD2). Before the claim it may do exactly
    what today's console does plus claim the first Owner; after the claim it maps to
    `usr_network_operator` (network_bound, Operator) for at most 30 days, then refuses;
  - a claimed `actor` in a body is ignored for authorization, and logged when it differs.
"""
from __future__ import annotations

import hmac
import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from . import redaction
from .authn import AuthnError, RequestCredentials
from .principal import (AuthContext, AuthMethod, CredentialRef, Principal, PrincipalKind,
                        PrincipalStatus)

LEGACY_SHARED_PRINCIPAL = "usr_legacy_shared"
LEGACY_CONSOLE_PRINCIPAL = "sys_legacy_console"
NETWORK_OPERATOR_PRINCIPAL = "usr_network_operator"
LEGACY_CUTOFF_DAYS = 30
LEGACY_BRIDGE_ROLE = "operator"
# S5 additions (not part of the Phase 0 freeze; nothing else depends on their spelling yet)
LEGACY_SYSTEM_PREFIX = "sys_legacy_"
LEGACY_WORKER_PRINCIPAL = "sys_legacy_system"     # BOSS_WORKER_PRINCIPAL's key until M4
SCIM_LEGACY_PRINCIPAL = "sys_scim_legacy"         # BOSS_ENTERPRISE_SCIM_BEARER_TOKENS
#: the header a trusted proxy (the console container) sets on a network-trusted build
CONSOLE_AUTH_HEADER = "x-boss-console-auth"
#: Tailscale's CGNAT range is not "private" to ipaddress, but it is the tailnet
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


class LegacyKind(StrEnum):
    """identity_legacy_mappings.legacy_kind."""
    MEMBER_ID = "member_id"
    SHARED_PASSWORD = "shared_password"
    CONSOLE_API_KEY = "console_api_key"
    ENV_API_KEY = "env_api_key"
    NETWORK_MODE = "network_mode"
    CLAIMED_ACTOR = "claimed_actor"
    SCIM_BEARER = "scim_bearer"
    OIDC_BEARER = "oidc_bearer"


# The API imports this module (through identity_guard) at startup: the one redaction filter goes
# in at the logging root before the first request (A80).
redaction.install()

_RETIRED = "legacy_credential_retired"
_AUTH_METHOD = {
    LegacyKind.CONSOLE_API_KEY: AuthMethod.LEGACY_API_KEY,
    LegacyKind.ENV_API_KEY: AuthMethod.LEGACY_API_KEY,
    LegacyKind.SHARED_PASSWORD: AuthMethod.LEGACY_PASSWORD,
    LegacyKind.NETWORK_MODE: AuthMethod.NETWORK,
    LegacyKind.OIDC_BEARER: AuthMethod.OIDC_BEARER,
    LegacyKind.SCIM_BEARER: AuthMethod.API_KEY,
}
_USED_EVENT = {
    LegacyKind.CONSOLE_API_KEY: "legacy.api_key_used",
    LegacyKind.ENV_API_KEY: "legacy.api_key_used",
    LegacyKind.SHARED_PASSWORD: "legacy.password_used",
    LegacyKind.NETWORK_MODE: "legacy.network_used",
}
NETWORK_REF = "BOSS_CONSOLE_AUTH=network"


def _settings(settings: Any = None) -> Any:
    if settings is None:
        from ..config import get_settings
        return get_settings()
    return settings


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


# ---- which legacy path does a request use? (no database) -----------------------------------------

@dataclass(frozen=True)
class LegacyPath:
    kind: LegacyKind
    ref: str                     # never a secret: a name, a member id or a token hash
    member_id: Optional[str] = None


def _ip(value: Optional[str]) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(str(value).strip()) if value else None
    except ValueError:
        return None


def is_private_peer(value: Optional[str]) -> bool:
    """Today's network check: loopback, RFC 1918/4193 private ranges and the tailnet."""
    ip = _ip(value)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback or ip.is_private or (ip.version == 4 and ip in _TAILNET)) \
        and not ip.is_unspecified


def _trusted_proxy(peer: Optional[str], trusted: str) -> bool:
    ip = _ip(peer)
    if ip is None:
        return False
    for entry in (trusted or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets = [ipaddress.ip_network(entry, strict=False)]
        except ValueError:
            try:   # a NAME (compose's `web`): the console container
                nets = [ipaddress.ip_network(a[4][0]) for a in socket.getaddrinfo(entry, None)]
            except OSError:
                nets = []
        if any(ip in n for n in nets):
            return True
    return False


def network_mode_for(creds: RequestCredentials, settings: Any = None) -> bool:
    """Is this request on a network-trusted install? Set on the API (BOSS_CONSOLE_AUTH=network),
    or said by the console container itself, which is a trusted proxy, never by a client."""
    s = _settings(settings)
    if str(getattr(s, "legacy_console_auth", "") or "").strip().lower() == "network":
        return True
    said = (creds.headers or {}).get(CONSOLE_AUTH_HEADER, "")
    return said.strip().lower() == "network" and _trusted_proxy(
        creds.peer_ip, str(getattr(s, "trusted_proxies", "") or ""))


def _presented_key(creds: RequestCredentials) -> Optional[str]:
    header = (creds.headers or {}).get("x-boss-api-key", "")
    return header.strip() or creds.bearer


def classify(creds: RequestCredentials, *, settings: Any = None) -> Optional[LegacyPath]:
    """The legacy path a request uses, decided from what the transport middleware already
    verified (creds.legacy_auth_method) and the settings. Never reads the database; never trusts
    a body field or a client-set header for identity."""
    if creds is None:
        return None
    s = _settings(settings)
    method = (creds.legacy_auth_method or "").strip().lower()
    if method == "api_key":
        token = _presented_key(creds)
        if not token:
            return None
        console = str(getattr(s, "legacy_console_api_key", "") or "")
        if console and hmac.compare_digest(token.encode(), console.encode()):
            return LegacyPath(LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY", creds.legacy_member_id)
        from .migration import token_hash
        return LegacyPath(LegacyKind.ENV_API_KEY, token_hash(token), creds.legacy_member_id)
    if method in ("oidc", "saml") and creds.legacy_member_id:
        return LegacyPath(LegacyKind.OIDC_BEARER, f"member:{creds.legacy_member_id}",
                          creds.legacy_member_id)
    if method == "legacy_password":
        return LegacyPath(LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD")
    if method:
        return None
    has_credential = bool(_presented_key(creds)) or any(
        str(k).lower() == "__host-boss_session" for k in (creds.cookies or {}))
    if not has_credential and network_mode_for(creds, s) and is_private_peer(creds.client_ip):
        return LegacyPath(LegacyKind.NETWORK_MODE, NETWORK_REF)
    return None


# ---- the bridge ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Resolved:
    principal: Principal
    credential: Optional[CredentialRef]
    retire_after: Optional[datetime]
    retired: bool
    org_id: str


_CACHE_TTL = 5.0
_cache: dict[tuple[str, str], tuple[float, _Resolved]] = {}
_cache_lock = threading.Lock()
_usage: dict[tuple[str, str, str, str], list[Any]] = {}   # (org, kind, ref, pid) -> [n, last]
_events_today: set[tuple[str, str, str, date]] = set()
_usage_lock = threading.Lock()


#: (org, legacy kind, principal) -> that bridge's end, as _resolve last saw it (Sunset header).
_sunsets: dict[tuple[str, str, str], Optional[datetime]] = {}


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _sunsets.clear()


def note_sunset(org_id: str, kind: str, principal_id: str, until: Optional[datetime]) -> None:
    with _cache_lock:
        _sunsets[(org_id, kind, principal_id)] = until


def deprecation_headers(ctx: Any) -> list[tuple[bytes, bytes]]:
    """The response headers for a request on a legacy bridge: `Deprecation: true` and, when its
    cutoff is known, `Sunset: <HTTP date>`. Nothing for a real credential, or for the worker's
    and SCIM's machine keys, which have no people cutoff."""
    kind = getattr(ctx, "legacy", None)
    principal = getattr(ctx, "principal", None)
    if not kind or principal is None or principal.id in (LEGACY_WORKER_PRINCIPAL,
                                                         SCIM_LEGACY_PRINCIPAL):
        return []
    out = [(b"deprecation", b"true")]
    with _cache_lock:
        until = _sunsets.get((principal.org_id, str(kind), principal.id))
    if until is not None:
        from email.utils import format_datetime

        out.append((b"sunset", format_datetime(until.astimezone(timezone.utc), usegmt=True).encode()))
    return out


def bridge(creds: RequestCredentials, *, db: Any = None, now: Optional[datetime] = None,
           settings: Any = None) -> Optional[AuthContext]:
    """Map what the legacy transport middleware decided (creds.legacy_member_id /
    legacy_auth_method) to an AuthContext for its mapped principal, or None when no legacy path
    applies. Records use in identity_legacy_mappings and refuses a retired path."""
    path = classify(creds, settings=settings)
    if path is None:
        return None
    now = now or datetime.now(timezone.utc)
    if db is None:
        fast = _from_cache(path, creds, now)
        if fast is not None:
            return fast
        from ..db import begin
        with begin() as conn:
            return bridge(creds, db=conn, now=now, settings=settings)
    resolved = _resolve(path, db=db, now=now, settings=settings)
    if resolved is None:
        return None
    ref = resolved.credential.id if resolved.credential else path.ref
    _count_use(resolved.org_id, path.kind.value, ref, resolved.principal.id, now)
    if resolved.retired:
        raise AuthnError(_RETIRED, 401)
    if resolved.retire_after is not None and resolved.retire_after <= now:
        # Every bridge has an end (M3): the three people bridges retire through retire_due (the
        # console key may be deferred, but never past the absolute cutoff); an env key's or an
        # OIDC bearer's end (its own expiry, or the absolute cutoff for a person's) just refuses.
        if path.kind in (LegacyKind.CONSOLE_API_KEY, LegacyKind.SHARED_PASSWORD,
                         LegacyKind.NETWORK_MODE):
            from . import migration
            done = migration.retire_due(resolved.org_id, db=db, now=now)
            hard = migration.hard_cutoff(db, resolved.org_id)
            if any(r.kind == path.kind.value and r.retired for r in done) or (
                    hard is not None and now >= hard):
                raise AuthnError(_RETIRED, 401)
        else:
            raise AuthnError(_RETIRED, 401)
    if not resolved.principal.active:
        raise AuthnError("account_disabled", 401)
    ctx = AuthContext(principal=resolved.principal, method=_AUTH_METHOD[path.kind],
                      credential=resolved.credential, legacy=path.kind.value,
                      ip=creds.client_ip, user_agent=(creds.headers or {}).get("user-agent"),
                      request_id=creds.request_id)
    _event_once(db, resolved.org_id, path, ctx, now)
    return ctx


def _from_cache(path: LegacyPath, creds: RequestCredentials, now: datetime
                ) -> Optional[AuthContext]:
    """The hot path (every console request carries the key): no database round trip while the
    mapping is cached (<= 5 s), live, not due, and today's use event is already written."""
    with _cache_lock:
        hit = _cache.get((path.kind.value, path.ref))
    if not hit or hit[0] <= time.monotonic():
        return None
    resolved = hit[1]
    if (resolved.retired or not resolved.principal.active
            or (resolved.retire_after is not None and resolved.retire_after <= now)):
        return None
    ref = resolved.credential.id if resolved.credential else path.ref
    with _usage_lock:
        if (resolved.org_id, path.kind.value, ref, now.date()) not in _events_today:
            return None
    _count_use(resolved.org_id, path.kind.value, ref, resolved.principal.id, now)
    return AuthContext(principal=resolved.principal, method=_AUTH_METHOD[path.kind],
                       credential=resolved.credential, legacy=path.kind.value,
                       ip=creds.client_ip, user_agent=(creds.headers or {}).get("user-agent"),
                       request_id=creds.request_id)


def _resolve(path: LegacyPath, *, db: Any, now: datetime, settings: Any) -> Optional[_Resolved]:
    key = (path.kind.value, path.ref)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    from . import migration
    org_id = migration.ensure_seeded(db, settings=settings, now=now)
    if org_id is None:
        return None
    credential: Optional[CredentialRef] = None
    ref = path.ref
    if path.kind == LegacyKind.ENV_API_KEY:
        cred = _env_credential(db, path.ref)
        if cred is None:   # a key added to the env since the seed: map it now
            migration.seed(db, settings=_settings(settings), org_id=org_id, now=now)
            cred = _env_credential(db, path.ref)
        if cred is None or cred["revoked_at"] is not None:
            return None
        ref = cred["id"]
        credential = CredentialRef(id=cred["id"], kind="legacy_env",
                                   principal_id=cred["principal_id"],
                                   scopes=frozenset(cred["scopes"] or ()),
                                   expires_at=cred["expires_at"])
    if path.kind == LegacyKind.OIDC_BEARER:
        member = path.member_id or ""
        target = migration.mapping(db, org_id, LegacyKind.MEMBER_ID, member)
        if target and target["principal_id"].startswith("sys_"):
            pid = target["principal_id"]
        else:
            pid = migration.member_service_account(db, org_id, member)
        migration.put_mapping(db, org_id, LegacyKind.OIDC_BEARER, path.ref, pid)
    if path.kind == LegacyKind.NETWORK_MODE and not migration.mapping(db, org_id, path.kind, ref):
        # first network request on an install whose API was not told: the bridge starts here
        migration.put_mapping(db, org_id, path.kind, ref, NETWORK_OPERATOR_PRINCIPAL,
                              migration.LEGACY_BRIDGE_ROLE_KEY)
        if migration.claimed_at(db, org_id) is None:
            migration.activate_network_operator(db, org_id)
        else:
            migration._apply_claimed_state(db, org_id, _settings(settings), now,
                                           started=migration.claimed_at(db, org_id))
    m = migration.mapping(db, org_id, path.kind, ref)
    if m is None:
        return None
    row = db.execute(_sql(
        "SELECT id, kind, org_id, status, display_name, network_bound, legacy_member_ids FROM "
        "identity_principals WHERE id = :p"), {"p": m["principal_id"]}).mappings().first()
    if row is None:
        return None
    principal = Principal(id=row["id"], kind=PrincipalKind(row["kind"]), org_id=row["org_id"],
                          status=PrincipalStatus(row["status"]), display_name=row["display_name"],
                          network_bound=bool(row["network_bound"]),
                          legacy_member_ids=tuple(row["legacy_member_ids"] or ()))
    # M3: the absolute cutoff caps every bridge a PERSON uses; a machine member's env key (the
    # worker's, sys_*) is not a people bridge. A legacy env key's own expiry always applies.
    retire_after = m["retire_after"]
    people_bridge = path.kind in (LegacyKind.CONSOLE_API_KEY, LegacyKind.SHARED_PASSWORD,
                                  LegacyKind.NETWORK_MODE) or not principal.id.startswith("sys_")
    if people_bridge:
        hard = migration.hard_cutoff(db, org_id)
        if hard is not None:
            retire_after = hard if retire_after is None else min(retire_after, hard)
    if credential is not None and credential.expires_at is not None:
        retire_after = (credential.expires_at if retire_after is None
                        else min(retire_after, credential.expires_at))
    resolved = _Resolved(principal, credential, retire_after, m["retired_at"] is not None,
                         org_id)
    with _cache_lock:
        _cache[key] = (time.monotonic() + _CACHE_TTL, resolved)
        _sunsets[(org_id, path.kind.value, principal.id)] = retire_after
    return resolved


def _env_credential(db: Any, digest: str) -> Optional[dict[str, Any]]:
    row = db.execute(_sql(
        "SELECT id, principal_id, scopes, expires_at, revoked_at FROM identity_api_credentials "
        "WHERE token_hash = :h AND kind = 'legacy_env'"), {"h": digest}).mappings().first()
    return dict(row) if row else None


def _count_use(org_id: str, kind: str, ref: str, principal_id: str, now: datetime) -> None:
    """Count every attempt on a legacy path (flushed to identity_legacy_mappings by flush_usage):
    posture's counts and last use, and the console key's retirement deferral, read it."""
    with _usage_lock:
        entry = _usage.setdefault((org_id, kind, ref, principal_id), [0, now])
        entry[0] += 1
        entry[1] = max(entry[1], now)


def _event_once(db: Any, org_id: str, path: LegacyPath, ctx: AuthContext, now: datetime) -> None:
    """Write the `legacy.*_used` event once per path per day, so posture can show use without
    flooding the hash chain (A5's rule for shadow denials, applied here too)."""
    ref = ctx.credential.id if ctx.credential else path.ref
    marker = (org_id, path.kind.value, ref, now.date())
    with _usage_lock:
        first_today = marker not in _events_today
    if first_today and path.kind in _USED_EVENT:
        from .audit import Outcome, SecurityEvent, record
        record(SecurityEvent.from_context(ctx, _USED_EVENT[path.kind], Outcome.INFO,
                                          target_type="legacy_path", target_id=path.kind.value,
                                          details={"path": path.kind.value, "ref": ref}), db=db)
        flush_usage(db)
    with _usage_lock:      # only once the event is written: a failed write is retried next time
        _events_today.add(marker)


def flush_usage(db: Any) -> int:
    """Write the in-process use counters to identity_legacy_mappings."""
    with _usage_lock:
        pending = {k: list(v) for k, v in _usage.items() if v[0]}
        for k in pending:
            _usage[k][0] = 0
    for (org_id, kind, ref, _pid), (count, last) in pending.items():
        db.execute(_sql(
            "UPDATE identity_legacy_mappings SET use_count = use_count + :n, last_used_at = "
            "GREATEST(COALESCE(last_used_at, :t), :t), first_used_at = COALESCE(first_used_at, "
            ":t) WHERE org_id = :o AND legacy_kind = :k AND legacy_ref = :r"),
            {"n": count, "t": last, "o": org_id, "k": kind, "r": ref})
    return len(pending)


def forget_usage() -> None:
    """Tests: drop the in-process counters and the once-a-day markers."""
    with _usage_lock:
        _usage.clear()
        _events_today.clear()
    invalidate_cache()


# ---- what other streams ask of the bridge --------------------------------------------------------

def system_permissions(principal_id: str, *, db: Any = None) -> Optional[frozenset[str]]:
    """S2: the fixed set of a legacy SYSTEM principal (IDENTITY.md §6.6 step 2 reads fixed sets
    for system principals). None for a principal that is not a legacy one.

    sys_legacy_console (BOSS_API_KEY) depends on the claim: the full Owner set before the first
    Owner is claimed; from the claim, the Owner set minus every identity write
    (CONSOLE_AFTER_CLAIM_SET); empty once its path is retired at the cutoff."""
    from . import migration
    fixed = migration.legacy_system_set(principal_id)
    if fixed is None:
        return None
    if principal_id in (LEGACY_WORKER_PRINCIPAL, SCIM_LEGACY_PRINCIPAL) or not fixed:
        return fixed              # minimal fixed sets: nothing identity-writing to narrow
    if db is None:
        from ..db import begin
        with begin() as conn:
            return system_permissions(principal_id, db=conn)
    if principal_id == LEGACY_CONSOLE_PRINCIPAL:
        row = db.execute(_sql(
            "SELECT m.retired_at, p.org_id FROM identity_principals p LEFT JOIN "
            "identity_legacy_mappings m ON m.legacy_kind = 'console_api_key' AND m.principal_id = p.id "
            "WHERE p.id = :p"), {"p": principal_id}).first()
        if row is None:
            return fixed              # not seeded yet: nothing is claimed either
        if row[0] is not None:
            return frozenset()
        if migration.claimed_at(db, row[1]) is not None:
            return migration.CONSOLE_AFTER_CLAIM_SET
        return fixed
    # H3: every other legacy system principal (metatron, coo, ...) gets the console key's
    # treatment: no identity writes from the claim, and nothing at all from the cutoff.
    org = db.execute(_sql("SELECT org_id FROM identity_principals WHERE id = :p"),
                     {"p": principal_id}).scalar()
    if org is None:
        return fixed
    claimed = migration.claimed_at(db, org)
    if claimed is None:
        return fixed
    from datetime import timedelta
    from .policy import get_policy

    cutoff = claimed + timedelta(days=get_policy(org, db=db).legacy_cutoff_days)
    hard = migration.hard_cutoff(db, org)          # M3: never past claim + 30 (+ one extension)
    if hard is not None:
        cutoff = min(cutoff, hard)
    if datetime.now(timezone.utc) >= cutoff:
        return frozenset()
    return migration.CONSOLE_AFTER_CLAIM_SET


def step_up_exempt(ctx: AuthContext) -> bool:
    """S2: a request on a legacy bridge cannot step up (no session, no MFA), and the bridges
    exist to behave exactly as today, where nothing asked for step-up (D25, D27, §12 M2). So a
    step-up-flagged permission the bridged principal holds is not refused for lack of step-up.
    Real sessions, PATs and run tokens never get this; the one session that does is the shared
    console password's (it has no factor to step up with, and stays the Operator it was)."""
    if not ctx.legacy or ctx.run_token is not None:
        return False
    return ctx.session is None or (ctx.principal.id == LEGACY_SHARED_PRINCIPAL
                                   and ctx.legacy == LegacyKind.SHARED_PASSWORD.value)


def is_bridge_principal(principal_id: str) -> bool:
    """Legacy and bridge principals never count as people (D28, D29) or as Owners."""
    return principal_id in (LEGACY_SHARED_PRINCIPAL, NETWORK_OPERATOR_PRINCIPAL,
                            LEGACY_CONSOLE_PRINCIPAL) or principal_id.startswith(
        LEGACY_SYSTEM_PREFIX) or principal_id == SCIM_LEGACY_PRINCIPAL


@dataclass(frozen=True)
class SharedPasswordResult:
    """What a presented shared console password means now. `claim`: before the first Owner it
    is the claim credential (S1 shows "Claim this BOSS"). `ok`: after the claim it signs in as
    usr_legacy_shared (Operator) until the cutoff. `retired`/`invalid`/`not_configured`."""
    status: str
    principal_id: Optional[str] = None
    retire_after: Optional[datetime] = None


def check_shared_password(candidate: str, *, db: Any, settings: Any = None,
                          now: Optional[datetime] = None) -> SharedPasswordResult:
    """For S1's sign-in and claim: constant-time check of the shared console password."""
    s = _settings(settings)
    configured = str(getattr(s, "legacy_console_password", "") or "")
    if not configured:
        return SharedPasswordResult("not_configured")
    if not hmac.compare_digest((candidate or "").encode(), configured.encode()):
        return SharedPasswordResult("invalid")
    from . import migration
    now = now or datetime.now(timezone.utc)
    org_id = migration.ensure_seeded(db, settings=s, now=now)
    if org_id is None or migration.claimed_at(db, org_id) is None:
        return SharedPasswordResult("claim")
    migration.retire_due(org_id, db=db, now=now)
    m = migration.mapping(db, org_id, LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD")
    if m is None or m["retired_at"] is not None:
        return SharedPasswordResult("retired")
    return SharedPasswordResult("ok", LEGACY_SHARED_PRINCIPAL, m["retire_after"])


def scim_bearer(token: str, *, db: Any) -> Optional[AuthContext]:
    """For S1's SCIM router: a raw legacy SCIM bearer token (BOSS_ENTERPRISE_SCIM_BEARER_TOKENS),
    hashed and looked up; works until rotated into a boss_scim_ key (§12)."""
    from .migration import token_hash
    row = db.execute(_sql(
        "SELECT c.id, c.principal_id, c.scopes, c.revoked_at, p.org_id, p.status, p.display_name "
        "FROM identity_api_credentials c JOIN identity_principals p ON p.id = c.principal_id "
        "WHERE c.token_hash = :h AND c.kind = 'scim'"), {"h": token_hash(token or "")}
    ).mappings().first()
    if row is None or row["revoked_at"] is not None or row["status"] != "active":
        return None
    principal = Principal(row["principal_id"], PrincipalKind.SYSTEM, row["org_id"],
                          PrincipalStatus.ACTIVE, row["display_name"])
    return AuthContext(principal, AuthMethod.API_KEY, legacy=LegacyKind.SCIM_BEARER.value,
                       credential=CredentialRef(row["id"], "scim", row["principal_id"],
                                                frozenset(row["scopes"] or ())))


# ---- claimed actors ------------------------------------------------------------------------------

_claims_noted: set[tuple[str, str, date]] = set()
_claims_lock = threading.Lock()


def note_claimed_actor(ctx: AuthContext, claimed: Optional[str], *, db: Any = None) -> None:
    """Record `legacy.claimed_actor_ignored` when a body's `actor` differs from ctx.principal.

    The claimed string decides nothing (R6); this only makes the difference visible. A claim that
    names the principal itself, or one of its legacy member ids ("ceo" for the Owner who took
    over ceo), is not a difference. Written once per (principal, claimed) per day."""
    if ctx is None or not claimed:
        return
    claimed = str(claimed).strip()[:120]
    if claimed in (ctx.principal.id, *ctx.principal.legacy_member_ids):
        return
    key = (ctx.principal.id, claimed, date.today())
    with _claims_lock:
        if key in _claims_noted:
            return
        _claims_noted.add(key)
    if db is None:
        from ..db import begin
        with begin() as conn:
            return note_claimed_actor_now(ctx, claimed, db=conn)
    note_claimed_actor_now(ctx, claimed, db=db)


def note_claimed_actor_now(ctx: AuthContext, claimed: str, *, db: Any) -> None:
    from .audit import Outcome, SecurityEvent, record
    record(SecurityEvent.from_context(ctx, "legacy.claimed_actor_ignored", Outcome.INFO,
                                      target_type="claimed_actor", target_id=claimed,
                                      details={"claimed_actor": claimed}), db=db)


def forget_claimed_actor_notes() -> None:
    with _claims_lock:
        _claims_noted.clear()


def actor_labels(org_id: str, actors: Any, *, db: Any) -> dict[str, str]:
    """History rendering in bulk: {stored actor -> "Param (was: ceo)" | "Param"} for every actor
    that maps to a principal (by its id, or a legacy member id it took over). One query."""
    wanted = sorted({str(a) for a in actors if a})
    if not wanted:
        return {}
    rows = db.execute(_sql(
        "SELECT m.legacy_ref AS actor, p.display_name FROM identity_legacy_mappings m JOIN "
        "identity_principals p ON p.id = m.principal_id WHERE m.org_id = :o AND m.legacy_kind = "
        "'member_id' AND m.legacy_ref = ANY(:a) UNION ALL SELECT p.id, p.display_name FROM "
        "identity_principals p WHERE p.org_id = :o AND p.id = ANY(:a)"),
        {"o": org_id, "a": wanted}).all()
    out: dict[str, str] = {}
    for actor, name in rows:
        if actor in out:
            continue
        out[actor] = name if actor.startswith(("usr_", "sva_", "agt_", "sys_")) else f"{name} (was: {actor})"
    return out


def resolve_actor(org_id: str, actor: Optional[str], *, db: Any) -> Optional[dict[str, Any]]:
    """History rendering (§12 "History"): a stored actor string -> the principal it maps to now,
    e.g. "ceo" -> {"principal_id": "usr_…", "display_name": "Param", "was": "ceo"}."""
    if not actor:
        return None
    row = db.execute(_sql(
        "SELECT p.id, p.display_name, p.kind, p.status FROM identity_principals p WHERE "
        "p.org_id = :o AND (p.id = :a OR p.id = (SELECT principal_id FROM "
        "identity_legacy_mappings WHERE org_id = :o AND legacy_kind = 'member_id' AND "
        "legacy_ref = :a))"), {"o": org_id, "a": actor}).mappings().first()
    if row is None:
        return None
    return {"principal_id": row["id"], "display_name": row["display_name"],
            "kind": row["kind"], "status": row["status"],
            "was": None if row["id"] == actor else actor,
            "label": row["display_name"] if row["id"] == actor
            else f"{row['display_name']} (was: {actor})"}
