"""Customer pools: the app's own users, a SEPARATE PRINCIPAL KIND (§6.3, S6.1, S6.2).

A pool user (`opu_`) has no identity_principals row, holds no role binding, and is never seen by
BOSS's PDP. Its session is `oidc_sessions` behind its own cookie, `__Host-boss_app_session`
(`bas_` tokens), which BOSS's authn never reads; its challenges are `oidc_app_challenges` behind
`__Host-boss_app_preauth`. Every lookup names the POOL: a session, challenge or user of one pool
is nothing in another pool or org.

It reuses identity's CODE, not its tables (the "realm" is the pool): Argon2id hashing, the
fair hash queue, the new-password rules and optional breach check (passwords.py), TOTP's
constant-time matching with replay prevention (totp.py), the WebAuthn library calls and RP rules
(webauthn.py's rp_for: the RP ID is the BOSS host), the rate-limit table (ratelimit.py, `oidc.*`
buckets), and the mail sender (email_signin.DELIVER).

The `__Host-` prefix requires Path=/, so the cookie cannot be scoped to /oidc/ as §6.3 sketched:
it is host-only and never forwarded to the API by the console except on /oidc/* browser steps,
and never read outside this module.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .util import audit, b64u, new_id, now, random_token, sha256_hex

APP_SESSION_COOKIE = "__Host-boss_app_session"
APP_PREAUTH_COOKIE = "__Host-boss_app_preauth"
SESSION_PREFIX = "bas_"
PREAUTH_PREFIX = "bap_"
SESSION_IDLE = timedelta(hours=12)
SESSION_ABSOLUTE = timedelta(days=7)
CHALLENGE_TTL = timedelta(minutes=10)
CHALLENGE_ATTEMPTS = 5
ACCOUNT_FAILURES = (10, 900)        # per pool user (by typed email), then a soft lock
IP_FAILURES = (60, 900)
MFA_FAILURES = (10, 900)
SIGNUPS_PER_IP = (10, 3600)
CODES_PER_ACCOUNT = (5, 3600)
MAX_PASSKEYS = 10
TOTP_VAULT_PROVIDER = "boss-oidc-totp"
_EMAIL = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
USER_COLUMNS = "id, pool_id, email, email_verified, name, status, password_hash, created_at, last_sign_in_at"


class RealmError(Exception):
    """A refusal shown to the end user. `code` is stable; the page words it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---- users -------------------------------------------------------------------------------------

def normalise_email(email: Any) -> str:
    value = str(email or "").strip()
    if len(value) > 254 or not _EMAIL.fullmatch(value):
        raise RealmError("bad_email")
    return value


def load_user(db: Any, *, pool_id: str, user_id: Any) -> Optional[dict[str, Any]]:
    if not isinstance(user_id, str):
        return None
    r = db.execute(text(f"SELECT {USER_COLUMNS} FROM oidc_pool_users WHERE id = :id AND pool_id = :p"),
                   {"id": user_id, "p": pool_id}).mappings().first()
    return dict(r) if r else None


def find_user(db: Any, *, pool_id: str, email: str) -> Optional[dict[str, Any]]:
    r = db.execute(text(f"SELECT {USER_COLUMNS} FROM oidc_pool_users WHERE pool_id = :p AND lower(email) = lower(:e)"),
                   {"p": pool_id, "e": email}).mappings().first()
    return dict(r) if r else None


def public_view(u: Mapping[str, Any], *, db: Any = None) -> dict[str, Any]:
    out = {k: u.get(k) for k in ("id", "pool_id", "email", "email_verified", "name", "status")}
    out["has_password"] = bool(u.get("password_hash"))
    for k in ("created_at", "last_sign_in_at"):
        out[k] = u[k].isoformat() if u.get(k) else None
    if db is not None:
        out["factors"] = [r[0] for r in db.execute(text(
            "SELECT DISTINCT kind FROM oidc_pool_user_factors WHERE pool_user_id = :u ORDER BY kind"), {"u": u["id"]})]
    return out


