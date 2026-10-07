"""Passkeys (WebAuthn; IDENTITY.md §4.5), on py_webauthn.

  - RP ID = host of the BOUND public origin (never the request's Host, F3/H2); the origin must
    match exactly. Attestation `none`; an optional AAGUID allowlist (BOSS_WEBAUTHN_AAGUIDS).
    AAGUID LIMIT (F3/L3): with attestation `none` the AAGUID is the authenticator's own unsigned
    claim, so the allowlist steers honest authenticators (and keeps out accidental ones); it is
    NOT a security boundary against a hostile client, which can claim any AAGUID. Enforcing a
    hardware model needs attestation verified against the FIDO MDS, which BOSS does not do.
  - userVerification REQUIRED and discoverable credentials: a passkey is a complete sign-in on
    its own, and also satisfies MFA after a password.
  - Challenges: 32 random bytes, stored hashed, 5 minutes, single use, bound to the browser that
    asked (pre-auth cookie, or the session token for registration and step-up).
  - Authentication: the credential's user handle must map to the principal the challenge resolves
    to (A6). A sign-count regression (F3/L3) BLOCKS a device-bound authenticator (not backup
    eligible: a counter going backwards there means a cloned key) and is flagged on the
    credential (counter_regressed_at) and in the audit log; a synced passkey (backup eligible,
    which may report 0 or a per-device counter) is flagged but not blocked.
  - Up to 10 passkeys per person.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import text

from . import ids
from .authn_support import (
    b64url,
    b64url_decode,
    consume_challenge,
    create_challenge,
    latest_bound_challenge,
)

CHALLENGE_TTL_SECONDS = 300
MAX_PASSKEYS = 10
PURPOSES = frozenset({"login", "mfa", "step_up", "register"})


class PasskeyError(Exception):
    """`code`: unavailable | challenge_invalid | verification_failed | unknown_credential |
    user_mismatch | limit_reached | aaguid_not_allowed | counter_regressed (`regressed` names the
    blocked credential: its principal and id, so the caller can record it)."""

    def __init__(self, code: str, regressed: Optional["VerifiedPasskey"] = None):
        super().__init__(code)
        self.code = code
        self.regressed = regressed


@dataclass(frozen=True)
class VerifiedPasskey:
    principal_id: str
    passkey_id: str
    counter_regressed: bool


def _multi_device(verified: Any) -> bool:
    """py_webauthn's credential_device_type is an enum whose str() is its NAME
    ("CredentialDeviceType.MULTI_DEVICE"): compare its value, case-insensitively."""
    kind = getattr(verified, "credential_device_type", "")
    return str(getattr(kind, "value", kind)).lower().endswith("multi_device")


def rp_for(origin: Optional[str]) -> tuple[str, str]:
    if not origin:
        raise PasskeyError("unavailable")
    host = urlsplit(origin).hostname
    if not host:
        raise PasskeyError("unavailable")
    return host, origin


def user_handle_for(principal_id: str) -> bytes:
    """The WebAuthn user.id: the principal id's bytes (random, not PII)."""
    return principal_id.encode("ascii")


def _aaguid_allowlist() -> frozenset[str]:
    from ..config import get_settings

    raw = get_settings().webauthn_aaguid_allowlist or ""
    return frozenset(a.strip().lower() for a in raw.split(",") if a.strip())


def count(db: Any, principal_id: str) -> int:
    return int(db.execute(text("SELECT count(*) FROM identity_webauthn_credentials WHERE principal_id = :p"),
                          {"p": principal_id}).scalar() or 0)


def list_passkeys(db: Any, principal_id: str) -> list[dict[str, Any]]:
    rows = db.execute(text("SELECT id, name, created_at, last_used_at, backup_eligible, backup_state "
                           "FROM identity_webauthn_credentials WHERE principal_id = :p ORDER BY created_at"),
                      {"p": principal_id})
    return [{"id": r.id, "name": r.name, "created_at": r.created_at, "last_used_at": r.last_used_at,
             "synced": bool(r.backup_state)} for r in rows]


# ---- registration --------------------------------------------------------------------------------

