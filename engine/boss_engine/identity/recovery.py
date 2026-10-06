"""Recovery codes and password reset tokens (IDENTITY.md §4.7).

Recovery codes: 10 per person, 12 base32 characters each (shown as XXXX-XXXX-XXXX), argon2id
(small profile) at rest, single use. Using one notifies the person; below 3 left, regeneration is
forced. Reset tokens: 32 random bytes, SHA-256 at rest, 30 minutes, single use, and void as soon
as the password changes by any route (A12).
"""
from __future__ import annotations

import secrets
from typing import Any, Optional

from sqlalchemy import text

from . import ids
from .authn_support import create_challenge, random_token
from .passwords import hash_code, verify_code

CODE_COUNT = 10
CODE_LENGTH = 12
FORCE_REGENERATE_BELOW = 3
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
RESET_TTL_SECONDS = 30 * 60


def _normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch in _ALPHABET)


def _display(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def generate(db: Any, principal_id: str) -> list[str]:
    """Replace every code with 10 new ones. Returned ONCE (for display); only hashes are kept."""
    codes = ["".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH)) for _ in range(CODE_COUNT)]
    db.execute(text("DELETE FROM identity_recovery_codes WHERE principal_id = :p"), {"p": principal_id})
    for code in codes:
        db.execute(text("INSERT INTO identity_recovery_codes (id, principal_id, code_hash) "
                        "VALUES (:id, :p, :h)"),
                   {"id": "rc_" + ids.new_id("key")[4:], "p": principal_id, "h": hash_code(code)})
    return [_display(c) for c in codes]


def remaining(db: Any, principal_id: str) -> int:
    return int(db.execute(text("SELECT count(*) FROM identity_recovery_codes WHERE principal_id = :p "
                               "AND used_at IS NULL"), {"p": principal_id}).scalar() or 0)


def use(db: Any, principal_id: str, code: str) -> bool:
    """Spend one code. Every unused hash is checked (no early exit on a hit before the others,
    and no lookup by a code prefix); the matched row is consumed atomically."""
    value = _normalize(code)
    if len(value) != CODE_LENGTH:
        return False
    rows = db.execute(text("SELECT id, code_hash FROM identity_recovery_codes WHERE principal_id = :p "
                           "AND used_at IS NULL FOR UPDATE"), {"p": principal_id}).fetchall()
    matched: Optional[str] = None
    for row in rows:
        if verify_code(value, row.code_hash) and matched is None:
            matched = row.id
    if matched is None:
        return False
    spent = db.execute(text("UPDATE identity_recovery_codes SET used_at = now() WHERE id = :id "
                            "AND used_at IS NULL RETURNING id"), {"id": matched}).first()
    return spent is not None


def must_regenerate(db: Any, principal_id: str) -> bool:
    total = int(db.execute(text("SELECT count(*) FROM identity_recovery_codes WHERE principal_id = :p"),
                           {"p": principal_id}).scalar() or 0)
    return total > 0 and remaining(db, principal_id) < FORCE_REGENERATE_BELOW


# ---- password reset tokens -----------------------------------------------------------------------

def issue_reset_token(db: Any, principal_id: str) -> str:
    """A new reset token (the raw token is returned ONCE, to be emailed or handed to an admin).
    Earlier unspent tokens for the person are voided."""
    void_reset_tokens(db, principal_id)
    token = random_token()
    create_challenge(db, kind="password_reset", binding_secret=None, ttl_seconds=RESET_TTL_SECONDS,
                     principal_id=principal_id, secret=token)
    return token


def void_reset_tokens(db: Any, principal_id: str) -> None:
    db.execute(text("UPDATE identity_auth_challenges SET consumed_at = now() WHERE kind = 'password_reset' "
                    "AND principal_id = :p AND consumed_at IS NULL"), {"p": principal_id})