def list_users(db: Any, *, pool_id: str) -> list[dict[str, Any]]:
    rows = db.execute(text(f"SELECT {USER_COLUMNS} FROM oidc_pool_users WHERE pool_id = :p ORDER BY created_at LIMIT 1000"),
                      {"p": pool_id}).mappings()
    return [public_view(dict(r), db=db) for r in rows]


def create_user(db: Any, *, pool: Mapping[str, Any], email: str, name: Optional[str], password_hash: Optional[str],
                status: str, verified: bool) -> dict[str, Any]:
    uid = new_id("opu")
    db.execute(text("INSERT INTO oidc_pool_users (id, pool_id, email, email_verified, name, status, password_hash) "
                    "VALUES (:id, :p, :e, :v, :n, :s, :h)"),
               {"id": uid, "p": pool["id"], "e": email, "v": verified, "n": (name or "").strip()[:120] or None,
                "s": status, "h": password_hash})
    return load_user(db, pool_id=pool["id"], user_id=uid)


def disable_everything(db: Any, *, user_id: str, reason: str) -> None:
    """Sessions, challenges and every grant (so every token) of this pool user end now."""
    from .tokens import revoke_subject_grants

    db.execute(text("UPDATE oidc_sessions SET revoked_at = now() WHERE pool_user_id = :u AND revoked_at IS NULL"),
               {"u": user_id})
    db.execute(text("UPDATE oidc_app_challenges SET used_at = now() WHERE pool_user_id = :u AND used_at IS NULL"),
               {"u": user_id})
    revoke_subject_grants(db, client_id=None, subject_ref=user_id, reason=reason)


def admin_update(db: Any, *, org_id: str, actor: str, pool: Mapping[str, Any], user_id: str,
                 status: Optional[str], reset_mfa: bool, name: Optional[str]) -> dict[str, Any]:
    user = load_user(db, pool_id=pool["id"], user_id=user_id)
    if user is None:
        raise LookupError("not_found")
    if status is not None:
        if status not in ("active", "disabled"):
            raise ValueError("status must be active or disabled")
        db.execute(text("UPDATE oidc_pool_users SET status = :s WHERE id = :id"), {"s": status, "id": user_id})
        if status == "disabled":
            disable_everything(db, user_id=user_id, reason="user_disabled")
            audit(db, org_id=org_id, event_type="oidc.pool_user.disabled", actor_principal_id=actor, actor_kind="user",
                  target_type="oidc_pool_user", target_id=user_id, details={"pool_id": pool["id"]})
    if reset_mfa:
        db.execute(text("DELETE FROM oidc_pool_user_factors WHERE pool_user_id = :u"), {"u": user_id})
        disable_everything(db, user_id=user_id, reason="mfa_reset")
    if name is not None:
        db.execute(text("UPDATE oidc_pool_users SET name = :n WHERE id = :id"), {"n": name.strip()[:120] or None, "id": user_id})
    audit(db, org_id=org_id, event_type="oidc.pool_user.updated", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_pool_user", target_id=user_id,
          details={"pool_id": pool["id"], "status": status, "reset_mfa": reset_mfa})
    return public_view(load_user(db, pool_id=pool["id"], user_id=user_id), db=db)