def registration_options(db: Any, *, principal_id: str, email: str, display_name: str,
                         origin: Optional[str], binding_secret: str) -> dict[str, Any]:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AttestationConveyancePreference,
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    rp_id, _ = rp_for(origin)
    if count(db, principal_id) >= MAX_PASSKEYS:
        raise PasskeyError("limit_reached")
    existing = [PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in db.execute(
        text("SELECT credential_id FROM identity_webauthn_credentials WHERE principal_id = :p"),
        {"p": principal_id})]
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=rp_id, rp_name="BOSS", user_name=email, user_id=user_handle_for(principal_id),
        user_display_name=display_name, challenge=challenge,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=existing)
    create_challenge(db, kind="webauthn", binding_secret=binding_secret,
                     ttl_seconds=CHALLENGE_TTL_SECONDS, principal_id=principal_id,
                     payload={"purpose": "register", "challenge": b64url(challenge)})
    return json.loads(options_to_json(options))


def verify_registration(db: Any, *, principal_id: str, origin: Optional[str], binding_secret: str,
                        credential: Any, name: str) -> str:
    from webauthn import verify_registration_response

    rp_id, expected_origin = rp_for(origin)
    pending = latest_bound_challenge(db, kind="webauthn", binding_secret=binding_secret)
    if (pending is None or pending.principal_id != principal_id
            or pending.payload.get("purpose") != "register"):
        raise PasskeyError("challenge_invalid")
    consume_challenge(db, pending.id_hash)   # single use, success or not
    if count(db, principal_id) >= MAX_PASSKEYS:
        raise PasskeyError("limit_reached")
    try:
        verified = verify_registration_response(
            credential=credential, expected_challenge=b64url_decode(pending.payload["challenge"]),
            expected_rp_id=rp_id, expected_origin=expected_origin, require_user_verification=True)
    except Exception as exc:  # noqa: BLE001 — py_webauthn raises several types; all are a refusal
        raise PasskeyError("verification_failed") from exc
    aaguid = str(verified.aaguid or "").lower() or None
    allow = _aaguid_allowlist()
    if allow and (aaguid or "") not in allow:
        raise PasskeyError("aaguid_not_allowed")
    transports = None
    if isinstance(credential, dict):
        transports = (credential.get("response") or {}).get("transports")
    pid = ids.new_id("pky")
    db.execute(text(
        "INSERT INTO identity_webauthn_credentials (id, principal_id, credential_id, public_key, "
        "sign_count, aaguid, transports, backup_eligible, backup_state, name) VALUES (:id, :p, :cid, "
        ":pk, :sc, CAST(:aaguid AS uuid), :tr, :be, :bs, :n)"),
        {"id": pid, "p": principal_id, "cid": verified.credential_id,
         "pk": verified.credential_public_key, "sc": int(verified.sign_count),
         "aaguid": aaguid if aaguid and aaguid != "00000000-0000-0000-0000-000000000000" else None,
         "tr": list(transports) if transports else None,
         "be": _multi_device(verified),
         "bs": bool(getattr(verified, "credential_backed_up", False)),
         "n": (name or "Passkey").strip()[:80] or "Passkey"})
    return pid


# ---- authentication ------------------------------------------------------------------------------

def authentication_options(db: Any, *, purpose: str, origin: Optional[str], binding_secret: str,
                           principal_id: Optional[str] = None) -> dict[str, Any]:
    """`login` (discoverable: no allowCredentials, no principal), `mfa` and `step_up` (bound to the
    principal the pre-auth challenge or session names)."""
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

    if purpose not in ("login", "mfa", "step_up"):
        raise PasskeyError("challenge_invalid")
    if purpose != "login" and not principal_id:
        raise PasskeyError("challenge_invalid")
    rp_id, _ = rp_for(origin)
    allow = None
    if principal_id:
        allow = [PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in db.execute(
            text("SELECT credential_id FROM identity_webauthn_credentials WHERE principal_id = :p"),
            {"p": principal_id})]
    challenge = secrets.token_bytes(32)
    options = generate_authentication_options(rp_id=rp_id, challenge=challenge, allow_credentials=allow,
                                              user_verification=UserVerificationRequirement.REQUIRED)
    create_challenge(db, kind="webauthn", binding_secret=binding_secret,
                     ttl_seconds=CHALLENGE_TTL_SECONDS, principal_id=principal_id,
                     payload={"purpose": purpose, "challenge": b64url(challenge)})
    return json.loads(options_to_json(options))


