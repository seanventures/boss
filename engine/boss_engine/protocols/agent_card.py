"""SIGNED A2A AGENT CARDS (A2A v1.0 §4.4.7 AgentCardSignature, §8.4 signing; AP-P5, BOSSAPP-1056).

A card is signed with JWS (RFC 7515) in DETACHED-payload form: each entry of `card.signatures`
is `{"protected": b64url(header), "signature": b64url(sig)}`; the signing input is

    protected + "." + b64url(canonical(card without `signatures`))

`canonical` is RFC 8785 (JCS) as far as BOSS's cards need it: object keys sorted, no
whitespace, UTF-8 (no ASCII escaping). Floats are NOT normalised to JCS's ECMAScript number
form — a card with float values from a peer that canonicalises them differently will not verify
(A2A cards carry strings, booleans, lists and objects; none of BOSS's carry a float).

The protected header holds `alg` ("EdDSA" — Ed25519 is the only algorithm accepted), `typ`
("JOSE"), `kid` (the key's id in the JWKS) and optionally `jku` (where the JWKS is published).
Verification looks the kid up ONLY in the JWKS the caller passes — `jku` is reported, never
fetched or trusted here (the caller decides which JWKS it trusts: BOSS's agent JWKS at
/v1/agents/{agent_id}/jwks for a BOSS card). `alg: none`, HS*, RS*, any `crit` are refused.

Not the official A2A SDK (it is not installed in this environment): a minimal, spec-conformant
implementation of the card signature only.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..identity.jwks import JoseError, b64u, b64u_decode, public_key_from_jwk

ALG = "EdDSA"
TYP = "JOSE"


class CardSignatureError(ValueError):
    """The card does not verify. `code` is a short machine reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical(card: Mapping[str, Any]) -> bytes:
    """JCS-style canonical bytes of the card WITHOUT its `signatures` (see the module docstring)."""
    body = {k: v for k, v in dict(card).items() if k != "signatures"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sign_card(card: Mapping[str, Any], private_key: Ed25519PrivateKey, kid: str, *,
              jku: Optional[str] = None) -> dict:
    """The card with one more AgentCardSignature appended (existing signatures are kept and are
    not part of what is signed)."""
    if not kid:
        raise CardSignatureError("bad_kid")
    header = {"alg": ALG, "typ": TYP, "kid": kid}
    if jku:
        header["jku"] = jku
    protected = b64u(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signing_input = (protected + "." + b64u(canonical(card))).encode("ascii")
    entry = {"protected": protected, "signature": b64u(private_key.sign(signing_input))}
    out = dict(card)
    out["signatures"] = list(card.get("signatures") or []) + [entry]
    return out


def signing_input(card: Mapping[str, Any], protected: str) -> bytes:
    return (protected + "." + b64u(canonical(card))).encode("ascii")


def _header(protected: str) -> dict:
    try:
        header = json.loads(b64u_decode(protected))
    except (JoseError, ValueError) as exc:
        raise CardSignatureError("malformed") from exc
    if not isinstance(header, dict):
        raise CardSignatureError("malformed")
    if header.get("alg") != ALG:
        raise CardSignatureError("bad_alg")
    if "crit" in header:
        raise CardSignatureError("bad_crit")
    if header.get("typ") not in (None, TYP):
        raise CardSignatureError("bad_typ")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 128:
        raise CardSignatureError("bad_kid")
    return header


def verify_card(card: Mapping[str, Any], jwks: Mapping[str, Any]) -> dict:
    """Verify the card against `jwks` ({"keys": [...]}). Returns the header of the first signature
    that verifies; raises CardSignatureError when none does (or there is none)."""
    signatures = card.get("signatures") if isinstance(card, Mapping) else None
    if not isinstance(signatures, list) or not signatures:
        raise CardSignatureError("unsigned")
    keys = {str(k.get("kid")): k for k in (jwks or {}).get("keys") or []
            if isinstance(k, Mapping) and k.get("kid")}
    last = CardSignatureError("bad_signature")
    for entry in signatures:
        try:
            if not isinstance(entry, Mapping):
                raise CardSignatureError("malformed")
            protected, sig = str(entry.get("protected") or ""), str(entry.get("signature") or "")
            header = _header(protected)
            jwk = keys.get(header["kid"])
            if jwk is None:
                raise CardSignatureError("unknown_kid")
            if str(jwk.get("status") or "active") == "revoked":
                raise CardSignatureError("revoked_key")
            try:
                key = public_key_from_jwk(jwk)
                key.verify(b64u_decode(sig), signing_input(card, protected))
            except (JoseError, InvalidSignature) as exc:
                raise CardSignatureError("bad_signature") from exc
            return header
        except CardSignatureError as exc:
            last = exc
    raise last


__all__ = ["ALG", "CardSignatureError", "TYP", "canonical", "sign_card", "signing_input",
           "verify_card"]