def invite(db: Any, *, org_id: str, actor: str, pool: Mapping[str, Any], email: str, name: Optional[str],
           account_url: str) -> dict[str, Any]:
    """An admin adds a user (any signup mode): no password yet; they set one with the emailed code
    from 'Forgot password' on the hosted sign-in page."""
    email = normalise_email(email)
    if find_user(db, pool_id=pool["id"], email=email) is not None:
        raise ValueError("a user with this email exists in this pool")
    user = create_user(db, pool=pool, email=email, name=name, password_hash=None, status="active", verified=False)
    _mail(email, f"You are invited to sign in with {pool['name']}",
          f"An administrator added you to {pool['name']}.\n\nTo choose a password, open {account_url} and use "
          "'Forgot password'.\n")
    audit(db, org_id=org_id, event_type="oidc.pool_user.created", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_pool_user", target_id=user["id"], details={"pool_id": pool["id"], "via": "invite"})
    return public_view(user, db=db)


def _mail(to: str, subject: str, body: str) -> None:
    from ..identity import email_signin

    email_signin.DELIVER(to, subject, body)


def mail_available() -> bool:
    from ..identity import email_signin

    return email_signin.DELIVER is not email_signin._send_async or email_signin.available()  # noqa: SLF001


# ---- sessions ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class AppSession:
    id: str
    pool_id: str
    user: dict[str, Any]
    auth_methods: tuple[str, ...]
    mfa_at: Optional[datetime]
    created_at: datetime

    def __repr__(self) -> str:
        return f"AppSession(id={self.id!r}, pool_id={self.pool_id!r})"


def session_lookup(db: Any, *, pool_id: str, token: Optional[str]) -> Optional[AppSession]:
    """The live session for this cookie IN THIS POOL, its user active; else None."""
    if not token or not token.startswith(SESSION_PREFIX) or len(token) > 100:
        return None
    r = db.execute(text("SELECT id, pool_id, pool_user_id, auth_methods, mfa_at, created_at, idle_expires_at, "
                        "absolute_expires_at, revoked_at FROM oidc_sessions WHERE token_hash = :h"),
                   {"h": sha256_hex(token)}).mappings().first()
    t = now()
    if r is None or r["pool_id"] != pool_id or r["revoked_at"] is not None or t >= r["idle_expires_at"] \
            or t >= r["absolute_expires_at"]:
        return None
    user = load_user(db, pool_id=pool_id, user_id=r["pool_user_id"])
    if user is None or user["status"] != "active":
        return None
    db.execute(text("UPDATE oidc_sessions SET last_seen_at = now(), idle_expires_at = LEAST(now() + :idle, "
                    "absolute_expires_at) WHERE id = :id"), {"id": r["id"], "idle": SESSION_IDLE})
    return AppSession(r["id"], pool_id, user, tuple(r["auth_methods"] or ()), r["mfa_at"], r["created_at"])


def session_pool(db: Any, *, org_id: str, token: Optional[str]) -> Optional[str]:
    """The pool (of THIS org) whose live session this cookie is, if any: the account page reached
    without ?pool finds its pool from the session itself."""
    if not token or not token.startswith(SESSION_PREFIX) or len(token) > 100:
        return None
    r = db.execute(text("SELECT s.pool_id FROM oidc_sessions s JOIN oidc_user_pools p ON p.id = s.pool_id WHERE "
                        "s.token_hash = :h AND p.org_id = :o AND s.revoked_at IS NULL"),
                   {"h": sha256_hex(token), "o": org_id}).first()
    return r[0] if r else None


def valid_preauth(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(PREAUTH_PREFIX) and len(value) <= 100


def issue_session(db: Any, *, pool_id: str, user_id: str, methods: list[str], mfa: bool,
                  replaces: Optional[str], ip: Optional[str], user_agent: Optional[str]) -> tuple[str, str]:
    """A NEW session (rotation on every sign-in and MFA: no fixation). Returns (token, Set-Cookie)."""
    from ..identity.sessions import set_cookie_header

    if replaces and replaces.startswith(SESSION_PREFIX):
        db.execute(text("UPDATE oidc_sessions SET revoked_at = now() WHERE token_hash = :h AND revoked_at IS NULL"),
                   {"h": sha256_hex(replaces)})
    token = random_token(SESSION_PREFIX)
    t = now()
    db.execute(text("INSERT INTO oidc_sessions (id, token_hash, pool_id, pool_user_id, auth_methods, mfa_at, "
                    "idle_expires_at, absolute_expires_at, ip, user_agent) VALUES (:id, :h, :p, :u, :m, :mfa, :idle, "
                    ":abs, CAST(:ip AS inet), :ua)"),
               {"id": new_id("oss"), "h": sha256_hex(token), "p": pool_id, "u": user_id, "m": sorted(set(methods)),
                "mfa": t if mfa else None, "idle": t + SESSION_IDLE, "abs": t + SESSION_ABSOLUTE,
                "ip": _inet(ip), "ua": (user_agent or "")[:400] or None})
    db.execute(text("UPDATE oidc_pool_users SET last_sign_in_at = now() WHERE id = :u"), {"u": user_id})
    return token, set_cookie_header(APP_SESSION_COOKIE, token, max_age=int(SESSION_ABSOLUTE.total_seconds()))


def end_session(db: Any, token: Optional[str]) -> str:
    from ..identity.sessions import clear_cookie_header

    if token and token.startswith(SESSION_PREFIX):
        db.execute(text("UPDATE oidc_sessions SET revoked_at = now() WHERE token_hash = :h AND revoked_at IS NULL"),
                   {"h": sha256_hex(token)})
    return clear_cookie_header(APP_SESSION_COOKIE)


def _inet(ip: Optional[str]) -> Optional[str]:
    import ipaddress

    try:
        return str(ipaddress.ip_address((ip or "").split("%", 1)[0])) if ip else None
    except ValueError:
        return None


# ---- challenges (bound to the pre-auth cookie) -----------------------------------------------------

def new_challenge(db: Any, *, pool_id: str, user_id: Optional[str], purpose: str, payload: Mapping[str, Any],
                  preauth: Optional[str] = None) -> tuple[str, str]:
    """A challenge bound to this browser's pre-auth cookie (a new one unless given). Earlier
    challenges of the same binding and purpose end. Returns (preauth token, Set-Cookie)."""
    from ..identity.sessions import set_cookie_header

    if not preauth or not preauth.startswith(PREAUTH_PREFIX) or len(preauth) > 100:
        preauth = random_token(PREAUTH_PREFIX)
    binding = sha256_hex(preauth)
    db.execute(text("UPDATE oidc_app_challenges SET used_at = now() WHERE binding_hash = :b AND purpose = :pu "
                    "AND used_at IS NULL"), {"b": binding, "pu": purpose})
    db.execute(text("INSERT INTO oidc_app_challenges (id_hash, binding_hash, pool_id, pool_user_id, purpose, payload, "
                    "expires_at) VALUES (:id, :b, :p, :u, :pu, CAST(:pl AS JSONB), :e)"),
               {"id": sha256_hex(secrets.token_hex(16)), "b": binding, "p": pool_id, "u": user_id, "pu": purpose,
                "pl": json.dumps(dict(payload)), "e": now() + CHALLENGE_TTL})
    return preauth, set_cookie_header(APP_PREAUTH_COOKIE, preauth, max_age=int(CHALLENGE_TTL.total_seconds()))


def challenge(db: Any, *, pool_id: str, preauth: Optional[str], purpose: str) -> Optional[dict[str, Any]]:
    """The live challenge of this purpose bound to this browser IN THIS POOL (locked)."""
    if not preauth or not preauth.startswith(PREAUTH_PREFIX) or len(preauth) > 100:
        return None
    r = db.execute(text("SELECT * FROM oidc_app_challenges WHERE binding_hash = :b AND purpose = :pu AND used_at IS NULL "
                        "AND expires_at > now() ORDER BY created_at DESC LIMIT 1 FOR UPDATE"),
                   {"b": sha256_hex(preauth), "pu": purpose}).mappings().first()
    if r is None or r["pool_id"] != pool_id:
        return None
    return dict(r)


def spend(db: Any, ch: Mapping[str, Any]) -> None:
    db.execute(text("UPDATE oidc_app_challenges SET used_at = now() WHERE id_hash = :h"), {"h": ch["id_hash"]})


def strike(db: Any, ch: Mapping[str, Any]) -> bool:
    """One failed attempt; True when the challenge is now spent."""
    n = db.execute(text("UPDATE oidc_app_challenges SET attempts = attempts + 1 WHERE id_hash = :h RETURNING attempts"),
                   {"h": ch["id_hash"]}).scalar()
    if n >= CHALLENGE_ATTEMPTS:
        spend(db, ch)
        return True
    return False


# ---- rate limits ---------------------------------------------------------------------------------

def _hit(db: Any, bucket: str, ident: str, limit: tuple[int, int]) -> bool:
    from ..identity import ratelimit

    return ratelimit.hit(bucket, ident, limit=limit[0], window_seconds=limit[1], db=db).allowed


def _count(db: Any, bucket: str, ident: str, window: int) -> int:
    from ..identity import ratelimit

    return ratelimit.peek(bucket, ident, window_seconds=window, db=db)


# ---- passwords ------------------------------------------------------------------------------------

def check_password(pool: Mapping[str, Any], password: str, *, email: str, db: Any = None) -> str:
    """A pool user's new password, checked like every other password BOSS sets: the pool's own
    length rule AND the org's identity policy (its optional breach check, IDENTITY.md §4.2)."""
    from ..identity import passwords
    from ..identity.policy import get_policy

    rules = pool.get("password_policy") or {}
    org_policy = get_policy(str(pool["org_id"]), db=db) if db is not None and pool.get("org_id") else None
    try:
        return passwords.check_new_password(password, email=email, min_length=int(rules.get("min_length") or 12),
                                            policy=org_policy)
    except passwords.PasswordRejected as exc:
        raise RealmError(str(exc) or "password_rejected") from None


def password_signin(db: Any, *, org_id: str, pool: Mapping[str, Any], email: str, password: str,
                    ip: Optional[str]) -> dict[str, Any]:
    """The user, when the password is right and nothing locks them out. One answer for unknown
    email, wrong password, no password and a disabled user, and the same hashing work for each
    (anti-enumeration, §4.10). Raises RealmError('bad_credentials' | 'locked' | 'unverified')."""
    from ..identity import passwords

    ident = f"{pool['id']}\x1f{str(email or '').strip().lower()}"
    if _count(db, "oidc.signin.account", ident, ACCOUNT_FAILURES[1]) >= ACCOUNT_FAILURES[0] or \
            _count(db, "oidc.signin.ip", ip or "-", IP_FAILURES[1]) >= IP_FAILURES[0]:
        passwords.verify_password(password or "", None)
        raise RealmError("locked")
    try:
        email = normalise_email(email)
        user = find_user(db, pool_id=pool["id"], email=email)
    except RealmError:
        user = None
    ok = passwords.verify_password(password or "", user["password_hash"] if user else None)
    if not ok or user is None or user["status"] == "disabled":
        _hit(db, "oidc.signin.account", ident, ACCOUNT_FAILURES)
        _hit(db, "oidc.signin.ip", ip or "-", IP_FAILURES)
        audit(db, org_id=org_id, event_type="oidc.pool_user.login_failed", outcome="failure", actor_kind="pool_user",
              target_type="oidc_pool", target_id=pool["id"], ip=ip, details={"pool_id": pool["id"]})
        if _count(db, "oidc.signin.account", ident, ACCOUNT_FAILURES[1]) == ACCOUNT_FAILURES[0]:
            audit(db, org_id=org_id, event_type="oidc.pool_user.locked", outcome="denied", actor_kind="pool_user",
                  target_type="oidc_pool", target_id=pool["id"], ip=ip, details={"pool_id": pool["id"]})
        raise RealmError("bad_credentials")
    if user["status"] == "pending":
        raise RealmError("unverified")
    if passwords.needs_rehash(user["password_hash"]):
        db.execute(text("UPDATE oidc_pool_users SET password_hash = :h WHERE id = :id"),
                   {"h": passwords.hash_password(password), "id": user["id"]})
    return user


def set_password(db: Any, *, pool: Mapping[str, Any], user: Mapping[str, Any], password: str) -> None:
    from ..identity import passwords

    value = check_password(pool, password, email=user["email"], db=db)
    db.execute(text("UPDATE oidc_pool_users SET password_hash = :h WHERE id = :id"),
               {"h": passwords.hash_password(value), "id": user["id"]})


# ---- emailed codes --------------------------------------------------------------------------------

def send_code(db: Any, *, pool: Mapping[str, Any], user: Optional[Mapping[str, Any]], email: str, purpose: str,
              preauth: Optional[str], subject: str) -> tuple[str, str]:
    """A 6-digit code by email, hashed in a challenge bound to this browser. With no user (an
    unknown address) the same challenge is made and no mail is sent: the page cannot tell."""
    from ..identity import passwords

    code = f"{secrets.randbelow(10 ** 6):06d}"
    sendable = user is not None and _hit(db, "oidc.email_code.account", f"{pool['id']}\x1f{email.lower()}",
                                         CODES_PER_ACCOUNT)
    preauth, cookie = new_challenge(db, pool_id=pool["id"], user_id=user["id"] if user else None, purpose=purpose,
                                    payload={"code": passwords.hash_code(code)}, preauth=preauth)
    if sendable:
        _mail(email, subject, f"Your code is {code}. It expires in 10 minutes. If you did not ask for it, ignore this.")
    return preauth, cookie


def check_code(db: Any, *, pool: Mapping[str, Any], preauth: Optional[str], purpose: str, code: str
               ) -> Optional[dict[str, Any]]:
    """The user the code was sent to, if it is right (the challenge is then spent)."""
    from ..identity import passwords

    ch = challenge(db, pool_id=pool["id"], preauth=preauth, purpose=purpose)
    if ch is None:
        return None
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code) or ch["pool_user_id"] is None or \
            not passwords.verify_code(code, ch["payload"]["code"]):
        strike(db, ch)
        return None
    spend(db, ch)
    return load_user(db, pool_id=pool["id"], user_id=ch["pool_user_id"])


# ---- TOTP -----------------------------------------------------------------------------------------

def has_factor(db: Any, user_id: str, kind: Optional[str] = None) -> bool:
    sql = "SELECT 1 FROM oidc_pool_user_factors WHERE pool_user_id = :u" + (" AND kind = :k" if kind else "")
    return db.execute(text(sql + " LIMIT 1"), {"u": user_id, "k": kind}).first() is not None


def totp_begin(db: Any, *, org_id: str, pool: Mapping[str, Any], user: Mapping[str, Any], preauth: Optional[str]
               ) -> tuple[str, str, str, str]:
    """A pending enrolment: (secret, otpauth uri, preauth, Set-Cookie). The secret is in the vault."""
    from ..identity import totp, vault

    secret = totp.generate_secret()
    item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.TOTP_SECRET,
                       provider=TOTP_VAULT_PROVIDER, secret=secret, db=db, connection_name=user["id"])
    preauth, cookie = new_challenge(db, pool_id=pool["id"], user_id=user["id"], purpose="enrol_totp",
                                    payload={"item": item}, preauth=preauth)
    uri = totp.otpauth_uri(secret, f"{pool['name']}:{user['email']}")
    return secret, uri, preauth, cookie


