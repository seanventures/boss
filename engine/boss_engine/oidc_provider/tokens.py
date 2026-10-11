"""Grants, codes, refresh families, and the ID and access tokens (§6.2, §6.6 S6.3).

  code           opaque, SHA-256 at rest, single use, 60 s, bound to client + redirect_uri + PKCE
                 challenge + nonce; a second redemption revokes the grant (and so everything
                 issued from it)
  refresh token  opaque, SHA-256 at rest, rotated on every use; presenting a rotated or revoked
                 one revokes the whole family (= the grant)
  access token   JWT, typ at+jwt (RFC 9068), RS256, 10 min, aud = the client's resource
                 (or client_id), `sid` = the grant, checked live at userinfo
  ID token       JWT, typ JWT, RS256 by default or EdDSA per client, aud = client_id, azp

Verification of our own tokens names exactly one algorithm per key (the key's own), wants the
exact `typ`, and finds keys only in the `oidc` family of this org: `none`, HS256 with the public
key, and every BOSS-internal token are refused before a signature is computed.
"""
from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import keys
from .util import (ACCESS_TTL, CLOCK_SKEW, CODE_TTL, FAMILY_MAX_AGE, ID_TOKEN_TTL, REFRESH_TTL, TYP_ACCESS,
                   TYP_ID, OidcError, audit, new_id, now, random_token, s256, sha256_hex)

CODE_PREFIX = "oac_"            # opaque; never a BOSS credential prefix
REFRESH_PREFIX = "ort_"


# ---- grants ----------------------------------------------------------------------------------

def create_grant(db: Any, *, org_id: str, client_id: str, pool_id: str, subject_ref: str,
                 session_ref: Optional[str], scopes: list[str], acr: Optional[str], amr: list[str],
                 auth_time: datetime) -> str:
    gid = new_id("ogr")
    db.execute(text("INSERT INTO oidc_grants (id, org_id, client_id, pool_id, subject_ref, session_ref, scopes, acr, "
                    "amr, auth_time) VALUES (:id, :o, :c, :p, :s, :sr, :sc, :acr, :amr, :at)"),
               {"id": gid, "o": org_id, "c": client_id, "p": pool_id, "s": subject_ref, "sr": session_ref,
                "sc": scopes, "acr": acr, "amr": amr, "at": auth_time})
    return gid


def load_grant(db: Any, grant_id: Any) -> Optional[dict[str, Any]]:
    if not isinstance(grant_id, str):
        return None
    r = db.execute(text("SELECT * FROM oidc_grants WHERE id = :id"), {"id": grant_id}).mappings().first()
    return dict(r) if r else None


def revoke_grant(db: Any, grant_id: str, *, reason: str) -> None:
    db.execute(text("UPDATE oidc_grants SET revoked_at = now(), revoked_reason = :r WHERE id = :id AND revoked_at IS NULL"),
               {"id": grant_id, "r": reason})
    db.execute(text("UPDATE oidc_refresh_tokens SET revoked_at = now() WHERE family_id = :id AND revoked_at IS NULL"),
               {"id": grant_id})


def _revoke_where(db: Any, where: str, params: Mapping[str, Any], reason: str) -> int:
    ids = [r[0] for r in db.execute(text(f"SELECT id FROM oidc_grants WHERE revoked_at IS NULL AND {where}"), params)]
    for gid in ids:
        revoke_grant(db, gid, reason=reason)
    return len(ids)


def revoke_client_grants(db: Any, *, client_id: str, reason: str) -> int:
    return _revoke_where(db, "client_id = :c", {"c": client_id}, reason)


def revoke_subject_grants(db: Any, *, client_id: Optional[str], subject_ref: str, reason: str) -> int:
    if client_id is None:
        return _revoke_where(db, "subject_ref = :s", {"s": subject_ref}, reason)
    return _revoke_where(db, "client_id = :c AND subject_ref = :s", {"c": client_id, "s": subject_ref}, reason)


# ---- codes -----------------------------------------------------------------------------------

