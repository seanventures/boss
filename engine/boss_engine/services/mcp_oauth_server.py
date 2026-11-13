"""BOSSAPP-862: OAuth 2.1 and device login for BOSS's OWN MCP server.

Remote MCP clients (Claude.ai, ChatGPT, IDE agents) expect the MCP authorization spec: an HTTP
401 that points at RFC 9728 protected-resource metadata, an RFC 8414 authorization server with
dynamic client registration (RFC 7591), the authorization-code grant with PKCE (S256 only, public
clients), and — for a terminal (`boss login`) — the device authorization grant (RFC 8628). BOSS
had personal / service-account keys and run tokens only.

THE BOSS WAY: a granted token IS a scoped BOSS API credential. The access token is a personal API
key (``identity/api_keys.create``, kind ``pat``) minted for the person who consented, narrowed to
the scopes the client asked for and that person holds, and short-lived; the MCP server already
verifies those (mcp/server.authenticate_transport), so no second token system exists. The
refresh token is BOSS's own: random, stored only as a hash, ROTATED on every use (the old one
dies; presenting a dead one revokes the whole grant — RFC 9700 reuse detection). Revoking a
connected app (``revoke_grant``) revokes its current key and its refresh token.

PKCE reuses the 846 client-side machinery's helper shape (services/mcp_oauth: S256 over a
43-128 character verifier), checked here on the server side.

Scopes (what a client may ask for): ``mcp`` (everything the person holds) and ``mcp:read``
(only the ``:read`` permissions they hold). An unknown scope is refused.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

from sqlalchemy import text

from .core import make_id

SCOPES = ("mcp", "mcp:read")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CODE_TTL = timedelta(minutes=5)
DEVICE_TTL = timedelta(minutes=10)
DEVICE_INTERVAL = 5
ACCESS_TTL_DAYS = 1
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"          # no vowels: no words, no 0/O 1/I confusion


class OAuthError(Exception):
    """An RFC 6749 error: `error` is the code, `description` the sentence, `status` the HTTP one."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status

    def body(self) -> dict[str, str]:
        return {"error": self.error, **({"error_description": self.description}
                                        if self.description else {})}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# ── metadata ───────────────────────────────────────────────────────────────────────────────

def endpoints(issuer: str) -> dict[str, str]:
    base = issuer.rstrip("/")
    return {"authorization_endpoint": f"{base}/v1/mcp-oauth/authorize",
            "token_endpoint": f"{base}/v1/mcp-oauth/token",
            "registration_endpoint": f"{base}/v1/mcp-oauth/register",
            "device_authorization_endpoint": f"{base}/v1/mcp-oauth/device_authorization",
            "revocation_endpoint": f"{base}/v1/mcp-oauth/revoke"}


def authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """RFC 8414."""
    return {"issuer": issuer.rstrip("/"), **endpoints(issuer),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_GRANT],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES)}


def protected_resource_metadata(resource: str, issuer: str) -> dict[str, Any]:
    """RFC 9728: what the MCP server (the resource) points a 401'd client at."""
    return {"resource": resource, "authorization_servers": [issuer.rstrip("/")],
            "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
            "resource_name": "BOSS"}


# ── dynamic client registration (RFC 7591) ─────────────────────────────────────────────────