def verify_authentication(db: Any, *, purpose: str, origin: Optional[str], binding_secret: str,
                          credential: Any) -> VerifiedPasskey:
    from webauthn import verify_authentication_response
    from webauthn.helpers import parse_authentication_credential_json

    rp_id, expected_origin = rp_for(origin)
    pending = latest_bound_challenge(db, kind="webauthn", binding_secret=binding_secret)
    if pending is None or pending.payload.get("purpose") != purpose:
        raise PasskeyError("challenge_invalid")
    consume_challenge(db, pending.id_hash)   # single use
    try:
        parsed = parse_authentication_credential_json(
            credential if isinstance(credential, str) else json.dumps(credential))
    except Exception as exc:  # noqa: BLE001
        raise PasskeyError("verification_failed") from exc
    row = db.execute(text("SELECT id, principal_id, public_key, sign_count, backup_eligible FROM "
                          "identity_webauthn_credentials WHERE credential_id = :c"),
                     {"c": bytes(parsed.raw_id)}).first()
    if row is None:
        raise PasskeyError("unknown_credential")
    # The credential must belong to the principal the challenge names (mfa/step_up), and its user
    # handle — when the authenticator returns one, always for discoverable credentials — must map
    # to that same principal (Kaappu's unbound-challenge flaw, A6).
    if pending.principal_id and pending.principal_id != row.principal_id:
        raise PasskeyError("user_mismatch")
    handle = parsed.response.user_handle
    if purpose == "login" and not handle:
        raise PasskeyError("user_mismatch")
    if handle and bytes(handle) != user_handle_for(row.principal_id):
        raise PasskeyError("user_mismatch")
    try:
        verified = verify_authentication_response(
            credential=parsed, expected_challenge=b64url_decode(pending.payload["challenge"]),
            expected_rp_id=rp_id, expected_origin=expected_origin,
            credential_public_key=bytes(row.public_key),
            credential_current_sign_count=0,   # regression is OUR check below: it never blocks
            require_user_verification=True)
    except Exception as exc:  # noqa: BLE001
        raise PasskeyError("verification_failed") from exc
    stored = int(row.sign_count)
    new = int(verified.new_sign_count)
    regressed = (stored > 0 or new > 0) and new <= stored
    synced = bool(row.backup_eligible) or _multi_device(verified)
    if regressed:
        # Flagged either way (posture and the person's passkey list show it).
        db.execute(text("UPDATE identity_webauthn_credentials SET counter_regressed_at = now() "
                        "WHERE id = :id"), {"id": row.id})
        if not synced:
            # A device-bound key whose counter went backwards is a clone: refused (L3).
            raise PasskeyError("counter_regressed", VerifiedPasskey(row.principal_id, row.id, True))
    db.execute(text("UPDATE identity_webauthn_credentials SET sign_count = GREATEST(sign_count, :n), "
                    "last_used_at = now(), backup_state = :bs WHERE id = :id"),
               {"n": new, "bs": bool(verified.credential_backed_up), "id": row.id})
    return VerifiedPasskey(row.principal_id, row.id, regressed)


def rename(db: Any, principal_id: str, passkey_id: str, name: str) -> bool:
    return db.execute(text("UPDATE identity_webauthn_credentials SET name = :n WHERE id = :id AND "
                           "principal_id = :p RETURNING id"),
                      {"n": (name or "").strip()[:80] or "Passkey", "id": passkey_id,
                       "p": principal_id}).first() is not None


def delete(db: Any, principal_id: str, passkey_id: str) -> bool:
    return db.execute(text("DELETE FROM identity_webauthn_credentials WHERE id = :id AND principal_id = :p "
                           "RETURNING id"), {"id": passkey_id, "p": principal_id}).first() is not None