def issue_code(db: Any, *, grant_id: str, client_id: str, subject_ref: str, redirect_uri: str,
               code_challenge: str, nonce: Optional[str], scopes: list[str], acr: Optional[str],
               auth_time: datetime) -> str:
    code = random_token(CODE_PREFIX)
    db.execute(text("INSERT INTO oidc_auth_codes (code_hash, client_id, grant_id, subject_ref, redirect_uri, "
                    "code_challenge, nonce, scopes, acr, auth_time, expires_at) VALUES (:h, :c, :g, :s, :r, :cc, :n, "
                    ":sc, :acr, :at, :exp)"),
               {"h": sha256_hex(code), "c": client_id, "g": grant_id, "s": subject_ref, "r": redirect_uri,
                "cc": code_challenge, "n": nonce, "sc": scopes, "acr": acr, "at": auth_time,
                "exp": now() + CODE_TTL})
    return code


def redeem_code(db: Any, *, client: Mapping[str, Any], code: str, redirect_uri: Optional[str],
                code_verifier: Optional[str]) -> dict[str, Any]:
    """The code's row, after every check. A replay (or a code presented by another client)
    revokes the grant it came from. Raises OidcError(invalid_grant)."""
    if not isinstance(code, str) or not code.startswith(CODE_PREFIX) or len(code) > 200:
        raise OidcError("invalid_grant", "invalid code")
    row = db.execute(text("SELECT * FROM oidc_auth_codes WHERE code_hash = :h FOR UPDATE"),
                     {"h": sha256_hex(code)}).mappings().first()
    if row is None:
        raise OidcError("invalid_grant", "invalid code")
    row = dict(row)
    grant = load_grant(db, row["grant_id"])
    if row["used_at"] is not None or row["client_id"] != client["id"]:
        revoke_grant(db, row["grant_id"], reason="code_replayed" if row["used_at"] else "code_wrong_client")
        db.execute(text("UPDATE oidc_auth_codes SET used_at = coalesce(used_at, now()) WHERE code_hash = :h"),
                   {"h": row["code_hash"]})
        audit(db, org_id=client["org_id"], event_type="oidc.code.replayed", outcome="denied",
              actor_kind="oidc_client", target_type="oidc_client", target_id=client["id"],
              details={"grant_id": row["grant_id"], "presented_by": client["id"], "issued_to": row["client_id"]})
        raise OidcError("invalid_grant", "invalid code")
    db.execute(text("UPDATE oidc_auth_codes SET used_at = now() WHERE code_hash = :h"), {"h": row["code_hash"]})
    if row["expires_at"] <= now():
        raise OidcError("invalid_grant", "code expired")
    if not isinstance(redirect_uri, str) or redirect_uri != row["redirect_uri"]:
        raise OidcError("invalid_grant", "redirect_uri does not match the authorization request")
    if not isinstance(code_verifier, str) or not _verifier_shape(code_verifier):
        raise OidcError("invalid_grant", "code_verifier required (PKCE S256)")
    if not hmac.compare_digest(s256(code_verifier), row["code_challenge"]):
        raise OidcError("invalid_grant", "code_verifier does not match")
    if grant is None or grant["revoked_at"] is not None:
        raise OidcError("invalid_grant", "grant revoked")
    return {**row, "grant": grant}


def _verifier_shape(v: str) -> bool:
    import re

    return re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", v) is not None


# ---- refresh tokens --------------------------------------------------------------------------

def issue_refresh(db: Any, *, grant: Mapping[str, Any], scopes: list[str]) -> str:
    token = random_token(REFRESH_PREFIX)
    db.execute(text("INSERT INTO oidc_refresh_tokens (token_hash, family_id, client_id, subject_ref, scopes, expires_at) "
                    "VALUES (:h, :f, :c, :s, :sc, :exp)"),
               {"h": sha256_hex(token), "f": grant["id"], "c": grant["client_id"], "s": grant["subject_ref"],
                "sc": scopes, "exp": now() + REFRESH_TTL})
    return token


