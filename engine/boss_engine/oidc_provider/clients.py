"""The client registry, the redirect-URI rules, and client authentication at the token and
revocation endpoints (§6.2, §6.3, §6.5).

Redirect URIs are registered absolute and compared byte for byte (no wildcard, no prefix, no
normalisation at match time). Registration refuses what could become an open redirect or a
confusion: no fragment, no userinfo, no dot segments, no wildcard, https only except loopback.

Client authentication is exactly the registered method, never a weaker one:
  client_secret_basic  BOSS-generated 32-byte secret, in the vault; compared in constant time;
                       N failures in the window lock the client's secret auth (S6.10)
  private_key_jwt      an assertion signed by a key in the client's INLINE JWKS (no jwks_uri
                       fetch: S6.5), asymmetric algs only, aud = this token endpoint or issuer,
                       short-lived, jti single-use
  none                 public clients only (PKCE carries the proof)
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import re
from typing import Any, Mapping, Optional
from urllib.parse import unquote_plus, urlsplit

from sqlalchemy import text

from .util import CLOCK_SKEW, OidcError, SUPPORTED_SCOPES, audit, b64u, new_id, now, sha256_hex

AUTH_METHODS = ("client_secret_basic", "private_key_jwt", "none")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_ALGS = {"RS256": "RSA", "PS256": "RSA", "ES256": "EC", "EdDSA": "OKP"}
ASSERTION_MAX_LIFETIME_S = 300
SECRET_FAILURE_LIMIT = 10
SECRET_FAILURE_WINDOW_S = 900
MAX_REDIRECT_URIS = 20
MAX_URI_LEN = 2000
VAULT_PROVIDER = "boss-oidc-client"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CTRL = re.compile(r"[\x00-\x20\x7f\\]")

COLUMNS = ("id, org_id, project_id, app_key, pool_id, name, type, redirect_uris, post_logout_redirect_uris, "
           "allowed_scopes, token_endpoint_auth_method, jwks, secret_vault_item_id, id_token_alg, "
           "subject_type, require_mfa, first_party, resource, status, created_by, created_at, rotated_at")


# ---- registration rules ----------------------------------------------------------------------

def check_redirect_uri(uri: Any) -> str:
    """The registered form of a redirect URI, or ValueError(reason). Matching is exact later."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LEN:
        raise ValueError("redirect_uri must be a non-empty string")
    if _CTRL.search(uri) or "*" in uri:
        raise ValueError("redirect_uri may not contain whitespace, backslashes, control characters or '*'")
    parts = urlsplit(uri)
    if parts.scheme not in ("https", "http"):
        raise ValueError("redirect_uri must be https (or http on loopback)")
    if "#" in uri or parts.fragment:
        raise ValueError("redirect_uri may not have a fragment")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("redirect_uri may not carry userinfo")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("redirect_uri must name a host")
    try:
        parts.port
    except ValueError:
        raise ValueError("redirect_uri has an invalid port") from None
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError("redirect_uri must be https unless the host is loopback")
    segments = parts.path.split("/")
    if any(s in (".", "..") or unquote_plus(s) in (".", "..") for s in segments) or "%2f" in parts.path.lower() \
            or "%5c" in parts.path.lower():
        raise ValueError("redirect_uri may not contain dot segments or encoded slashes")
    return uri


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_jwks(jwks: Any) -> dict[str, Any]:
    """An inline public JWKS for private_key_jwt: public members only, asymmetric kty."""
    if not isinstance(jwks, Mapping) or not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
        raise ValueError("jwks must be {\"keys\": [...]} with at least one key")
    if len(json.dumps(jwks)) > 20000 or len(jwks["keys"]) > 10:
        raise ValueError("jwks is too large")
    out, kids = [], set()
    for key in jwks["keys"]:
        if not isinstance(key, Mapping):
            raise ValueError("each jwk must be an object")
        kty = key.get("kty")
        if kty not in ("RSA", "EC", "OKP"):
            raise ValueError("jwk kty must be RSA, EC or OKP (no symmetric keys)")
        if any(k in key for k in ("d", "p", "q", "dp", "dq", "qi", "k", "oth")):
            raise ValueError("jwks may hold public keys only")
        if any(k in key for k in ("x5u", "jku")):
            raise ValueError("jwk may not point at a URL")
        kid = key.get("kid")
        if kid is not None and (not isinstance(kid, str) or kid in kids):
            raise ValueError("jwk kids must be unique strings")
        kids.add(kid)
        import jwt

        try:
            jwt.PyJWK(dict(key))
        except Exception:  # noqa: BLE001
            raise ValueError("jwk is not a valid public key") from None
        out.append(dict(key))
    return {"keys": out}


