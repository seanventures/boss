"""TOTP second factor (RFC 6238; IDENTITY.md §4.4).

SHA-1, 6 digits, 30 s steps, a window of ±1 step. The secret is 20 random bytes held ONLY in the
vault (AES-256-GCM, AAD-bound to its row; S3's vault.store / get_credential) — never in plaintext
anywhere, including while an enrolment is pending (the pending secret sits in a challenge payload
encrypted under a key derived from the browser's session token). The last accepted time-step is
stored and any code at or below it is refused (replay, A5).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Any, Optional
from urllib.parse import quote

from sqlalchemy import text

from . import vault
from .authn_support import consume_challenge, create_challenge, latest_bound_challenge
from .principal import AuthContext, AuthMethod, Principal

DIGITS = 6
STEP_SECONDS = 30
WINDOW = 1
MAX_ATTEMPTS_PER_CHALLENGE = 5
PER_USER_PER_HOUR = 20
ENROL_TTL_SECONDS = 600
ISSUER = "BOSS"


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _key(secret_b32: str) -> bytes:
    return base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))


def code_at(secret_b32: str, step: int) -> str:
    mac = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(value).zfill(DIGITS)


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def match_step(secret_b32: str, code: str, *, last_step: int, now: Optional[float] = None
               ) -> Optional[int]:
    """The time-step `code` belongs to (within ±1), strictly after `last_step`; else None. Every
    candidate is compared in constant time."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    base = current_step(now)
    found: Optional[int] = None
    for step in range(base - WINDOW, base + WINDOW + 1):
        if hmac.compare_digest(code_at(secret_b32, step), code) and step > last_step and found is None:
            found = step
    return found


def otpauth_uri(secret_b32: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}")


def is_enrolled(db: Any, principal_id: str) -> bool:
    return db.execute(text("SELECT 1 FROM identity_totp WHERE principal_id = :p"),
                      {"p": principal_id}).first() is not None


# ---- enrolment ---------------------------------------------------------------------------------------

def begin_enrolment(db: Any, *, principal_id: str, account: str, session_token: str) -> tuple[str, str]:
    """A new pending secret, bound to this signed-in browser. Returns (secret, otpauth URI) — shown
    ONCE; the caller never stores either."""
    secret = generate_secret()
    db.execute(text("UPDATE identity_auth_challenges SET consumed_at = now() WHERE kind = 'mfa' "
                    "AND principal_id = :p AND consumed_at IS NULL AND binding_hash IS NOT NULL "
                    "AND payload_enc IS NOT NULL"), {"p": principal_id})
    create_challenge(db, kind="mfa", binding_secret=session_token, ttl_seconds=ENROL_TTL_SECONDS,
                     principal_id=principal_id, payload={"enrol_totp": secret})
    return secret, otpauth_uri(secret, account)


def has_pending_enrolment(db: Any, *, principal: Principal, session_token: str) -> bool:
    """Is there a live enrolment, begun by THIS browser's session, for this person? Without one a
    code cannot be checked at all (the key expired after ENROL_TTL_SECONDS, a newer "begin"
    replaced it, or the session changed) — which is not a wrong code and must never be answered
    as one: the person would retype a correct code forever, and nothing would count or say why."""
    pending = latest_bound_challenge(db, kind="mfa", binding_secret=session_token, lock=False)
    return (pending is not None and pending.principal_id == principal.id
            and "enrol_totp" in pending.payload)


def confirm_enrolment(db: Any, *, principal: Principal, session_token: str, code: str) -> bool:
    """Store the pending secret in the vault once a valid code proves the authenticator has it."""
    pending = latest_bound_challenge(db, kind="mfa", binding_secret=session_token)
    if pending is None or pending.principal_id != principal.id or "enrol_totp" not in pending.payload:
        return False
    secret = pending.payload["enrol_totp"]
    step = match_step(secret, code, last_step=0)
    if step is None:
        from .authn_support import count_attempt

        count_attempt(db, pending.id_hash, max_attempts=MAX_ATTEMPTS_PER_CHALLENGE)
        return False
    consume_challenge(db, pending.id_hash)
    item_id = vault.store(org_id=principal.org_id, owner_principal_id=principal.id,
                          kind=vault.VaultItemKind.TOTP_SECRET, provider="totp", secret=secret, db=db)
    db.execute(text("INSERT INTO identity_totp (principal_id, vault_item_id, last_step) "
                    "VALUES (:p, :v, :s) ON CONFLICT (principal_id) DO UPDATE SET "
                    "vault_item_id = EXCLUDED.vault_item_id, last_step = EXCLUDED.last_step, "
                    "enrolled_at = now()"), {"p": principal.id, "v": item_id, "s": step})
    return True


def remove(db: Any, principal_id: str) -> bool:
    row = db.execute(text("DELETE FROM identity_totp WHERE principal_id = :p RETURNING vault_item_id"),
                     {"p": principal_id}).first()
    return row is not None


# ---- verification ------------------------------------------------------------------------------------

def verify(db: Any, principal: Principal, code: str) -> bool:
    """Verify a sign-in/step-up code and advance the replay guard atomically: two requests with
    the same code cannot both succeed (the UPDATE only lands for a step above the stored one)."""
    row = db.execute(text("SELECT vault_item_id, last_step FROM identity_totp WHERE principal_id = :p "
                          "FOR UPDATE"), {"p": principal.id}).first()
    if row is None:
        return False
    cred = vault.get_credential(
        vault.ConnectionRef("totp", principal.id, row.vault_item_id), vault.CredentialPurpose.TOTP_VERIFY,
        ctx=AuthContext(principal, AuthMethod.SESSION), db=db)
    step = match_step(cred.token, code, last_step=int(row.last_step))
    if step is None:
        return False
    advanced = db.execute(text("UPDATE identity_totp SET last_step = :s WHERE principal_id = :p "
                               "AND last_step < :s RETURNING principal_id"),
                          {"s": step, "p": principal.id}).first()
    return advanced is not None