def totp_confirm(db: Any, *, org_id: str, pool: Mapping[str, Any], preauth: Optional[str], code: str,
                 user_id: str) -> bool:
    from ..identity import totp, vault

    ch = challenge(db, pool_id=pool["id"], preauth=preauth, purpose="enrol_totp")
    if ch is None or ch["pool_user_id"] != user_id:
        return False
    secret = vault.read_secret(ch["payload"]["item"], db=db, kind=vault.VaultItemKind.TOTP_SECRET)
    step = totp.match_step(secret, code, last_step=-1)
    if step is None:
        strike(db, ch)
        return False
    spend(db, ch)
    db.execute(text("DELETE FROM oidc_pool_user_factors WHERE pool_user_id = :u AND kind = 'totp'"), {"u": user_id})
    db.execute(text("INSERT INTO oidc_pool_user_factors (id, pool_user_id, kind, secret_vault_item_id, last_used_step, "
                    "label) VALUES (:id, :u, 'totp', :item, :step, 'Authenticator app')"),
               {"id": new_id("opf"), "u": user_id, "item": ch["payload"]["item"], "step": step})
    audit(db, org_id=org_id, event_type="oidc.pool_user.mfa_enrolled", actor_kind="pool_user",
          target_type="oidc_pool_user", target_id=user_id, details={"pool_id": pool["id"], "kind": "totp"})
    return True


