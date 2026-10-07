"""DPoP (RFC 9449) proofs: the run token is useless without the shim's private key. Owned by S3.

The credential shim in a sandbox holds an Ed25519 key it generated at boot; the run supervisor binds
the run token to its thumbprint (`cnf.jkt`). Every request carries a fresh proof:

    header  {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": <the shim's PUBLIC key>}
    claims  {"jti", "htm", "htu", "iat", "ath": b64url(sha256(access_token))}

`verify_proof` checks, in order: header (alg, typ, a public Ed25519 jwk), signature, the key's
thumbprint == cnf.jkt, htm, htu (scheme, host and path; query and fragment ignored per RFC 9449),
iat inside the window, ath, then the jti replay cache (identity_dpop_replay) — A41, A42.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import jwks

TYP = "dpop+jwt"
#: A proof older (or newer) than this, after skew, is refused.
MAX_AGE_SECONDS = 60
CLOCK_SKEW_SECONDS = 30


class DPoPError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DPoPKey:
    """What the shim holds. The private half never leaves the shim's memory."""
    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "DPoPKey":
        return cls(Ed25519PrivateKey.generate())

    @property
    def public_jwk(self) -> dict[str, Any]:
        return jwks.public_jwk(self.private_key.public_key())

    @property
    def jkt(self) -> str:
        return jwks.thumbprint(self.public_jwk)

    def proof(self, htm: str, htu: str, *, access_token: Optional[str] = None,
              iat: Optional[int] = None, jti: Optional[str] = None) -> str:
        claims: dict[str, Any] = {"jti": jti or secrets.token_urlsafe(16), "htm": htm.upper(),
                                  "htu": normalize_htu(htu), "iat": int(iat if iat is not None else time.time())}
        if access_token is not None:
            claims["ath"] = ath_of(access_token)
        return jwks.sign({"typ": TYP, "alg": jwks.ALG, "jwk": self.public_jwk}, claims, self.private_key)


def ath_of(access_token: str) -> str:
    return jwks.b64u(hashlib.sha256(access_token.encode("ascii")).digest())


def normalize_htu(url: str) -> str:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{parts.path or '/'}"


def verify_proof(proof: Optional[str], *, htm: str, htu: str, access_token: Optional[str],
                 expected_jkt: Optional[str], db: Any, now: Optional[float] = None) -> str:
    """Return the proof key's thumbprint, or raise DPoPError. `db` holds the replay cache."""
    if not proof:
        raise DPoPError("dpop_missing")
    try:
        _, claims, jwk = jwks.verify_with_embedded_jwk(proof, typ=TYP)
    except jwks.JoseError as exc:
        raise DPoPError(f"dpop_{exc.code}") from exc
    jkt = jwks.thumbprint(jwk)
    if expected_jkt is not None and not secrets.compare_digest(jkt, expected_jkt):
        raise DPoPError("dpop_key_mismatch")
    if str(claims.get("htm", "")).upper() != htm.upper():
        raise DPoPError("dpop_htm")
    if claims.get("htu") != normalize_htu(htu):
        raise DPoPError("dpop_htu")
    now = time.time() if now is None else now
    iat = claims.get("iat")
    # Past: at most MAX_AGE + skew old. Future: at most the clock skew (a proof dated ahead would
    # otherwise stay valid for longer than its replay record is kept).
    if not isinstance(iat, int) or now - iat > MAX_AGE_SECONDS + CLOCK_SKEW_SECONDS or iat - now > CLOCK_SKEW_SECONDS:
        raise DPoPError("dpop_iat")
    if access_token is not None:
        if claims.get("ath") != ath_of(access_token):
            raise DPoPError("dpop_ath")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not (8 <= len(jti) <= 128):
        raise DPoPError("dpop_jti")
    _remember(db, jkt, jti, now, iat=iat)
    return jkt


def _remember(db: Any, jkt: str, jti: str, now: float, *, iat: Optional[float] = None) -> None:
    """Insert (jkt, jti); a second insert of the same pair is a replay (A42). The record is kept
    at least as long as the proof could still be accepted (iat + MAX_AGE + skew), plus margin."""
    from sqlalchemy import text

    valid_until = max(now, iat if iat is not None else now) + MAX_AGE_SECONDS + 2 * CLOCK_SKEW_SECONDS
    expires = datetime.fromtimestamp(valid_until, timezone.utc)
    inserted = db.execute(text(
        "INSERT INTO identity_dpop_replay (jkt, jti, expires_at) VALUES (:k, :j, :e) "
        "ON CONFLICT (jkt, jti) DO NOTHING RETURNING 1"), {"k": jkt, "j": jti, "e": expires}).scalar()
    if not inserted:
        raise DPoPError("dpop_replay")
    if secrets.randbelow(50) == 0:
        db.execute(text("DELETE FROM identity_dpop_replay WHERE expires_at < now()"))