def _redirect_ok(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment:
        return False
    if parts.scheme == "https" and parts.netloc:
        return True
    # A native or CLI client's loopback redirect (RFC 8252 §7.3).
    return parts.scheme == "http" and (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")


def register_client(connection: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    uris = metadata.get("redirect_uris") or []
    grants = metadata.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        raise OAuthError("invalid_redirect_uri", "redirect_uris is a list of URLs")
    if "authorization_code" in grants and not uris:
        raise OAuthError("invalid_redirect_uri", "an authorization-code client needs a redirect_uri")
    bad = [u for u in uris if not _redirect_ok(u)]
    if bad:
        raise OAuthError("invalid_redirect_uri",
                         f"{bad[0]} is not https (or a loopback http URL for a native client)")
    unknown = [g for g in grants if g not in ("authorization_code", "refresh_token", DEVICE_GRANT)]
    if unknown:
        raise OAuthError("invalid_client_metadata", f"grant type {unknown[0]} is not supported")
    if str(metadata.get("token_endpoint_auth_method") or "none") != "none":
        raise OAuthError("invalid_client_metadata", "BOSS registers public clients (PKCE, no secret)")
    name = " ".join(str(metadata.get("client_name") or "MCP client").split())[:120]
    client_id = "mcpc_" + secrets.token_urlsafe(18)
    connection.execute(text(
        "INSERT INTO mcp_oauth_clients (client_id, client_name, redirect_uris, grant_types) "
        "VALUES (:c, :n, CAST(:r AS jsonb), CAST(:g AS jsonb))"),
        {"c": client_id, "n": name, "r": json.dumps(uris), "g": json.dumps(grants)})
    return {"client_id": client_id, "client_name": name, "redirect_uris": uris,
            "grant_types": grants, "token_endpoint_auth_method": "none",
            "client_id_issued_at": int(_now().timestamp())}


def _client(connection: Any, client_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT client_id, client_name, redirect_uris, grant_types, status FROM mcp_oauth_clients "
        "WHERE client_id = :c"), {"c": str(client_id or "")}).mappings().first()
    if not row or row["status"] != "active":
        raise OAuthError("invalid_client", "unknown client", 401)
    return dict(row)


def _scopes(requested: Optional[str]) -> list[str]:
    wanted = sorted(set(str(requested or "mcp").split()))
    unknown = [s for s in wanted if s not in SCOPES]
    if unknown:
        raise OAuthError("invalid_scope", f"unknown scope {unknown[0]}")
    return wanted


# ── authorization code + PKCE ──────────────────────────────────────────────────────────────

def check_authorize_request(connection: Any, *, client_id: str, redirect_uri: str,
                            code_challenge: str, code_challenge_method: str,
                            response_type: str = "code", scope: Optional[str] = None) -> dict:
    """Validate an authorization request before a person is asked (never redirect to an
    unregistered URI). Returns the client and the scopes."""
    client = _client(connection, client_id)
    if redirect_uri not in (client["redirect_uris"] or []):
        raise OAuthError("invalid_request", "redirect_uri is not registered for this client")
    if response_type != "code":
        raise OAuthError("unsupported_response_type", "only code")
    if code_challenge_method != "S256" or not (43 <= len(code_challenge or "") <= 128):
        raise OAuthError("invalid_request", "PKCE with S256 is required")
    return {"client": client, "scopes": _scopes(scope)}


def authorize(connection: Any, *, principal_id: str, org_id: str, client_id: str,
              redirect_uri: str, code_challenge: str, code_challenge_method: str = "S256",
              scope: Optional[str] = None, state: Optional[str] = None, approve: bool = True
              ) -> str:
    """The signed-in person's answer to the consent screen: the URL to send the browser to."""
    checked = check_authorize_request(connection, client_id=client_id, redirect_uri=redirect_uri,
                                      code_challenge=code_challenge,
                                      code_challenge_method=code_challenge_method, scope=scope)
    sep = "&" if "?" in redirect_uri else "?"
    if not approve:
        return redirect_uri + sep + urlencode({"error": "access_denied",
                                                **({"state": state} if state else {})})
    code = secrets.token_urlsafe(32)
    connection.execute(text(
        "INSERT INTO mcp_oauth_codes (code_hash, client_id, principal_id, org_id, redirect_uri, "
        " code_challenge, scopes, expires_at) VALUES (:h, :c, :p, :o, :r, :ch, :s, :exp)"),
        {"h": _hash(code), "c": client_id, "p": principal_id, "o": org_id, "r": redirect_uri,
         "ch": code_challenge, "s": checked["scopes"], "exp": _now() + CODE_TTL})
    return redirect_uri + sep + urlencode({"code": code, **({"state": state} if state else {})})


# ── tokens: a scoped BOSS API key + a rotating refresh token ───────────────────────────────

def _permissions_for(connection: Any, principal: Any, scopes: list[str]) -> list[str]:
    from ..identity import api_keys

    held = sorted(api_keys.held_permissions(connection, principal))
    if "mcp" in scopes:
        return held
    return [p for p in held if p.endswith(":read")]


def _principal(connection: Any, principal_id: str) -> Any:
    from ..identity.principal import Principal, PrincipalKind, PrincipalStatus

    row = connection.execute(text(
        "SELECT id, kind, org_id, status, display_name FROM identity_principals WHERE id = :p"),
        {"p": principal_id}).mappings().first()
    if not row or row["status"] != "active" or row["kind"] != "user":
        raise OAuthError("invalid_grant", "the person who consented is no longer active")
    return Principal(row["id"], PrincipalKind(row["kind"]), row["org_id"],
                     PrincipalStatus(row["status"]), row["display_name"])


def _mint(connection: Any, *, grant: dict[str, Any], client_name: str) -> dict[str, Any]:
    from ..identity import api_keys
    from ..identity.principal import AuthContext, AuthMethod

    owner = _principal(connection, grant["principal_id"])
    perms = _permissions_for(connection, owner, list(grant["scopes"]))
    if not perms:
        raise OAuthError("invalid_scope", "the person holds nothing in the scope asked for")
    created = api_keys.create(connection, creator=AuthContext(owner, AuthMethod.SESSION),
                              owner=owner, kind="pat", name=f"MCP: {client_name}"[:120],
                              scopes=perms, expires_in_days=ACCESS_TTL_DAYS)
    refresh = secrets.token_urlsafe(40)
    old_key = grant.get("api_key_id")
    connection.execute(text(
        "UPDATE mcp_oauth_grants SET api_key_id = :k, refresh_hash = :r, refreshed_at = now() "
        "WHERE id = :g"), {"k": created.id, "r": _hash(refresh), "g": grant["id"]})
    if old_key:
        api_keys.revoke(connection, actor=AuthContext(owner, AuthMethod.SESSION), key_id=old_key,
                        org_id=owner.org_id)
    expires_in = int((created.expires_at - _now()).total_seconds()) if created.expires_at else 86400
    return {"access_token": created.key, "token_type": "Bearer", "expires_in": max(1, expires_in),
            "refresh_token": refresh, "scope": " ".join(grant["scopes"])}


def _new_grant(connection: Any, *, client_id: str, principal_id: str, org_id: str,
               scopes: list[str]) -> dict[str, Any]:
    grant = {"id": make_id("mcpg"), "client_id": client_id, "principal_id": principal_id,
             "org_id": org_id, "scopes": scopes, "api_key_id": None}
    connection.execute(text(
        "INSERT INTO mcp_oauth_grants (id, client_id, principal_id, org_id, scopes) "
        "VALUES (:id, :c, :p, :o, :s)"),
        {"id": grant["id"], "c": client_id, "p": principal_id, "o": org_id, "s": scopes})
    return grant


def exchange_code(connection: Any, *, client_id: str, code: str, redirect_uri: str,
                  code_verifier: str) -> dict[str, Any]:
    client = _client(connection, client_id)
    row = connection.execute(text(
        "SELECT * FROM mcp_oauth_codes WHERE code_hash = :h FOR UPDATE"),
        {"h": _hash(code or "")}).mappings().first()
    if not row or row["client_id"] != client_id or row["used_at"] is not None \
            or row["expires_at"] <= _now():
        raise OAuthError("invalid_grant", "the code is unknown, used or expired")
    connection.execute(text("UPDATE mcp_oauth_codes SET used_at = now() WHERE code_hash = :h"),
                       {"h": row["code_hash"]})
    if row["redirect_uri"] != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
    if not (43 <= len(code_verifier or "") <= 128) or not hmac.compare_digest(
            s256(code_verifier), row["code_challenge"]):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    grant = _new_grant(connection, client_id=client_id, principal_id=row["principal_id"],
                       org_id=row["org_id"], scopes=list(row["scopes"]))
    return _mint(connection, grant=grant, client_name=client["client_name"])


def refresh(connection: Any, *, client_id: str, refresh_token: str) -> dict[str, Any]:
    """Rotate: a new key and refresh token; the old ones die. A dead refresh token presented
    again (a stolen copy, or a replay) revokes the whole grant."""
    client = _client(connection, client_id)
    h = _hash(refresh_token or "")
    grant = connection.execute(text(
        "SELECT * FROM mcp_oauth_grants WHERE refresh_hash = :h FOR UPDATE"), {"h": h}).mappings().first()
    if grant is None:
        reused = connection.execute(text(
            "SELECT id FROM mcp_oauth_grants WHERE :h = ANY(spent_refresh_hashes) AND revoked_at IS NULL"),
            {"h": h}).scalar()
        if reused:
            revoke_grant(connection, reused, reason="refresh token reused")
        raise OAuthError("invalid_grant", "the refresh token is unknown or was already used")
    if grant["client_id"] != client_id or grant["revoked_at"] is not None:
        raise OAuthError("invalid_grant", "the grant was revoked")
    connection.execute(text(
        "UPDATE mcp_oauth_grants SET spent_refresh_hashes = array_append(spent_refresh_hashes, :h) "
        "WHERE id = :g"), {"h": h, "g": grant["id"]})
    return _mint(connection, grant=dict(grant), client_name=client["client_name"])


# ── device authorization grant (RFC 8628) ──────────────────────────────────────────────────

def _user_code() -> str:
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def device_authorize(connection: Any, *, client_id: str, scope: Optional[str],
                     verification_uri: str) -> dict[str, Any]:
    client = _client(connection, client_id)
    if DEVICE_GRANT not in (client["grant_types"] or []):
        raise OAuthError("unauthorized_client", "this client did not register the device grant")
    scopes = _scopes(scope)
    device_code = secrets.token_urlsafe(32)
    user_code = _user_code()
    connection.execute(text(
        "INSERT INTO mcp_oauth_device_codes (device_code_hash, user_code, client_id, scopes, "
        " expires_at, interval_seconds) VALUES (:h, :u, :c, :s, :exp, :i)"),
        {"h": _hash(device_code), "u": user_code, "c": client_id, "s": scopes,
         "exp": _now() + DEVICE_TTL, "i": DEVICE_INTERVAL})
    return {"device_code": device_code, "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": f"{verification_uri}?user_code={user_code}",
            "expires_in": int(DEVICE_TTL.total_seconds()), "interval": DEVICE_INTERVAL}


def device_decide(connection: Any, *, user_code: str, principal_id: str, org_id: str,
                  approve: bool) -> dict[str, Any]:
    """The signed-in person typed the code BOSS showed the terminal, and approved or denied."""
    code = str(user_code or "").strip().upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    row = connection.execute(text(
        "SELECT * FROM mcp_oauth_device_codes WHERE user_code = :u FOR UPDATE"),
        {"u": code}).mappings().first()
    if not row or row["status"] != "pending" or row["expires_at"] <= _now():
        raise OAuthError("invalid_request", "That code is unknown, used or expired.")
    connection.execute(text(
        "UPDATE mcp_oauth_device_codes SET status = :st, principal_id = :p, org_id = :o "
        "WHERE device_code_hash = :h"),
        {"st": "approved" if approve else "denied", "p": principal_id, "o": org_id,
         "h": row["device_code_hash"]})
    return {"user_code": code, "status": "approved" if approve else "denied",
            "client_id": row["client_id"]}


def device_token(connection: Any, *, client_id: str, device_code: str) -> dict[str, Any]:
    """A terminal polling for its token (RFC 8628 §3.4-3.5)."""
    client = _client(connection, client_id)
    row = connection.execute(text(
        "SELECT * FROM mcp_oauth_device_codes WHERE device_code_hash = :h FOR UPDATE"),
        {"h": _hash(device_code or "")}).mappings().first()
    if not row or row["client_id"] != client_id:
        raise OAuthError("invalid_grant", "unknown device code")
    if row["expires_at"] <= _now():
        raise OAuthError("expired_token", "the code expired; start again")
    last = row["last_polled_at"]
    connection.execute(text("UPDATE mcp_oauth_device_codes SET last_polled_at = now() "
                            "WHERE device_code_hash = :h"), {"h": row["device_code_hash"]})
    if row["status"] == "pending":
        if last is not None and (_now() - last).total_seconds() < row["interval_seconds"]:
            raise OAuthError("slow_down", "poll less often")
        raise OAuthError("authorization_pending", "waiting for the person to approve")
    if row["status"] == "denied":
        raise OAuthError("access_denied", "the person said no")
    if row["status"] != "approved":
        raise OAuthError("invalid_grant", "this code was already used")
    connection.execute(text("UPDATE mcp_oauth_device_codes SET status = 'used' "
                            "WHERE device_code_hash = :h"), {"h": row["device_code_hash"]})
    grant = _new_grant(connection, client_id=client_id, principal_id=row["principal_id"],
                       org_id=row["org_id"], scopes=list(row["scopes"]))
    return _mint(connection, grant=grant, client_name=client["client_name"])


# ── connected apps ─────────────────────────────────────────────────────────────────────────

def connected_apps(connection: Any, principal_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT g.id, g.client_id, c.client_name, g.scopes, g.created_at, g.refreshed_at "
        "FROM mcp_oauth_grants g JOIN mcp_oauth_clients c ON c.client_id = g.client_id "
        "WHERE g.principal_id = :p AND g.revoked_at IS NULL ORDER BY g.created_at DESC"),
        {"p": principal_id}).mappings().all()
    return [{"id": r["id"], "client_id": r["client_id"], "client_name": r["client_name"],
             "scopes": list(r["scopes"] or []),
             "connected_at": r["created_at"].isoformat() if r["created_at"] else None,
             "last_refreshed_at": r["refreshed_at"].isoformat() if r["refreshed_at"] else None}
            for r in rows]


def revoke_grant(connection: Any, grant_id: str, *, principal_id: Optional[str] = None,
                 reason: str = "revoked") -> bool:
    """End a connected app: its current key and its refresh token stop working now."""
    from ..identity import api_keys
    from ..identity.principal import AuthContext, AuthMethod

    params: dict[str, Any] = {"g": grant_id}
    owner_clause = ""
    if principal_id:
        owner_clause, params["p"] = " AND principal_id = :p", principal_id
    grant = connection.execute(text(
        "UPDATE mcp_oauth_grants SET revoked_at = now(), revoked_reason = :why, refresh_hash = NULL "
        "WHERE id = :g AND revoked_at IS NULL" + owner_clause + " RETURNING principal_id, api_key_id"),
        {**params, "why": reason[:200]}).mappings().first()
    if not grant:
        return False
    if grant["api_key_id"]:
        try:
            owner = _principal(connection, grant["principal_id"])
            api_keys.revoke(connection, actor=AuthContext(owner, AuthMethod.SESSION),
                            key_id=grant["api_key_id"], org_id=owner.org_id)
        except OAuthError:
            connection.execute(text("UPDATE identity_api_credentials SET revoked_at = now() "
                                    "WHERE id = :k AND revoked_at IS NULL"), {"k": grant["api_key_id"]})
    return True


def revoke_token(connection: Any, *, client_id: str, token: str) -> None:
    """RFC 7009: a refresh token (or an access key this client holds) presented for revocation
    ends its grant. Unknown tokens are not an error."""
    h = _hash(token or "")
    grant_id = connection.execute(text(
        "SELECT id FROM mcp_oauth_grants WHERE client_id = :c AND revoked_at IS NULL AND "
        "(refresh_hash = :h OR api_key_id IN (SELECT id FROM identity_api_credentials "
        " WHERE token_hash = :th))"),
        {"c": client_id, "h": h, "th": _key_hash(token)}).scalar()
    if grant_id:
        revoke_grant(connection, grant_id, reason="revoked by the client")


def _key_hash(token: str) -> str:
    try:
        from ..identity.authn import hash_token

        return hash_token(token or "")
    except Exception:  # noqa: BLE001
        return ""
