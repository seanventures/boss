"""The `oidc` signing-key family (§6.2, D10, IDENTITY.md D35).

Per org and per algorithm (RS256, the default for ID tokens and the only one for access tokens;
EdDSA, per client for ID tokens). Each key moves next -> active -> retiring -> retired like the
BOSS families (identity/signing_keys.py), but lives in `oidc_signing_keys`, never in
`identity_signing_keys`: BOSS's internal verifiers look keys up only there, so an app token can
never verify as a BOSS token, and an `oidc` verification looks keys up only here, so a BOSS token
can never verify as an app token (S6.6).

Private keys live in the vault (kind signing_key, provider `boss-oidc`). `provision` is explicit
(the first client or pool, or the operator); signing without a key raises KeysMissing.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from sqlalchemy import text

from .util import audit, b64u, new_id, now as _now

ALGS = ("RS256", "EdDSA")
ROTATE_EVERY = timedelta(days=30)
#: A retiring key keeps verifying (and stays in the JWKS) for a day: longer than anything it
#: signed lives (10 min), and long enough for apps that cache the JWKS.
OVERLAP = timedelta(days=1)
VAULT_PROVIDER = "boss-oidc"


class KeysMissing(Exception):
    """No active `oidc` key for this org and algorithm. Provision; never invented silently."""


_lock = threading.Lock()
_private: dict[str, Any] = {}


def _b64u_int(value: int) -> str:
    return b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def public_jwk(private_key: Any, *, kid: str, alg: str) -> dict[str, Any]:
    pub = private_key.public_key()
    if alg == "RS256":
        numbers = pub.public_numbers()
        return {"kty": "RSA", "n": _b64u_int(numbers.n), "e": _b64u_int(numbers.e), "kid": kid,
                "alg": "RS256", "use": "sig"}
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": b64u(raw), "kid": kid, "alg": "EdDSA", "use": "sig"}


def _generate(alg: str) -> Any:
    if alg == "RS256":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if alg == "EdDSA":
        return ed25519.Ed25519PrivateKey.generate()
    raise ValueError(alg)


def _new_key(db: Any, org_id: str, alg: str, status: str) -> str:
    from ..identity import vault

    private = _generate(alg)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    kid = "oidc-" + ("rs-" if alg == "RS256" else "ed-") + new_id("k")[2:18]
    item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.SIGNING_KEY,
                       provider=VAULT_PROVIDER, secret=pem, db=db, connection_name=kid)
    db.execute(text(
        "INSERT INTO oidc_signing_keys (kid, org_id, alg, public_jwk, private_vault_item_id, status, "
        "activated_at) VALUES (:kid, :org, :alg, CAST(:jwk AS JSONB), :item, :status, "
        "CASE WHEN :status = 'active' THEN now() END)"),
        {"kid": kid, "org": org_id, "alg": alg, "jwk": json.dumps(public_jwk(private, kid=kid, alg=alg)),
         "item": item, "status": status})
    with _lock:
        _private[kid] = private
    return kid


def _xlock(db: Any, org_id: str, alg: str) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"oidc_signing:{org_id}:{alg}"})


def provision(db: Any, *, org_id: str, actor: Optional[str] = None) -> dict[str, str]:
    """An active and a next key for each algorithm that has none. Idempotent."""
    out: dict[str, str] = {}
    created = False
    for alg in ALGS:
        _xlock(db, org_id, alg)
        active = db.execute(text("SELECT kid FROM oidc_signing_keys WHERE org_id = :o AND alg = :a "
                                 "AND status = 'active'"), {"o": org_id, "a": alg}).scalar()
        if not active:
            active, created = _new_key(db, org_id, alg, "active"), True
        if not db.execute(text("SELECT 1 FROM oidc_signing_keys WHERE org_id = :o AND alg = :a "
                               "AND status = 'next'"), {"o": org_id, "a": alg}).scalar():
            _new_key(db, org_id, alg, "next")
            created = True
        out[alg] = active
    if created:
        audit(db, org_id=org_id, event_type="oidc.keys.provisioned", actor_principal_id=actor,
              target_type="oidc_keys", target_id=org_id, details={"algs": list(ALGS)})
    return out


def rotate(db: Any, *, org_id: str, alg: str, at: Optional[datetime] = None) -> str:
    """next -> active, active -> retiring (until at + OVERLAP), expired retiring -> retired, new next."""
    if alg not in ALGS:
        raise ValueError(alg)
    at = at or _now()
    _xlock(db, org_id, alg)
    nxt = db.execute(text("SELECT kid FROM oidc_signing_keys WHERE org_id = :o AND alg = :a AND status = 'next' "
                          "ORDER BY created_at LIMIT 1"), {"o": org_id, "a": alg}).scalar()
    if not nxt:
        nxt = _new_key(db, org_id, alg, "next")
    db.execute(text("UPDATE oidc_signing_keys SET status = 'retired' WHERE org_id = :o AND alg = :a "
                    "AND status = 'retiring' AND retire_after <= :at"), {"o": org_id, "a": alg, "at": at})
    db.execute(text("UPDATE oidc_signing_keys SET status = 'retiring', retire_after = :ra WHERE org_id = :o "
                    "AND alg = :a AND status = 'active'"), {"o": org_id, "a": alg, "ra": at + OVERLAP})
    db.execute(text("UPDATE oidc_signing_keys SET status = 'active', activated_at = :at WHERE kid = :k"),
               {"k": nxt, "at": at})
    _new_key(db, org_id, alg, "next")
    audit(db, org_id=org_id, event_type="oidc.keys.rotated", target_type="oidc_keys", target_id=org_id,
          details={"alg": alg, "kid": nxt})
    return nxt


def rotate_if_due(db: Any, *, org_id: Optional[str] = None, at: Optional[datetime] = None) -> list[str]:
    """The 30-day schedule for every org (or one), and retiring keys past their overlap -> retired."""
    at = at or _now()
    db.execute(text("UPDATE oidc_signing_keys SET status = 'retired' WHERE status = 'retiring' "
                    "AND retire_after <= :at" + (" AND org_id = :o" if org_id else "")), {"at": at, "o": org_id})
    rows = db.execute(text("SELECT org_id, alg FROM oidc_signing_keys WHERE status = 'active' "
                           "AND activated_at <= :due" + (" AND org_id = :o" if org_id else "")),
                      {"due": at - ROTATE_EVERY, "o": org_id}).all()
    return [rotate(db, org_id=r[0], alg=r[1], at=at) for r in rows]


def _load_private(db: Any, kid: str, item_id: str) -> Any:
    from ..identity import vault

    with _lock:
        cached = _private.get(kid)
    if cached is not None:
        return cached
    pem = vault.read_secret(item_id, db=db, kind=vault.VaultItemKind.SIGNING_KEY)
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    with _lock:
        _private[kid] = key
    return key


def signing_key(db: Any, *, org_id: str, alg: str) -> tuple[str, Any]:
    """(kid, private key) of the active key; rotates first when the key is due."""
    if alg not in ALGS:
        raise KeysMissing(alg)
    row = db.execute(text("SELECT kid, private_vault_item_id, activated_at FROM oidc_signing_keys "
                          "WHERE org_id = :o AND alg = :a AND status = 'active'"),
                     {"o": org_id, "a": alg}).mappings().first()
    if row is None:
        raise KeysMissing(f"no active {alg} oidc key")
    if row["activated_at"] is not None and _now() - row["activated_at"] >= ROTATE_EVERY:
        rotate_if_due(db, org_id=org_id)
        return signing_key(db, org_id=org_id, alg=alg)
    return row["kid"], _load_private(db, row["kid"], row["private_vault_item_id"])


def verification_key(db: Any, *, org_id: str, kid: str, at: Optional[datetime] = None
                     ) -> Optional[tuple[str, Any]]:
    """(alg, public key) for `kid` if it may verify now (active, or retiring inside its overlap)
    and belongs to this org. Anything else, including any BOSS-internal kid: None."""
    if not isinstance(kid, str) or not kid.startswith("oidc-") or len(kid) > 64:
        return None
    at = at or _now()
    row = db.execute(text("SELECT alg, public_jwk, status, retire_after FROM oidc_signing_keys "
                          "WHERE kid = :k AND org_id = :o"), {"k": kid, "o": org_id}).mappings().first()
    if row is None:
        return None
    if not (row["status"] == "active" or (row["status"] == "retiring" and row["retire_after"]
                                           and at < row["retire_after"])):
        return None
    import jwt

    jwk = dict(row["public_jwk"])
    return row["alg"], jwt.PyJWK(jwk, algorithm=row["alg"]).key


def jwks(db: Any, *, org_id: str, at: Optional[datetime] = None) -> dict[str, Any]:
    """The keys that may verify now: active + retiring, public members only."""
    at = at or _now()
    rows = db.execute(text("SELECT public_jwk FROM oidc_signing_keys WHERE org_id = :o AND (status = 'active' "
                           "OR (status = 'retiring' AND retire_after > :at)) ORDER BY alg, created_at"),
                      {"o": org_id, "at": at}).mappings().all()
    keys = []
    for r in rows:
        jwk = {k: v for k, v in dict(r["public_jwk"]).items() if k in ("kty", "n", "e", "crv", "x", "kid", "alg", "use")}
        keys.append(jwk)
    return {"keys": keys}


def clear_cache() -> None:
    with _lock:
        _private.clear()
