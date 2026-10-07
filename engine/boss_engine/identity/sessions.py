"""Opaque server-side sessions for people (IDENTITY.md §4.3, D2).

    token   bss_ + 32 random bytes (base64url); only SHA-256(token) is stored
    cookie  __Host-boss_session; Secure; HttpOnly; SameSite=Lax; Path=/; no Domain
    expiry  idle (policy, default 12 h) and absolute (policy, default 7 d), checked per request
    rotate  a NEW token on sign-in, MFA completion and step-up; the old row is revoked and points
            at its replacement. A pre-auth cookie never becomes a session (A7).

No cache: every request reads the row, so a revocation takes effect on the very next request
(J6, A29) in every API process.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import ids
from .authn_support import get_policy, keyed_hash, random_token, sha256_hex, utcnow
from .principal import SessionRef

SESSION_PREFIX = "bss_"
#: last_seen_at (and the idle expiry it drives) is written at most this often per session.
TOUCH_SECONDS = 60


@dataclass(frozen=True)
class SessionRow:
    id: str
    principal_id: str
    auth_methods: tuple[str, ...]
    mfa_at: Optional[datetime]
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    ip: Optional[str]
    user_agent: Optional[str]
    device_id_hash: Optional[str]
    revoked_at: Optional[datetime]
    #: H1: until when this session may enrol a person's FIRST second factor (None: never).
    enrol_grant_until: Optional[datetime] = None

    def ref(self) -> SessionRef:
        return SessionRef(self.id, self.mfa_at, self.auth_methods, self.idle_expires_at,
                          self.absolute_expires_at)


@dataclass(frozen=True)
class IssuedSession:
    token: str            # the cookie value; never stored, never logged
    session_id: str
    absolute_expires_at: datetime
    idle_expires_at: datetime

    def __repr__(self) -> str:  # the token must not reach a log through a repr
        return f"IssuedSession(session_id={self.session_id!r})"


_COLUMNS = ("id, principal_id, auth_methods, mfa_at, created_at, last_seen_at, idle_expires_at, "
            "absolute_expires_at, host(ip) AS ip, user_agent, device_id_hash, revoked_at, enrol_grant_until")


_COLUMNS_S = ("s.id, s.principal_id, s.auth_methods, s.mfa_at, s.created_at, s.last_seen_at, "
              "s.idle_expires_at, s.absolute_expires_at, host(s.ip) AS ip, s.user_agent, "
              "s.device_id_hash, s.revoked_at, s.enrol_grant_until")


def _row(r: Any) -> SessionRow:
    return SessionRow(r.id, r.principal_id, tuple(r.auth_methods or ()), r.mfa_at, r.created_at,
                      r.last_seen_at, r.idle_expires_at, r.absolute_expires_at, r.ip, r.user_agent,
                      r.device_id_hash, r.revoked_at, getattr(r, "enrol_grant_until", None))


def csrf_token(session_id: str) -> str:
    """X-Boss-CSRF = HMAC(server key, session id). Delivered by GET /v1/identity/me."""
    mac = hmac.new(keyed_hash("csrf-key", "v1").encode(), session_id.encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def _lifetimes(db: Any, org_id: str) -> tuple[timedelta, timedelta]:
    pol = get_policy(org_id, db=db)
    idle = min(max(int(pol.session_idle_minutes), 15), 24 * 60)
    absolute = min(max(int(pol.session_absolute_hours), 1), 30 * 24)
    return timedelta(minutes=idle), timedelta(hours=absolute)


def issue(db: Any, *, principal_id: str, org_id: str, methods: Iterable[str],
          mfa_at: Optional[datetime], ip: Optional[str], user_agent: Optional[str],
          device_id_hash: Optional[str], replaces: Optional[str] = None,
          absolute_expires_at: Optional[datetime] = None,
          enrol_grant_until: Optional[datetime] = None) -> IssuedSession:
    """Create a session row and return its token (the only time the token exists server-side).
    `replaces` revokes that session and links it here (rotation). A rotation keeps the original
    absolute expiry: rotating never extends a session's life."""
    token = random_token(SESSION_PREFIX)
    sid = ids.new_id("ses")
    now = utcnow()
    idle, absolute = _lifetimes(db, org_id)
    abs_at = absolute_expires_at or (now + absolute)
    idle_at = min(now + idle, abs_at)
    db.execute(text(
        "INSERT INTO identity_sessions (id, token_hash, principal_id, auth_methods, mfa_at, "
        "idle_expires_at, absolute_expires_at, ip, user_agent, device_id_hash, enrol_grant_until) VALUES "
        "(:id, :th, :p, :m, :mfa, :idle, :abs, CAST(:ip AS inet), :ua, :dev, :grant)"),
        {"id": sid, "th": sha256_hex(token), "p": principal_id, "m": sorted(set(methods)),
         "mfa": mfa_at, "idle": idle_at, "abs": abs_at, "ip": _inet(ip),
         "ua": (user_agent or "")[:400] or None, "dev": device_id_hash, "grant": enrol_grant_until})
    if replaces:
        db.execute(text("UPDATE identity_sessions SET revoked_at = now(), revoked_reason = 'rotated', "
                        "replaced_by = :new WHERE id = :old AND revoked_at IS NULL"),
                   {"new": sid, "old": replaces})
    return IssuedSession(token, sid, abs_at, idle_at)


def _inet(ip: Optional[str]) -> Optional[str]:
    import ipaddress

    try:
        return str(ipaddress.ip_address((ip or "").split("%", 1)[0])) if ip else None
    except ValueError:
        return None


