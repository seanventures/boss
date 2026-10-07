"""Signing keys for run tokens and actor assertions: Ed25519, vaulted, rotated, published. Owned by S3.

IDENTITY.md §7.5. Each family (`run`, `actor`) has its own keys. A key moves
    next -> active -> retiring -> retired
`provision` creates the first active key and a `next` key (explicitly, at bootstrap or by the
operator, never as a silent side effect of signing: Kaappu flaw #3). `rotate` (every 30 days) makes
`next` active, the old active `retiring` until `retire_after` (the longest a token it signed can
live, plus skew), then `retired`. The JWKS publishes active + retiring: two keys at a time.

Private keys live in the vault (kind `signing_key`); `identity_signing_keys` holds the public JWK
and the vault item id. Signing without a provisioned key raises `SigningKeysMissing`: BOSS refuses to
run rather than invent an ephemeral key.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import jwks

FAMILIES = ("run", "actor")
ROTATE_EVERY = timedelta(days=30)
#: A retiring key keeps verifying for the longest lifetime of anything it signed, plus skew.
OVERLAP = timedelta(seconds=300 + 30)


class SigningKeysMissing(Exception):
    """No active signing key for a family. Provision keys; BOSS never generates one silently."""


@dataclass(frozen=True)
class SigningKey:
    kid: str
    family: str
    private_key: Ed25519PrivateKey


_cache_lock = threading.Lock()
_private_cache: dict[str, Ed25519PrivateKey] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_key_row(family: str, status: str, *, org_id: str, db: Any) -> str:
    from sqlalchemy import text

    from . import vault
    from .ids import new_id

    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    item_id = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.SIGNING_KEY,
                          provider="boss", secret=pem, db=db, connection_name=f"{family}-signing-key")
    kid = f"{family}-" + new_id("key")[4:20]
    db.execute(text(
        "INSERT INTO identity_signing_keys (kid, family, alg, public_jwk, private_vault_item_id, status, "
        " activated_at) VALUES (:kid, :fam, 'EdDSA', CAST(:jwk AS JSONB), :item, :status, "
        " CASE WHEN :status = 'active' THEN now() END)"),
        {"kid": kid, "fam": family, "jwk": _json(jwks.public_jwk(private.public_key(), kid=kid)),
         "item": item_id, "status": status})
    with _cache_lock:
        _private_cache[kid] = private
    return kid


def _json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def provision(*, org_id: str, db: Any) -> dict[str, str]:
    """Create an active and a next key for every family that has none. Idempotent."""
    from sqlalchemy import text

    out: dict[str, str] = {}
    for family in FAMILIES:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"identity_signing_{family}"})
        active = db.execute(text("SELECT kid FROM identity_signing_keys WHERE family = :f AND status = 'active'"),
                            {"f": family}).scalar()
        if not active:
            active = _new_key_row(family, "active", org_id=org_id, db=db)
        has_next = db.execute(text("SELECT 1 FROM identity_signing_keys WHERE family = :f AND status = 'next'"),
                              {"f": family}).scalar()
        if not has_next:
            _new_key_row(family, "next", org_id=org_id, db=db)
        out[family] = active
    return out


def rotate(family: str, *, org_id: str, db: Any, now: Optional[datetime] = None) -> str:
    """next -> active, active -> retiring (until now + OVERLAP), expired retiring -> retired, and a
    fresh next. Returns the new active kid."""
    from sqlalchemy import text

    if family not in FAMILIES:
        raise ValueError(family)
    now = now or _now()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"identity_signing_{family}"})
    nxt = db.execute(text("SELECT kid FROM identity_signing_keys WHERE family = :f AND status = 'next' "
                          "ORDER BY created_at LIMIT 1"), {"f": family}).scalar()
    if not nxt:
        nxt = _new_key_row(family, "next", org_id=org_id, db=db)
    db.execute(text("UPDATE identity_signing_keys SET status = 'retired' WHERE family = :f "
                    "AND status = 'retiring' AND retire_after <= :now"), {"f": family, "now": now})
    db.execute(text("UPDATE identity_signing_keys SET status = 'retiring', retire_after = :ra "
                    "WHERE family = :f AND status = 'active'"), {"f": family, "ra": now + OVERLAP})
    db.execute(text("UPDATE identity_signing_keys SET status = 'active', activated_at = :now WHERE kid = :k"),
               {"k": nxt, "now": now})
    _new_key_row(family, "next", org_id=org_id, db=db)
    return nxt


def rotate_if_due(*, org_id: str, db: Any, now: Optional[datetime] = None) -> list[str]:
    """The 30-day schedule, plus retiring keys past their overlap -> retired."""
    from sqlalchemy import text

    now = now or _now()
    rotated = []
    for family in FAMILIES:
        db.execute(text("UPDATE identity_signing_keys SET status = 'retired' WHERE family = :f "
                        "AND status = 'retiring' AND retire_after <= :now"), {"f": family, "now": now})
        activated = db.execute(text("SELECT activated_at FROM identity_signing_keys WHERE family = :f "
                                    "AND status = 'active'"), {"f": family}).scalar()
        if activated is not None and now - activated >= ROTATE_EVERY:
            rotated.append(rotate(family, org_id=org_id, db=db, now=now))
    return rotated


def assert_ready(*, db: Any) -> None:
    """Boot check: refuse to serve agent runs without keys for both families."""
    from sqlalchemy import text

    for family in FAMILIES:
        if not db.execute(text("SELECT 1 FROM identity_signing_keys WHERE family = :f AND status = 'active'"),
                          {"f": family}).scalar():
            raise SigningKeysMissing(f"no active {family} signing key")


def require_keys(*, org_id: str, db: Any) -> None:
    """The API's boot step: rotate what is due, then REFUSE (SigningKeysMissing) unless both
    families have an active key whose private half decrypts from the vault. A process that cannot
    sign never starts and then fails each run with a 500 and no lineage."""
    from sqlalchemy import text

    rotate_if_due(org_id=org_id, db=db)
    assert_ready(db=db)
    for family in FAMILIES:
        row = db.execute(text("SELECT kid, private_vault_item_id FROM identity_signing_keys "
                              "WHERE family = :f AND status = 'active' ORDER BY activated_at DESC LIMIT 1"),
                         {"f": family}).mappings().first()
        try:
            _private(row["kid"], row["private_vault_item_id"], db)
        except Exception as exc:  # noqa: BLE001 — any failure to open the key is a refusal
            raise SigningKeysMissing(f"the active {family} signing key cannot be opened") from exc


def _private(kid: str, item_id: str, db: Any) -> Ed25519PrivateKey:
    from . import vault

    with _cache_lock:
        cached = _private_cache.get(kid)
    if cached is not None:
        return cached
    pem = vault.read_secret(item_id, db=db, kind=vault.VaultItemKind.SIGNING_KEY)
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeysMissing("signing key is not Ed25519")
    with _cache_lock:
        _private_cache[kid] = key
    return key


def active_key(family: str, *, db: Any) -> SigningKey:
    from sqlalchemy import text

    row = db.execute(text("SELECT kid, private_vault_item_id FROM identity_signing_keys "
                          "WHERE family = :f AND status = 'active' ORDER BY activated_at DESC LIMIT 1"),
                     {"f": family}).mappings().first()
    if row is None:
        raise SigningKeysMissing(f"no active {family} signing key")
    return SigningKey(row["kid"], family, _private(row["kid"], row["private_vault_item_id"], db))


def verification_key(kid: str, family: str, *, db: Any, now: Optional[datetime] = None
                     ) -> Optional[Ed25519PublicKey]:
    """The public key for `kid` in `family` if it may verify NOW: active, or retiring inside its
    overlap. Unknown, retired, `next`, or another family's kid: None (refused, never fetched)."""
    from sqlalchemy import text

    now = now or _now()
    row = db.execute(text("SELECT public_jwk, status, retire_after FROM identity_signing_keys "
                          "WHERE kid = :k AND family = :f"), {"k": kid, "f": family}).mappings().first()
    if row is None:
        return None
    if row["status"] == "active" or (row["status"] == "retiring" and row["retire_after"] and now < row["retire_after"]):
        return jwks.public_key_from_jwk(row["public_jwk"])
    return None


def jwks_document(*, db: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    """`/.well-known/boss-jwks.json`: the keys that may verify right now (active + retiring)."""
    from sqlalchemy import text

    now = now or _now()
    rows = db.execute(text("SELECT public_jwk FROM identity_signing_keys WHERE status = 'active' "
                           "OR (status = 'retiring' AND retire_after > :now) ORDER BY family, created_at"),
                      {"now": now}).mappings().all()
    keys = []
    for r in rows:
        jwk = dict(r["public_jwk"])
        jwk.pop("d", None)            # defence in depth: never publish private material
        keys.append(jwk)
    return {"keys": keys}


def clear_cache() -> None:
    with _cache_lock:
        _private_cache.clear()
