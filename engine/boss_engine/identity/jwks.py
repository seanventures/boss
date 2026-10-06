"""Compact JWS for BOSS's own tokens: EdDSA (Ed25519) only, keys resolved only locally. Owned by S3.

BOSS signs and verifies its own run tokens, actor assertions and DPoP proofs here, with no JWT
library in between, so every check the JWT BCP (RFC 8725) asks for is visible in one place:

  - `alg` must be exactly "EdDSA". `none`, HS*, RS*, ES* are refused before anything else is read,
    so an HS256 token "signed" with the public key (algorithm confusion) never reaches a MAC.
  - `typ` must be the one the caller expects (`boss-run+jwt`, `boss-actor+jwt`, `dpop+jwt`); a token
    of one family is never accepted as another (A43).
  - `kid` is looked up ONLY in the caller's local key set. `jku`, `x5u`, `x5c` and `jwk` headers
    are never read for verification (they are ignored, not followed), and an unknown `kid` is a
    refusal without any fetch. DPoP is the one exception: its proof carries its own public `jwk`,
    and the caller then pins that key to the token's `cnf.jkt` (dpop.py).
  - `crit` is refused: BOSS understands no critical extension.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALG = "EdDSA"
MAX_TOKEN_BYTES = 16 * 1024


class JoseError(Exception):
    """A token is malformed or does not verify. `code` is a short machine reason (never a secret)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    if not isinstance(text, str) or any(c in text for c in "=+/ \n"):
        raise JoseError("malformed")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception as exc:  # noqa: BLE001
        raise JoseError("malformed") from exc


def canonical(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---- keys ------------------------------------------------------------------------------------------

def public_jwk(public_key: Ed25519PublicKey, *, kid: Optional[str] = None) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization

    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk: dict[str, Any] = {"kty": "OKP", "crv": "Ed25519", "x": b64u(raw)}
    if kid:
        jwk.update(kid=kid, alg=ALG, use="sig")
    return jwk


def public_key_from_jwk(jwk: Mapping[str, Any]) -> Ed25519PublicKey:
    """Only a PUBLIC Ed25519 OKP key. A JWK carrying private material (`d`) is refused."""
    if not isinstance(jwk, Mapping) or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise JoseError("bad_jwk")
    if "d" in jwk:
        raise JoseError("bad_jwk")
    raw = b64u_decode(str(jwk.get("x", "")))
    if len(raw) != 32:
        raise JoseError("bad_jwk")
    return Ed25519PublicKey.from_public_bytes(raw)


def thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url): the `cnf.jkt` of a DPoP-bound token."""
    if jwk.get("kty") != "OKP":
        raise JoseError("bad_jwk")
    members = {"crv": jwk["crv"], "kty": "OKP", "x": jwk["x"]}
    return b64u(hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":"))
                               .encode("utf-8")).digest())


# ---- sign / verify -----------------------------------------------------------------------------------

def sign(header: Mapping[str, Any], claims: Mapping[str, Any], private_key: Ed25519PrivateKey) -> str:
    if header.get("alg") != ALG:
        raise JoseError("bad_alg")
    signing_input = f"{b64u(canonical(header))}.{b64u(canonical(claims))}".encode("ascii")
    return signing_input.decode("ascii") + "." + b64u(private_key.sign(signing_input))


@dataclass(frozen=True)
class Unverified:
    header: dict[str, Any]
    claims: dict[str, Any]
    signing_input: bytes
    signature: bytes


def split(token: str) -> Unverified:
    """Parse WITHOUT trusting anything. Callers must still verify."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
        raise JoseError("malformed")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts[:2]):
        raise JoseError("malformed")
    try:
        header = json.loads(b64u_decode(parts[0]))
        claims = json.loads(b64u_decode(parts[1]))
    except JoseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise JoseError("malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise JoseError("malformed")
    return Unverified(header, claims, f"{parts[0]}.{parts[1]}".encode("ascii"), b64u_decode(parts[2]))


def check_header(header: Mapping[str, Any], *, typ: str) -> None:
    """The header rules, in the order that keeps a hostile header from steering anything."""
    if header.get("alg") != ALG:           # none / HS256 / RS256 / ... — algorithm confusion
        raise JoseError("bad_alg")
    if header.get("typ") != typ:
        raise JoseError("bad_typ")
    if "crit" in header:
        raise JoseError("bad_crit")
    # jku / x5u / x5c / jwk are deliberately never consulted here.


def verify(token: str, *, typ: str, key_for_kid: Callable[[str], Optional[Ed25519PublicKey]]
           ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify a BOSS-issued JWS. `key_for_kid` resolves a kid from the LOCAL key set only and
    returns None for an unknown one (refused, never fetched)."""
    parsed = split(token)
    check_header(parsed.header, typ=typ)
    kid = parsed.header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 128:
        raise JoseError("bad_kid")
    key = key_for_kid(kid)
    if key is None:
        raise JoseError("unknown_kid")
    try:
        key.verify(parsed.signature, parsed.signing_input)
    except InvalidSignature as exc:
        raise JoseError("bad_signature") from exc
    return parsed.header, parsed.claims


def verify_with_embedded_jwk(token: str, *, typ: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """DPoP only: the proof is signed by the public key in its own `jwk` header. The caller MUST pin
    that key (thumbprint == the access token's cnf.jkt)."""
    parsed = split(token)
    check_header(parsed.header, typ=typ)
    jwk = parsed.header.get("jwk")
    key = public_key_from_jwk(jwk)          # refuses private keys and non-Ed25519 keys
    try:
        key.verify(parsed.signature, parsed.signing_input)
    except InvalidSignature as exc:
        raise JoseError("bad_signature") from exc
    return parsed.header, parsed.claims, dict(jwk)