def totp_verify(db: Any, *, org_id: str, pool: Mapping[str, Any], user_id: str, code: str, ip: Optional[str]) -> bool:
    """A sign-in code: constant-time, ±1 step, strictly after the last used step (no replay),
    rate-limited per user."""
    from ..identity import totp, vault

    if _count(db, "oidc.mfa.user", user_id, MFA_FAILURES[1]) >= MFA_FAILURES[0]:
        return False
    row = db.execute(text("SELECT id, secret_vault_item_id, last_used_step FROM oidc_pool_user_factors WHERE "
                          "pool_user_id = :u AND kind = 'totp' FOR UPDATE"), {"u": user_id}).mappings().first()
    step = None
    if row is not None:
        secret = vault.read_secret(row["secret_vault_item_id"], db=db, kind=vault.VaultItemKind.TOTP_SECRET)
        step = totp.match_step(secret, code, last_step=int(row["last_used_step"] or -1))
    if step is None:
        _hit(db, "oidc.mfa.user", user_id, MFA_FAILURES)
        audit(db, org_id=org_id, event_type="oidc.pool_user.mfa_failed", outcome="failure", actor_kind="pool_user",
              target_type="oidc_pool_user", target_id=user_id, ip=ip, details={"pool_id": pool["id"]})
        return False
    db.execute(text("UPDATE oidc_pool_user_factors SET last_used_step = :s, last_used_at = now() WHERE id = :id"),
               {"s": step, "id": row["id"]})
    return True