def use_refresh(db: Any, *, client: Mapping[str, Any], token: Any) -> dict[str, Any]:
    """Rotate: the presented token is spent and its row returned with its grant. A rotated or
    revoked token presented again revokes the family (reuse detection). Raises invalid_grant."""
    if not isinstance(token, str) or not token.startswith(REFRESH_PREFIX) or len(token) > 200:
        raise OidcError("invalid_grant", "invalid refresh token")
    row = db.execute(text("SELECT * FROM oidc_refresh_tokens WHERE token_hash = :h FOR UPDATE"),
                     {"h": sha256_hex(token)}).mappings().first()
    if row is None:
        raise OidcError("invalid_grant", "invalid refresh token")
    row = dict(row)
    if row["rotated_at"] is not None or row["revoked_at"] is not None or row["client_id"] != client["id"]:
        revoke_grant(db, row["family_id"], reason="refresh_reuse" if row["client_id"] == client["id"]
                     else "refresh_wrong_client")
        audit(db, org_id=client["org_id"], event_type="oidc.token.reuse_detected", outcome="denied",
              actor_kind="oidc_client", target_type="oidc_client", target_id=client["id"],
              details={"grant_id": row["family_id"]})
        raise OidcError("invalid_grant", "invalid refresh token")
    db.execute(text("UPDATE oidc_refresh_tokens SET rotated_at = now() WHERE token_hash = :h"), {"h": row["token_hash"]})
    grant = load_grant(db, row["family_id"])
    if row["expires_at"] <= now() or grant is None or grant["revoked_at"] is not None \
            or grant["created_at"] + FAMILY_MAX_AGE <= now():
        raise OidcError("invalid_grant", "refresh token expired or revoked")
    return {**row, "grant": grant}


# ---- JWTs ------------------------------------------------------------------------------------

def _sign(db: Any, *, org_id: str, alg: str, typ: str, claims: Mapping[str, Any]) -> str:
    import jwt

    kid, private = keys.signing_key(db, org_id=org_id, alg=alg)
    return jwt.encode(dict(claims), private, algorithm=alg, headers={"kid": kid, "typ": typ})


def mint_id_token(db: Any, *, issuer: str, client: Mapping[str, Any], grant: Mapping[str, Any],
                  claims: Mapping[str, Any], nonce: Optional[str]) -> str:
    t = int(now().timestamp())
    body = {**claims, "iss": issuer, "aud": client["id"], "azp": client["id"], "iat": t,
            "exp": t + int(ID_TOKEN_TTL.total_seconds()), "auth_time": int(grant["auth_time"].timestamp()),
            "sid": grant["id"]}
    if grant.get("acr"):
        body["acr"] = grant["acr"]
    if grant.get("amr"):
        body["amr"] = list(grant["amr"])
    if nonce:
        body["nonce"] = nonce
    return _sign(db, org_id=client["org_id"], alg=client["id_token_alg"], typ=TYP_ID, claims=body)


def mint_access_token(db: Any, *, issuer: str, client: Mapping[str, Any], grant: Mapping[str, Any], sub: str,
                      scopes: list[str]) -> str:
    t = int(now().timestamp())
    body = {"iss": issuer, "sub": sub, "aud": client.get("resource") or client["id"], "client_id": client["id"],
            "iat": t, "exp": t + int(ACCESS_TTL.total_seconds()), "jti": new_id("oat"),
            "scope": " ".join(scopes), "auth_time": int(grant["auth_time"].timestamp()), "sid": grant["id"]}
    if grant.get("acr"):
        body["acr"] = grant["acr"]
    return _sign(db, org_id=client["org_id"], alg="RS256", typ=TYP_ACCESS, claims=body)


def verify_own(db: Any, *, org_id: str, issuer: str, token: Any, typ: str, verify_exp: bool = True,
               audience: Optional[str] = None) -> dict[str, Any]:
    """Verify a token this provider issued for this org. Raises OidcError(invalid_token, 401)."""
    import jwt

    bad = OidcError("invalid_token", "the token is not valid here", 401)
    if not isinstance(token, str) or len(token) > 16384 or token.count(".") != 2:
        raise bad
    try:
        header = jwt.get_unverified_header(token)
    except Exception:  # noqa: BLE001
        raise bad from None
    if header.get("typ") != typ or "crit" in header or header.get("alg") not in keys.ALGS:
        raise bad
    found = keys.verification_key(db, org_id=org_id, kid=header.get("kid"))
    if found is None:
        raise bad
    alg, key = found
    if header["alg"] != alg or (typ == TYP_ACCESS and alg != "RS256"):
        raise bad
    try:
        claims = jwt.decode(token, key=key, algorithms=[alg], issuer=issuer,
                            audience=audience, leeway=int(CLOCK_SKEW.total_seconds()),
                            options={"verify_exp": verify_exp, "verify_aud": audience is not None,
                                     "require": ["iss", "sub", "iat", "exp"]})
    except Exception:  # noqa: BLE001
        raise bad from None
    return claims