def lookup(db: Any, token: Optional[str]) -> Optional[SessionRow]:
    """The live session for a cookie value, or None (unknown, revoked, idle- or absolute-expired).
    The principal's status is checked by the caller (authn.authenticate), live."""
    if not token or not token.startswith(SESSION_PREFIX) or len(token) > 100:
        return None
    r = db.execute(text(f"SELECT {_COLUMNS} FROM identity_sessions WHERE token_hash = :th"),
                   {"th": sha256_hex(token)}).first()
    if r is None:
        return None
    row = _row(r)
    now = utcnow()
    if row.revoked_at is not None or now >= row.idle_expires_at or now >= row.absolute_expires_at:
        return None
    return row


def touch(db: Any, row: SessionRow, *, org_id: str) -> None:
    """Slide the idle expiry, at most once a minute."""
    now = utcnow()
    if (now - row.last_seen_at).total_seconds() < TOUCH_SECONDS:
        return
    idle, _ = _lifetimes(db, org_id)
    db.execute(text("UPDATE identity_sessions SET last_seen_at = now(), idle_expires_at = "
                    "LEAST(now() + make_interval(secs => :idle), absolute_expires_at) "
                    "WHERE id = :id AND revoked_at IS NULL"),
               {"idle": int(idle.total_seconds()), "id": row.id})


def by_id(db: Any, session_id: str) -> Optional[SessionRow]:
    r = db.execute(text(f"SELECT {_COLUMNS} FROM identity_sessions WHERE id = :id"),
                   {"id": session_id}).first()
    return _row(r) if r else None


def list_live(db: Any, principal_id: Optional[str] = None, *, org_id: Optional[str] = None
              ) -> list[SessionRow]:
    sql = (f"SELECT {_COLUMNS_S} FROM identity_sessions s "
           "JOIN identity_principals p ON p.id = s.principal_id WHERE s.revoked_at IS NULL AND "
           "s.idle_expires_at > now() AND s.absolute_expires_at > now()")
    params: dict[str, Any] = {}
    if principal_id:
        sql += " AND s.principal_id = :p"
        params["p"] = principal_id
    if org_id:
        sql += " AND p.org_id = :org"
        params["org"] = org_id
    return [_row(r) for r in db.execute(text(sql + " ORDER BY s.last_seen_at DESC"), params)]


def revoke(db: Any, session_id: str, *, by: Optional[str], reason: str) -> bool:
    res = db.execute(text("UPDATE identity_sessions SET revoked_at = now(), revoked_by = :by, "
                          "revoked_reason = :r WHERE id = :id AND revoked_at IS NULL RETURNING id"),
                     {"by": by, "r": reason[:200], "id": session_id}).first()
    return res is not None


def revoke_all(db: Any, principal_id: str, *, by: Optional[str], reason: str,
               except_session: Optional[str] = None) -> int:
    rows = db.execute(text(
        "UPDATE identity_sessions SET revoked_at = now(), revoked_by = :by, revoked_reason = :r "
        "WHERE principal_id = :p AND revoked_at IS NULL AND (CAST(:keep AS text) IS NULL OR id <> :keep) RETURNING id"),
        {"by": by, "r": reason[:200], "p": principal_id, "keep": except_session}).fetchall()
    return len(rows)


def device_is_familiar(db: Any, principal_id: str, device_cookie: Optional[str]) -> bool:
    """A device that has completed a sign-in for this account before (§4.10 soft lock exemption).
    The device cookie is never a credential: it only decides who the soft lock applies to."""
    if not device_cookie or len(device_cookie) > 100:
        return False
    r = db.execute(text("SELECT 1 FROM identity_sessions WHERE principal_id = :p AND "
                        "device_id_hash = :d LIMIT 1"),
                   {"p": principal_id, "d": sha256_hex(device_cookie)}).first()
    return r is not None


def step_up_fresh_until(row_or_ref: Any, *, window_seconds: int = 600) -> Optional[datetime]:
    mfa_at = getattr(row_or_ref, "mfa_at", None)
    if mfa_at is None:
        return None
    until = mfa_at + timedelta(seconds=window_seconds)
    return until if until > utcnow() else None


# ---- cookies ---------------------------------------------------------------------------------------

def set_cookie_header(name: str, value: str, *, max_age: Optional[int], samesite: str = "Lax") -> str:
    """A `__Host-` cookie: Secure, HttpOnly, SameSite=Lax, Path=/ and no Domain — always (A19).
    The value is a server-minted token, so it needs no quoting. `samesite="None"` exists ONLY for
    the pre-auth cookie of a flow whose IdP POSTs back cross-site (SAML ACS, Apple form_post);
    a session cookie is always Lax."""
    if not name.startswith("__Host-"):
        raise ValueError("identity cookies are always __Host- cookies")
    if samesite not in ("Lax", "Strict", "None"):
        raise ValueError("samesite must be Lax, Strict or None")
    if samesite == "None" and not name.endswith("_preauth"):
        raise ValueError("only the pre-auth cookie may be SameSite=None")
    parts = [f"{name}={value}", "Path=/", "Secure", "HttpOnly", f"SameSite={samesite}"]
    if max_age is not None:
        parts.append(f"Max-Age={max(0, int(max_age))}")
    return "; ".join(parts)


def clear_cookie_header(name: str) -> str:
    return set_cookie_header(name, "", max_age=0)