# ---- passkeys -------------------------------------------------------------------------------------

def user_handle(user_id: str) -> bytes:
    return user_id.encode("ascii")


def passkey_options(db: Any, *, pool: Mapping[str, Any], origin: str, purpose: str, user: Optional[Mapping[str, Any]],
                    preauth: Optional[str]) -> tuple[dict[str, Any], str, str]:
    """WebAuthn options for `register` (a signed-in user), `mfa` (the user a password named) or
    `login` (discoverable). The RP ID is the BOSS host (identity's rp_for). Returns (options,
    preauth, Set-Cookie)."""
    from webauthn import generate_authentication_options, generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AttestationConveyancePreference, AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
        ResidentKeyRequirement, UserVerificationRequirement)

    from ..identity.webauthn import PasskeyError, rp_for

    if not pool.get("passkeys"):
        raise RealmError("passkeys_off")
    try:
        rp_id, _ = rp_for(origin)
    except PasskeyError:
        raise RealmError("passkeys_unavailable") from None
    challenge_bytes = secrets.token_bytes(32)
    mine = []
    if user is not None:
        mine = [PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in db.execute(text(
            "SELECT credential_id FROM oidc_pool_user_factors WHERE pool_user_id = :u AND kind = 'webauthn'"),
            {"u": user["id"]})]
    if purpose == "register":
        if user is None or len(mine) >= MAX_PASSKEYS:
            raise RealmError("passkey_limit")
        options = generate_registration_options(
            rp_id=rp_id, rp_name=pool["name"], user_name=user["email"], user_id=user_handle(user["id"]),
            user_display_name=user.get("name") or user["email"], challenge=challenge_bytes,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=mine)
    elif purpose in ("login", "mfa"):
        if purpose == "mfa" and user is None:
            raise RealmError("challenge_invalid")
        options = generate_authentication_options(rp_id=rp_id, challenge=challenge_bytes,
                                                  allow_credentials=mine if purpose == "mfa" else None,
                                                  user_verification=UserVerificationRequirement.REQUIRED)
    else:
        raise RealmError("challenge_invalid")
    preauth, cookie = new_challenge(db, pool_id=pool["id"], user_id=user["id"] if user else None, purpose="passkey",
                                    payload={"purpose": purpose, "challenge": b64u(challenge_bytes)}, preauth=preauth)
    return json.loads(options_to_json(options)), preauth, cookie