def validate(body: Mapping[str, Any], *, existing: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The full, checked client definition (create or the merged result of an update)."""
    c = dict(existing or {})
    c.update({k: v for k, v in body.items() if v is not None})
    ctype = c.get("type") or "confidential"
    if ctype not in ("confidential", "public"):
        raise ValueError("type must be confidential or public")
    method = c.get("token_endpoint_auth_method") or ("none" if ctype == "public" else "client_secret_basic")
    if method not in AUTH_METHODS:
        raise ValueError(f"token_endpoint_auth_method must be one of {', '.join(AUTH_METHODS)}")
    if (ctype == "public") != (method == "none"):
        raise ValueError("public clients authenticate with 'none' and only they do")
    uris = list(c.get("redirect_uris") or [])
    if not uris or len(uris) > MAX_REDIRECT_URIS:
        raise ValueError(f"register 1 to {MAX_REDIRECT_URIS} redirect_uris")
    uris = [check_redirect_uri(u) for u in uris]
    logout = [check_redirect_uri(u) for u in list(c.get("post_logout_redirect_uris") or [])]
    if len(logout) > MAX_REDIRECT_URIS:
        raise ValueError("too many post_logout_redirect_uris")
    scopes = list(dict.fromkeys(c.get("allowed_scopes") or ["openid"]))
    unknown = [s for s in scopes if s not in SUPPORTED_SCOPES]
    if unknown or "openid" not in scopes:
        raise ValueError(f"allowed_scopes must include openid and only {', '.join(SUPPORTED_SCOPES)}")
    jwks = c.get("jwks")
    if method == "private_key_jwt":
        jwks = check_jwks(jwks)
    elif jwks:
        raise ValueError("jwks is only for private_key_jwt clients")
    alg = c.get("id_token_alg") or "RS256"
    if alg not in ("RS256", "EdDSA"):
        raise ValueError("id_token_alg must be RS256 or EdDSA")
    subject_type = c.get("subject_type") or "pairwise"
    if subject_type not in ("pairwise", "public"):
        raise ValueError("subject_type must be pairwise or public")
    resource = c.get("resource")
    if resource is not None and (not isinstance(resource, str) or not resource or len(resource) > 500
                                 or _CTRL.search(resource)):
        raise ValueError("resource must be a short identifier")
    name = str(c.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name is required (120 characters at most)")
    app_key = str(c.get("app_key") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", app_key):
        raise ValueError("app_key must be a lowercase word")
    return {**c, "type": ctype, "token_endpoint_auth_method": method, "redirect_uris": uris,
            "post_logout_redirect_uris": logout, "allowed_scopes": scopes, "jwks": jwks,
            "id_token_alg": alg, "subject_type": subject_type, "resource": resource, "name": name,
            "app_key": app_key, "require_mfa": bool(c.get("require_mfa")),
            "first_party": bool(c.get("first_party"))}


# ---- registry --------------------------------------------------------------------------------

def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["redirect_uris"] = list(d.get("redirect_uris") or [])
    d["post_logout_redirect_uris"] = list(d.get("post_logout_redirect_uris") or [])
    d["allowed_scopes"] = list(d.get("allowed_scopes") or [])
    return d


def load(db: Any, *, org_id: str, client_id: Any, any_status: bool = False) -> Optional[dict[str, Any]]:
    if not isinstance(client_id, str) or not client_id or len(client_id) > 100:
        return None
    r = db.execute(text(f"SELECT {COLUMNS} FROM oidc_clients WHERE id = :id AND org_id = :o"),
                   {"id": client_id, "o": org_id}).mappings().first()
    if r is None or (not any_status and r["status"] != "active"):
        return None
    return _row(r)


def public_view(c: Mapping[str, Any]) -> dict[str, Any]:
    """What the admin API returns: never the secret, only whether one is bound."""
    keys = ("id", "project_id", "app_key", "pool_id", "name", "type", "redirect_uris",
            "post_logout_redirect_uris", "allowed_scopes", "token_endpoint_auth_method", "jwks",
            "id_token_alg", "subject_type", "require_mfa", "first_party", "resource", "status",
            "created_by")
    out = {k: c.get(k) for k in keys}
    out["client_id"] = c["id"]
    out["has_secret"] = bool(c.get("secret_vault_item_id"))
    for k in ("created_at", "rotated_at"):
        out[k] = c[k].isoformat() if c.get(k) else None
    return out


def list_clients(db: Any, *, org_id: str, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    sql = f"SELECT {COLUMNS} FROM oidc_clients WHERE org_id = :o AND status <> 'deleted'"
    if project_id:
        sql += " AND project_id = :p"
    return [_row(r) for r in db.execute(text(sql + " ORDER BY created_at"), {"o": org_id, "p": project_id}).mappings()]


def _store_secret(db: Any, org_id: str, client_id: str) -> tuple[str, str]:
    import secrets as _secrets

    from ..identity import vault

    secret = b64u(_secrets.token_bytes(32))
    item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET,
                       provider=VAULT_PROVIDER, secret=secret, db=db, connection_name=client_id)
    return secret, item


def create(db: Any, *, org_id: str, actor: str, body: Mapping[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """Register a client. Returns (client, secret): the secret exists in this return value once."""
    from . import keys

    c = validate(body)
    pool = db.execute(text("SELECT id FROM oidc_user_pools WHERE id = :p AND org_id = :o"),
                      {"p": c.get("pool_id"), "o": org_id}).first()
    if pool is None:
        raise LookupError("pool_not_found")
    client_id = new_id("boc")
    secret = item = None
    if c["token_endpoint_auth_method"] == "client_secret_basic":
        secret, item = _store_secret(db, org_id, client_id)
    db.execute(text(
        "INSERT INTO oidc_clients (id, org_id, project_id, app_key, pool_id, name, type, redirect_uris, "
        "post_logout_redirect_uris, allowed_scopes, token_endpoint_auth_method, jwks, secret_vault_item_id, "
        "id_token_alg, subject_type, require_mfa, first_party, resource, status, created_by) VALUES "
        "(:id, :o, :p, :ak, :pool, :n, :t, :ru, :plr, :sc, :m, CAST(:jwks AS JSONB), :item, :alg, :st, :mfa, "
        ":fp, :res, 'active', :by)"),
        {"id": client_id, "o": org_id, "p": c["project_id"], "ak": c["app_key"], "pool": c["pool_id"],
         "n": c["name"], "t": c["type"], "ru": c["redirect_uris"], "plr": c["post_logout_redirect_uris"],
         "sc": c["allowed_scopes"], "m": c["token_endpoint_auth_method"],
         "jwks": json.dumps(c["jwks"]) if c["jwks"] else None, "item": item, "alg": c["id_token_alg"],
         "st": c["subject_type"], "mfa": c["require_mfa"], "fp": c["first_party"], "res": c["resource"],
         "by": actor})
    keys.provision(db, org_id=org_id, actor=actor)
    audit(db, org_id=org_id, event_type="oidc.client.created", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_client", target_id=client_id,
          details={"project_id": c["project_id"], "app_key": c["app_key"], "type": c["type"],
                   "auth_method": c["token_endpoint_auth_method"], "first_party": c["first_party"]})
    return load(db, org_id=org_id, client_id=client_id), secret


UPDATABLE = ("name", "redirect_uris", "post_logout_redirect_uris", "allowed_scopes", "jwks", "id_token_alg",
             "subject_type", "require_mfa", "first_party", "resource", "status")


def update(db: Any, *, org_id: str, actor: str, client_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    current = load(db, org_id=org_id, client_id=client_id, any_status=True)
    if current is None or current["status"] == "deleted":
        raise LookupError("not_found")
    changes = {k: v for k, v in body.items() if k in UPDATABLE and v is not None}
    status = changes.pop("status", current["status"])
    if status not in ("active", "disabled"):
        raise ValueError("status must be active or disabled")
    c = validate(changes, existing=current)
    db.execute(text(
        "UPDATE oidc_clients SET name = :n, redirect_uris = :ru, post_logout_redirect_uris = :plr, "
        "allowed_scopes = :sc, jwks = CAST(:jwks AS JSONB), id_token_alg = :alg, subject_type = :st, "
        "require_mfa = :mfa, first_party = :fp, resource = :res, status = :status WHERE id = :id"),
        {"id": client_id, "n": c["name"], "ru": c["redirect_uris"], "plr": c["post_logout_redirect_uris"],
         "sc": c["allowed_scopes"], "jwks": json.dumps(c["jwks"]) if c["jwks"] else None,
         "alg": c["id_token_alg"], "st": c["subject_type"], "mfa": c["require_mfa"], "fp": c["first_party"],
         "res": c["resource"], "status": status})
    if status != "active" or c["subject_type"] != current["subject_type"]:
        from .tokens import revoke_client_grants

        revoke_client_grants(db, client_id=client_id, reason="client_changed")
    audit(db, org_id=org_id, event_type="oidc.client.updated", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_client", target_id=client_id, details={"fields": sorted(set(changes) | {"status"})})
    return load(db, org_id=org_id, client_id=client_id, any_status=True)


def delete(db: Any, *, org_id: str, actor: str, client_id: str) -> None:
    from .tokens import revoke_client_grants

    current = load(db, org_id=org_id, client_id=client_id, any_status=True)
    if current is None or current["status"] == "deleted":
        raise LookupError("not_found")
    db.execute(text("UPDATE oidc_clients SET status = 'deleted' WHERE id = :id"), {"id": client_id})
    revoke_client_grants(db, client_id=client_id, reason="client_deleted")
    audit(db, org_id=org_id, event_type="oidc.client.deleted", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_client", target_id=client_id)


def rotate_secret(db: Any, *, org_id: str, actor: str, client_id: str) -> tuple[dict[str, Any], str]:
    current = load(db, org_id=org_id, client_id=client_id, any_status=True)
    if current is None or current["status"] == "deleted":
        raise LookupError("not_found")
    if current["token_endpoint_auth_method"] != "client_secret_basic":
        raise ValueError("only client_secret_basic clients have a secret")
    secret, item = _store_secret(db, org_id, client_id)
    old = current.get("secret_vault_item_id")
    db.execute(text("UPDATE oidc_clients SET secret_vault_item_id = :i, rotated_at = now() WHERE id = :id"),
               {"i": item, "id": client_id})
    if old:
        # The old secret stops working now: its vault item is revoked (identity's own material,
        # no provider to call).
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE id = :i AND kind = 'oidc_client_secret'"),
                   {"i": old})
    audit(db, org_id=org_id, event_type="oidc.client.secret_rotated", actor_principal_id=actor,
          actor_kind="user", target_type="oidc_client", target_id=client_id)
    return load(db, org_id=org_id, client_id=client_id, any_status=True), secret


# ---- client authentication -------------------------------------------------------------------

def _basic(headers: Mapping[str, str]) -> Optional[tuple[str, str]]:
    value = headers.get("authorization") or ""
    if not value[:6].lower() == "basic ":
        return None
    try:
        raw = base64.b64decode(value[6:].strip(), validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001
        raise OidcError("invalid_client", "malformed basic credentials", 401) from None
    cid, sep, secret = raw.partition(":")
    if not sep:
        raise OidcError("invalid_client", "malformed basic credentials", 401)
    return unquote_plus(cid), unquote_plus(secret)


def authenticate(db: Any, *, org_id: str, form: Mapping[str, str], headers: Mapping[str, str],
                 audiences: tuple[str, ...], ip: Optional[str] = None) -> dict[str, Any]:
    """The client making a token or revocation request, authenticated by its registered method.
    Raises OidcError(invalid_client, 401)."""
    from ..identity import ratelimit

    basic = _basic(headers)
    assertion = form.get("client_assertion")
    presented = [m for m, on in (("client_secret_basic", basic is not None),
                                 ("private_key_jwt", assertion is not None),
                                 ("client_secret_post", "client_secret" in form)) if on]
    if len(presented) > 1:
        raise OidcError("invalid_request", "use exactly one client authentication method")
    if presented == ["client_secret_post"]:
        raise OidcError("invalid_client", "client_secret_post is not supported", 401)
    form_cid = form.get("client_id")
    cid = basic[0] if basic else form_cid
    if basic and form_cid is not None and form_cid != basic[0]:
        raise OidcError("invalid_client", "client_id mismatch", 401)
    if assertion is not None and not cid:
        cid = _assertion_subject(assertion)
    client = load(db, org_id=org_id, client_id=cid)
    if client is None:
        raise OidcError("invalid_client", "unknown client", 401)
    method = client["token_endpoint_auth_method"]
    used = presented[0] if presented else "none"
    if used != method:
        _failed(db, client, ip, "wrong_method")
        raise OidcError("invalid_client", "client authentication failed", 401)
    if method == "client_secret_basic":
        if ratelimit.peek("oidc.client_auth_failure.client", client["id"], window_seconds=SECRET_FAILURE_WINDOW_S,
                          db=db) >= SECRET_FAILURE_LIMIT:
            raise OidcError("invalid_client", "client authentication is locked; try later", 401)
        if not _secret_ok(db, client, basic[1]):
            _failed(db, client, ip, "bad_secret")
            raise OidcError("invalid_client", "client authentication failed", 401)
    elif method == "private_key_jwt":
        if form.get("client_assertion_type") != ASSERTION_TYPE:
            raise OidcError("invalid_client", "unsupported client_assertion_type", 401)
        try:
            _verify_assertion(db, client, assertion, audiences)
        except OidcError:
            _failed(db, client, ip, "bad_assertion")
            raise
    return client


def _assertion_subject(assertion: str) -> Optional[str]:
    import jwt

    try:
        claims = jwt.decode(assertion, options={"verify_signature": False})
    except Exception:  # noqa: BLE001
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) else None


def _secret_ok(db: Any, client: Mapping[str, Any], presented: str) -> bool:
    from ..identity import vault

    item = client.get("secret_vault_item_id")
    if not item:
        return False
    try:
        stored = vault.read_secret(item, db=db, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET)
    except Exception:  # noqa: BLE001 — a revoked or unreadable secret authenticates nobody
        return False
    return hmac.compare_digest(sha256_hex(presented or ""), sha256_hex(stored))


def _failed(db: Any, client: Mapping[str, Any], ip: Optional[str], why: str) -> None:
    from ..identity import ratelimit

    decision = ratelimit.hit("oidc.client_auth_failure.client", client["id"], limit=SECRET_FAILURE_LIMIT,
                             window_seconds=SECRET_FAILURE_WINDOW_S, db=db)
    audit(db, org_id=client["org_id"], event_type="oidc.client.auth_failed", outcome="failure",
          actor_kind="oidc_client", target_type="oidc_client", target_id=client["id"], ip=ip,
          details={"reason": why})
    count = ratelimit.peek("oidc.client_auth_failure.client", client["id"], window_seconds=SECRET_FAILURE_WINDOW_S, db=db)
    if not decision.allowed or count == SECRET_FAILURE_LIMIT:
        audit(db, org_id=client["org_id"], event_type="oidc.client.locked", outcome="denied",
              actor_kind="oidc_client", target_type="oidc_client", target_id=client["id"], ip=ip,
              details={"window_seconds": SECRET_FAILURE_WINDOW_S})


def _verify_assertion(db: Any, client: Mapping[str, Any], assertion: Optional[str], audiences: tuple[str, ...]) -> None:
    import jwt

    if not assertion or len(assertion) > 8000:
        raise OidcError("invalid_client", "client assertion required", 401)
    try:
        header = jwt.get_unverified_header(assertion)
    except Exception:  # noqa: BLE001
        raise OidcError("invalid_client", "malformed client assertion", 401) from None
    alg = header.get("alg")
    if alg not in ASSERTION_ALGS or "crit" in header:
        raise OidcError("invalid_client", "client assertion algorithm not allowed", 401)
    kid = header.get("kid")
    candidates = [k for k in (client.get("jwks") or {}).get("keys", [])
                  if k.get("kty") == ASSERTION_ALGS[alg] and (kid is None or k.get("kid") == kid)
                  and k.get("alg") in (None, alg)]
    if len(candidates) != 1:
        raise OidcError("invalid_client", "no registered key for this assertion", 401)
    try:
        key = jwt.PyJWK(dict(candidates[0]), algorithm=alg).key
        claims = jwt.decode(assertion, key=key, algorithms=[alg], audience=list(audiences),
                            issuer=client["id"], leeway=int(CLOCK_SKEW.total_seconds()),
                            options={"require": ["exp", "iss", "sub", "aud", "jti"]})
    except Exception:  # noqa: BLE001 — every verification failure is the same answer
        raise OidcError("invalid_client", "client assertion did not verify", 401) from None
    if claims.get("sub") != client["id"]:
        raise OidcError("invalid_client", "client assertion subject mismatch", 401)
    exp = int(claims["exp"])
    current = int(now().timestamp())
    if exp - current > ASSERTION_MAX_LIFETIME_S + int(CLOCK_SKEW.total_seconds()):
        raise OidcError("invalid_client", "client assertion lives too long", 401)
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > 200:
        raise OidcError("invalid_client", "client assertion needs a jti", 401)
    db.execute(text("DELETE FROM oidc_client_assertion_jtis WHERE expires_at < now()"))
    inserted = db.execute(text(
        "INSERT INTO oidc_client_assertion_jtis (jti_hash, client_id, expires_at) VALUES (:h, :c, to_timestamp(:e)) "
        "ON CONFLICT (jti_hash) DO NOTHING"), {"h": sha256_hex(f"{client['id']}\x1f{jti}"), "c": client["id"],
                                               "e": exp + int(CLOCK_SKEW.total_seconds())}).rowcount
    if not inserted:
        raise OidcError("invalid_client", "client assertion was already used", 401)