def _b64u_decode(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def passkey_verify(db: Any, *, org_id: str, pool: Mapping[str, Any], origin: str, purpose: str,
                   preauth: Optional[str], credential: Any) -> dict[str, Any]:
    """Verify a ceremony; returns the user. A credential of another pool, or one whose user
    handle names another user, is refused (the unbound-challenge flaw, A6)."""
    from webauthn import verify_authentication_response, verify_registration_response
    from webauthn.helpers import parse_authentication_credential_json

    from ..identity.webauthn import PasskeyError, rp_for

    try:
        rp_id, expected_origin = rp_for(origin)
    except PasskeyError:
        raise RealmError("passkeys_unavailable") from None
    ch = challenge(db, pool_id=pool["id"], preauth=preauth, purpose="passkey")
    if ch is None or ch["payload"].get("purpose") != purpose:
        raise RealmError("challenge_invalid")
    spend(db, ch)
    expected = _b64u_decode(ch["payload"]["challenge"])
    if purpose == "register":
        user = load_user(db, pool_id=pool["id"], user_id=ch["pool_user_id"])
        if user is None:
            raise RealmError("challenge_invalid")
        try:
            v = verify_registration_response(credential=credential, expected_challenge=expected, expected_rp_id=rp_id,
                                             expected_origin=expected_origin, require_user_verification=True)
        except Exception:  # noqa: BLE001
            raise RealmError("passkey_failed") from None
        db.execute(text("INSERT INTO oidc_pool_user_factors (id, pool_user_id, kind, credential_id, public_key, sign_count, "
                        "label) VALUES (:id, :u, 'webauthn', :c, :pk, :sc, 'Passkey')"),
                   {"id": new_id("opf"), "u": user["id"], "c": v.credential_id, "pk": v.credential_public_key,
                    "sc": int(v.sign_count)})
        audit(db, org_id=org_id, event_type="oidc.pool_user.passkey_added", actor_kind="pool_user",
              target_type="oidc_pool_user", target_id=user["id"], details={"pool_id": pool["id"]})
        return user
    try:
        parsed = parse_authentication_credential_json(credential if isinstance(credential, str) else json.dumps(credential))
    except Exception:  # noqa: BLE001
        raise RealmError("passkey_failed") from None
    row = db.execute(text("SELECT f.id, f.pool_user_id, f.public_key, f.sign_count FROM oidc_pool_user_factors f JOIN "
                          "oidc_pool_users u ON u.id = f.pool_user_id WHERE f.credential_id = :c AND f.kind = 'webauthn' "
                          "AND u.pool_id = :p"), {"c": bytes(parsed.raw_id), "p": pool["id"]}).mappings().first()
    if row is None:
        raise RealmError("passkey_failed")
    if ch["pool_user_id"] and ch["pool_user_id"] != row["pool_user_id"]:
        raise RealmError("passkey_failed")
    handle = parsed.response.user_handle
    if (purpose == "login" and not handle) or (handle and bytes(handle) != user_handle(row["pool_user_id"])):
        raise RealmError("passkey_failed")
    try:
        v = verify_authentication_response(credential=parsed, expected_challenge=expected, expected_rp_id=rp_id,
                                           expected_origin=expected_origin, credential_public_key=bytes(row["public_key"]),
                                           credential_current_sign_count=0, require_user_verification=True)
    except Exception:  # noqa: BLE001
        raise RealmError("passkey_failed") from None
    stored, new = int(row["sign_count"]), int(v.new_sign_count)
    if (stored > 0 or new > 0) and new <= stored and not v.credential_backed_up:
        raise RealmError("passkey_failed")       # a device-bound key whose counter went back: a clone
    db.execute(text("UPDATE oidc_pool_user_factors SET sign_count = GREATEST(sign_count, :n), last_used_at = now() "
                    "WHERE id = :id"), {"n": new, "id": row["id"]})
    user = load_user(db, pool_id=pool["id"], user_id=row["pool_user_id"])
    if user is None or user["status"] != "active":
        raise RealmError("passkey_failed")
    return user


def factors(db: Any, user_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(text("SELECT id, kind, label, created_at, last_used_at FROM oidc_pool_user_factors "
                                             "WHERE pool_user_id = :u AND kind <> 'recovery_code' ORDER BY created_at"),
                                        {"u": user_id}).mappings()]


def remove_factor(db: Any, *, org_id: str, pool: Mapping[str, Any], user_id: str, factor_id: str) -> bool:
    if pool.get("mfa") == "required":
        remaining = db.execute(text("SELECT count(*) FROM oidc_pool_user_factors WHERE pool_user_id = :u AND id <> :f "
                                    "AND kind <> 'recovery_code'"), {"u": user_id, "f": factor_id}).scalar()
        if not remaining:
            raise RealmError("last_factor")
    gone = db.execute(text("DELETE FROM oidc_pool_user_factors WHERE id = :f AND pool_user_id = :u"),
                      {"f": factor_id, "u": user_id}).rowcount
    if gone:
        audit(db, org_id=org_id, event_type="oidc.pool_user.factor_removed", actor_kind="pool_user",
              target_type="oidc_pool_user", target_id=user_id, details={"pool_id": pool["id"]})
    return bool(gone)


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
