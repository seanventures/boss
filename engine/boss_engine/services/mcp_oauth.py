"""Sign in to a remote MCP server the way the MCP authorization spec says (BOSSAPP-846).

WHY THIS EXISTS
---------------
BOSS could sign in to an MCP server only with a token an operator pasted (bearer / header). The
hosted Linear, Atlassian, Notion and Sentry servers take no pasted token: they speak OAuth 2.1 as
the MCP spec (2025-06-18) describes it, and OAuth was hard-coded twice in BOSS (Google/YouTube in
channel_oauth, GitHub in identity/github_device) with nothing general for a server.

THE FLOW (one door per step, network never inside a transaction)
----------------------------------------------------------------
1. DISCOVER (:func:`discover`). Ask the server with no token; its 401 names the protected-resource
   metadata (RFC 9728, `WWW-Authenticate: Bearer resource_metadata="…"`), else the well-known
   path is tried. That names the authorization server, whose metadata (RFC 8414, then OpenID
   discovery) gives the endpoints. A server with no resource metadata (the 2025-03-26 spec) is its
   own authorization server.
2. REGISTER (:func:`prepare`). Dynamic client registration (RFC 7591) when the server offers it;
   otherwise the client id (and secret) an operator typed in the console; otherwise
   :class:`NeedsClient`, which the console turns into those two fields.
3. AUTHORIZE (:func:`start`). PKCE S256: the verifier and the HASH of `state` are kept here
   (mcp_oauth_pending, ten minutes, single use); the browser goes to the provider with the
   challenge and comes back to BOSS's callback route.
4. EXCHANGE (:func:`complete`). The callback spends the pending row, trades the code (with the
   verifier and the RFC 8707 resource) for tokens, and stores them as an identity VAULT item —
   never in mcp_servers.auth — for the subject the sign-in was for (the org's shared sign-in, a
   person's own, or an agent's own: connection_credentials).
5. USE AND REFRESH (:func:`refresher`). connection_credentials reads the token through
   vault.connection_token, which refreshes single-flight (SELECT … FOR UPDATE in the vault's own
   transaction) when less than five minutes are left. A refresh the provider refuses marks the
   credential (and, for the shared one, the server) `needs_reauth`; the console offers Reconnect.

THE SSRF GUARD (:func:`guard_url`). Every URL BOSS learns from somebody else's metadata —
resource metadata, authorization server, every endpoint — is checked before it is fetched or
shown to a browser: https only, and a host that resolves to a private, loopback, link-local,
CGNAT or metadata address is refused unless the install opted that host in
(BOSS_OUTBOUND_ALLOW_PRIVATE). Fetches go through identity/http.fetch, the SSRF-safe client the
identity provider discovery uses, which pins the resolved address and refuses cross-host
redirects.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event

#: Names the caller of every outbound fetch (identity/http audits private-host exceptions by it).
PURPOSE = "mcp.oauth"
#: How long a sign-in may take between leaving for the provider and coming back.
PENDING_TTL_SECONDS = 600
#: The callback route (apps/api routers/mcp_credentials.py) the provider sends the browser to.
CALLBACK_PATH = "/v1/mcp/oauth/callback"
#: What BOSS calls itself when it registers a client.
CLIENT_NAME = "BOSS"
#: The protocol BOSS speaks on the unauthenticated discovery probe (mcp_client's first choice).
PROBE_PROTOCOL_VERSION = "2025-06-18"

Begin = Callable[[], ContextManager[Connection]]


class McpOAuthError(Exception):
    """A sign-in that cannot go on, said for the person who pressed Sign in. `code` is stable."""

    status = 422

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NeedsClient(McpOAuthError):
    """The authorization server registers no clients itself: type a client id (and secret)."""

    status = 409


class UnsafeDestination(McpOAuthError):
    """A discovered URL is not https, or points somewhere private, and the install did not opt in."""


# --------------------------------------------------------------------------- #
# The SSRF guard                                                                #
# --------------------------------------------------------------------------- #

def guard_url(url: str, *, what: str) -> str:
    """The URL, or UnsafeDestination. `what` names it in the refusal ("token endpoint")."""
    import ipaddress

    from ..identity import http as safe_http

    raw = str(url or "").strip()
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or scheme not in ("http", "https"):
        raise UnsafeDestination("bad_url", f"The {what} is not a web address BOSS can use.")
    if parts.username or parts.password:
        raise UnsafeDestination("credentials_in_url", f"The {what} carries a user name or password.")
    allowed_private = host in safe_http._allowed_private_hosts()
    if scheme != "https" and not allowed_private:
        raise UnsafeDestination("https_required", f"The {what} must use https ({raw[:120]}).")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = safe_http._resolve(host, parts.port or (443 if scheme == "https" else 80))
        except OSError:
            raise UnsafeDestination("unresolvable", f"The {what}'s host {host} does not resolve.") \
                from None
    if not addresses:
        raise UnsafeDestination("unresolvable", f"The {what}'s host {host} has no address.")
    if not allowed_private and any(not safe_http._is_public(a) for a in addresses):
        raise UnsafeDestination(
            "private_destination",
            f"The {what} ({host}) is a private, loopback or link-local address. BOSS refuses it "
            "unless the install allows that host (BOSS_OUTBOUND_ALLOW_PRIVATE).")
    return raw


def _fetch(method: str, url: str, *, what: str, headers: Optional[dict[str, str]] = None,
           data: Optional[bytes] = None) -> Any:
    from ..identity import http as safe_http

    guard_url(url, what=what)
    try:
        return safe_http.fetch(method, url, headers=headers or {}, data=data, purpose=PURPOSE)
    except safe_http.OutboundRefused as refused:
        raise McpOAuthError("unreachable", f"BOSS could not reach the {what}: {refused}.") from None


def _json(response: Any, *, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise McpOAuthError("bad_metadata", f"The {what} did not answer JSON.") from None
    if not isinstance(body, dict):
        raise McpOAuthError("bad_metadata", f"The {what} did not answer a JSON object.")
    return body


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #

def _auth_params(header: str) -> dict[str, str]:
    """The parameters of a `WWW-Authenticate: Bearer k="v", k2=v2` challenge."""
    out: dict[str, str] = {}
    _, _, rest = str(header or "").partition(" ")
    for part in _split_params(rest):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip().strip('"')
    return out


def _split_params(text_: str) -> list[str]:
    parts, buf, quoted = [], "", False
    for ch in text_:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _well_known(url: str, suffix: str) -> list[str]:
    """RFC 8414 §3 / RFC 9728 §3: the well-known segment goes between the host and the path;
    the root form is tried after it."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    found = []
    if path:
        found.append(urlunsplit((parts.scheme, parts.netloc, f"/.well-known/{suffix}{path}", "", "")))
    found.append(urlunsplit((parts.scheme, parts.netloc, f"/.well-known/{suffix}", "", "")))
    return found


def _probe_challenge(server_url: str) -> dict[str, str]:
    """POST initialize with no token; a 401's challenge parameters (empty when it did not ask)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": PROBE_PROTOCOL_VERSION, "capabilities": {},
                                  "clientInfo": {"name": "boss", "version": "1.0.0"}}}).encode()
    resp = _fetch("POST", server_url, what="MCP server", data=body,
                  headers={"Content-Type": "application/json",
                           "Accept": "application/json, text/event-stream",
                           "MCP-Protocol-Version": PROBE_PROTOCOL_VERSION})
    if resp.status != 401:
        return {}
    return _auth_params(resp.headers.get("www-authenticate", ""))


def _resource_metadata(server_url: str, hinted: Optional[str]) -> Optional[dict[str, Any]]:
    for candidate in ([hinted] if hinted else []) + _well_known(server_url,
                                                                "oauth-protected-resource"):
        try:
            resp = _fetch("GET", candidate, what="protected-resource metadata",
                          headers={"Accept": "application/json"})
        except UnsafeDestination:
            raise
        except McpOAuthError:
            if hinted and candidate == hinted:
                raise
            continue
        if resp.status == 200:
            body = _json(resp, what="protected-resource metadata")
            if body.get("authorization_servers"):
                return body
        if hinted and candidate == hinted:
            raise McpOAuthError("bad_metadata",
                                f"The server's resource metadata ({hinted}) answered {resp.status}.")
    return None


def _authorization_server_metadata(issuer: str) -> dict[str, Any]:
    candidates = _well_known(issuer, "oauth-authorization-server") \
        + _well_known(issuer, "openid-configuration")
    if urlsplit(issuer).path.strip("/"):
        candidates.append(issuer.rstrip("/") + "/.well-known/openid-configuration")
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            resp = _fetch("GET", candidate, what="authorization server metadata",
                          headers={"Accept": "application/json"})
        except UnsafeDestination:
            raise
        except McpOAuthError:
            continue
        if resp.status == 200:
            return _json(resp, what="authorization server metadata")
    raise McpOAuthError("no_authorization_server",
                        f"The authorization server {issuer} publishes no OAuth metadata.")


def discover(server_url: str) -> dict[str, Any]:
    """Everything a sign-in needs to know about one MCP server, checked and SSRF-guarded.

    {resource, authorization_server, authorization_endpoint, token_endpoint,
     registration_endpoint?, scope?, token_endpoint_auth_methods, discovered_at}"""
    server_url = guard_url(server_url, what="MCP server")
    challenge = _probe_challenge(server_url)
    hinted = challenge.get("resource_metadata")
    if hinted:
        hinted = guard_url(hinted, what="protected-resource metadata")
    prm = _resource_metadata(server_url, hinted)
    if prm is not None:
        resource = str(prm.get("resource") or server_url)
        # RFC 9728 §3.3: the metadata must be about THIS server. One that names another origin
        # would have BOSS mint a token for somebody else's resource.
        if _origin(resource).lower() != _origin(server_url).lower():
            raise McpOAuthError("resource_mismatch",
                                f"The server's metadata is about {resource}, not {server_url}.")
        issuer = str((prm.get("authorization_servers") or [""])[0])
        scopes = prm.get("scopes_supported")
    else:
        # The 2025-03-26 spec: a server with no resource metadata is its own authorization server.
        resource, issuer, scopes = server_url, _origin(server_url), None
    issuer = guard_url(issuer, what="authorization server")
    meta = _authorization_server_metadata(issuer)
    # RFC 8414 §3.3: the issuer it states is the one we asked about (a mismatch is an attack).
    if str(meta.get("issuer") or "").rstrip("/") != issuer.rstrip("/"):
        raise McpOAuthError("issuer_mismatch",
                            f"The authorization server says it is {meta.get('issuer')!r}, "
                            f"not {issuer}.")
    methods = meta.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise McpOAuthError("no_pkce", "The authorization server does not support PKCE S256, "
                                       "which BOSS requires.")
    for field in ("authorization_endpoint", "token_endpoint"):
        if not meta.get(field):
            raise McpOAuthError("bad_metadata", f"The authorization server names no {field}.")
    out = {
        "resource": resource,
        "authorization_server": issuer,
        "authorization_endpoint": guard_url(meta["authorization_endpoint"],
                                            what="authorization endpoint"),
        "token_endpoint": guard_url(meta["token_endpoint"], what="token endpoint"),
        "token_endpoint_auth_methods": list(meta.get("token_endpoint_auth_methods_supported")
                                            or ["client_secret_basic"]),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    if meta.get("registration_endpoint"):
        out["registration_endpoint"] = guard_url(meta["registration_endpoint"],
                                                 what="registration endpoint")
    scope = challenge.get("scope") or (" ".join(str(s) for s in scopes)
                                       if isinstance(scopes, list) and scopes else "")
    if scope:
        out["scope"] = scope
    return out


# --------------------------------------------------------------------------- #
# Client registration                                                           #
# --------------------------------------------------------------------------- #

def register_client(meta: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
    """RFC 7591 dynamic registration: a public client using PKCE. {client_id, client_secret?,
    token_endpoint_auth_method}."""
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise NeedsClient("needs_client",
                          "This server's sign-in does not register apps itself. Create an OAuth "
                          "app with the provider and type its client id (and secret) here.")
    request = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
               "grant_types": ["authorization_code", "refresh_token"],
               "response_types": ["code"], "token_endpoint_auth_method": "none"}
    if meta.get("scope"):
        request["scope"] = meta["scope"]
    resp = _fetch("POST", endpoint, what="registration endpoint",
                  headers={"Content-Type": "application/json", "Accept": "application/json"},
                  data=json.dumps(request).encode())
    if resp.status not in (200, 201):
        raise McpOAuthError("registration_refused",
                            f"The server refused to register BOSS ({resp.status}).")
    body = _json(resp, what="registration endpoint")
    if not body.get("client_id"):
        raise McpOAuthError("registration_refused", "The server registered BOSS without a client id.")
    return {"client_id": str(body["client_id"]),
            "client_secret": str(body["client_secret"]) if body.get("client_secret") else None,
            "token_endpoint_auth_method": str(body.get("token_endpoint_auth_method") or
                                              ("client_secret_basic" if body.get("client_secret")
                                               else "none"))}


# --------------------------------------------------------------------------- #
# Stored configuration (mcp_servers.auth.oauth — never a token)                 #
# --------------------------------------------------------------------------- #

def _server_and_auth(connection: Connection, project_id: str, server_id: str
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    from .mcp_registry import _raw_auth, get_mcp_server

    server = get_mcp_server(connection, project_id, server_id)
    if server is None:
        raise McpOAuthError("not_found", "MCP server not found.")
    auth = _raw_auth(connection, project_id, server_id)
    if str(auth.get("type") or "none") != "oauth":
        raise McpOAuthError("not_oauth", f"{server['name']} does not sign in with OAuth. Choose "
                                          "OAuth as its sign-in first.")
    return server, auth


def _save_oauth(connection: Connection, project_id: str, server_id: str,
                oauth: dict[str, Any], *, secret_ref: Optional[dict[str, Any]] = None) -> None:
    from .mcp_registry import save_oauth_config

    save_oauth_config(connection, project_id, server_id, oauth, secret_ref=secret_ref)


def _client_secret(connection: Connection, settings: Any, auth: dict[str, Any]) -> Optional[str]:
    ref = str(auth.get("secret_ref_id") or "")
    if not ref:
        return None
    from .secret_backends import resolve_secret_ref

    return resolve_secret_ref(connection, ref, settings) or None


def prepare(begin: Begin, settings: Any, project_id: str, server_id: str, *, redirect_uri: str,
            actor: str) -> dict[str, Any]:
    """Discover and register (OUTSIDE any transaction), then keep what was learned on the
    server's auth. A client the operator typed is used as typed; a registered one is reused while
    its redirect URI and authorization server stay the same."""
    with begin() as connection:
        server, auth = _server_and_auth(connection, project_id, server_id)
    current = dict(auth.get("oauth") or {})
    meta = discover(server["url"])
    oauth = {**current, **meta}
    manual = current.get("client_source") == "manual" and current.get("client_id")
    same_as = current.get("authorization_server") == meta["authorization_server"]
    reusable = (current.get("client_source") == "dynamic" and current.get("client_id") and same_as
                and current.get("client_redirect_uri") == redirect_uri)
    secret_ref: Optional[dict[str, Any]] = None
    if not manual and not reusable:
        client = register_client(meta, redirect_uri)
        oauth.update(client_id=client["client_id"], client_source="dynamic",
                     client_redirect_uri=redirect_uri,
                     token_endpoint_auth_method=client["token_endpoint_auth_method"])
        if client.get("client_secret"):
            from .secret_backends import default_secret_backend
            from .secrets_service import put_secret

            with begin() as connection:
                written = put_secret(connection, settings, project_id=project_id,
                                     name=f"mcp_{server['name']}_oauth_client",
                                     value=client["client_secret"], secret_kind="api_token",
                                     backend=default_secret_backend(settings), actor=actor)
            secret_ref = {"secret_ref_id": written["secret_ref_id"],
                          "last4": written.get("last4")}
        else:
            secret_ref = {"secret_ref_id": None, "last4": None}
    elif manual:
        oauth.setdefault("token_endpoint_auth_method",
                         "client_secret_basic" if auth.get("secret_ref_id") else "none")
    with begin() as connection:
        _save_oauth(connection, project_id, server_id, oauth, secret_ref=secret_ref)
        emit_event(connection, project_id, "mcp.oauth_prepared", actor, "mcp_server", server_id,
                   {"authorization_server": oauth["authorization_server"],
                    "client_source": oauth.get("client_source")})
    return oauth


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) — RFC 7636: 43-128 unreserved characters."""
    verifier = _b64url(secrets.token_bytes(48))          # 64 characters
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _state_hash(state: str) -> str:
    return hashlib.sha256(str(state).encode("utf-8")).hexdigest()


def start(connection: Connection, settings: Any, project_id: str, server_id: str, *,
          subject_kind: str, subject_id: str, redirect_uri: str, actor: str,
          started_by: Optional[str] = None) -> dict[str, Any]:
    """Begin one sign-in: keep the verifier and the state's hash, answer the provider's URL.

    `started_by` is the VERIFIED principal who pressed Sign in (None on an install without
    identity); the callback must come back signed in as them (:func:`complete`). A person's own
    sign-in is always theirs."""
    from .connection_credentials import SUBJECT_KINDS

    if subject_kind not in SUBJECT_KINDS or not str(subject_id or "").strip():
        raise McpOAuthError("bad_subject", "Say whose sign-in this is.")
    if subject_kind == "user":
        started_by = subject_id
    _, auth = _server_and_auth(connection, project_id, server_id)
    oauth = auth.get("oauth") or {}
    if not oauth.get("authorization_endpoint") or not oauth.get("client_id"):
        raise McpOAuthError("not_prepared", "Sign-in is not set up for this server yet.")
    endpoint = guard_url(oauth["authorization_endpoint"], what="authorization endpoint")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    connection.execute(text("DELETE FROM mcp_oauth_pending WHERE expires_at < now()"))
    connection.execute(text(
        "INSERT INTO mcp_oauth_pending (state_hash, project_id, server_id, subject_kind, "
        "subject_id, code_verifier, redirect_uri, created_by, started_by_principal, expires_at) "
        "VALUES (:h, :p, :s, :k, :sub, :v, :r, :by, :sbp, now() + make_interval(secs => :ttl))"),
        {"h": _state_hash(state), "p": project_id, "s": server_id, "k": subject_kind,
         "sub": subject_id, "v": verifier, "r": redirect_uri, "by": actor, "sbp": started_by,
         "ttl": PENDING_TTL_SECONDS})
    query = {"response_type": "code", "client_id": oauth["client_id"],
             "redirect_uri": redirect_uri, "code_challenge": challenge,
             "code_challenge_method": "S256", "state": state,
             "resource": oauth.get("resource") or ""}
    if oauth.get("scope"):
        query["scope"] = oauth["scope"]
    query = {k: v for k, v in query.items() if v}
    sep = "&" if urlsplit(endpoint).query else "?"
    emit_event(connection, project_id, "mcp.oauth_started", actor, "mcp_server", server_id,
               {"subject_kind": subject_kind, "subject_id": subject_id})
    return {"redirect": endpoint + sep + urlencode(query), "state": state}


def begin_sign_in(begin: Begin, settings: Any, project_id: str, server_id: str, *,
                  subject_kind: str, subject_id: str, redirect_uri: str, actor: str,
                  started_by: Optional[str] = None) -> dict[str, Any]:
    """Prepare (discover, register) then start: what the console's Sign in button calls."""
    prepare(begin, settings, project_id, server_id, redirect_uri=redirect_uri, actor=actor)
    with begin() as connection:
        return start(connection, settings, project_id, server_id, subject_kind=subject_kind,
                     subject_id=subject_id, redirect_uri=redirect_uri, actor=actor,
                     started_by=started_by)


# --------------------------------------------------------------------------- #
# Token endpoint                                                                #
# --------------------------------------------------------------------------- #

def _token_request(oauth: dict[str, Any], client_secret: Optional[str],
                   form: dict[str, str]) -> Any:
    form = {k: v for k, v in {**form, "client_id": oauth["client_id"],
                              "resource": oauth.get("resource") or ""}.items() if v}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    method = str(oauth.get("token_endpoint_auth_method") or "none")
    if client_secret and method == "client_secret_post":
        form["client_secret"] = client_secret
    elif client_secret:
        pair = f"{oauth['client_id']}:{client_secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    return _fetch("POST", oauth["token_endpoint"], what="token endpoint", headers=headers,
                  data=urlencode(form).encode("ascii"))


def _tokens(resp: Any) -> dict[str, Any]:
    body = _json(resp, what="token endpoint")
    if resp.status != 200 or not body.get("access_token"):
        error = str(body.get("error") or f"HTTP {resp.status}")
        raise McpOAuthError(error, f"The server refused the sign-in ({error}).")
    if str(body.get("token_type") or "bearer").lower() != "bearer":
        raise McpOAuthError("token_type", "The server issued a token BOSS cannot send (not Bearer).")
    return body


def _connected_as(body: dict[str, Any]) -> Optional[str]:
    """Who signed in, for the console only: the id token's email / name if one came back
    (not verified — it is a label, never a decision)."""
    token = body.get("id_token")
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # noqa: BLE001 — a label, not a check
        return None
    for key in ("email", "preferred_username", "name", "sub"):
        if claims.get(key):
            return str(claims[key])[:200]
    return None


def complete(begin: Begin, settings: Any, *, state: str, code: str = "", error: str = "",
             completed_by: Optional[str] = None) -> dict[str, Any]:
    """The callback: spend the pending sign-in, trade the code, keep the tokens in the vault.

    The state is single-use whatever happens next (a replayed callback finds nothing), and it
    is the ONLY thing that says which server and whose sign-in this is — never a query field.

    BOUND TO WHOEVER STARTED IT. `completed_by` is the verified principal of the browser that
    came back (None when it carries no BOSS session). A sign-in started by a known principal —
    always so for a person's own — completes only for that same principal: a consent link handed
    to someone else cannot put THEIR provider account under the starter's name (login CSRF)."""
    from . import connection_credentials as creds

    if not str(state or "").strip():
        raise McpOAuthError("invalid_state", "This sign-in link is not one BOSS started.")
    with begin() as connection:
        pending = connection.execute(text(
            "DELETE FROM mcp_oauth_pending WHERE state_hash = :h RETURNING project_id, server_id, "
            "subject_kind, subject_id, code_verifier, redirect_uri, created_by, "
            "started_by_principal, (expires_at < now()) AS expired"),
            {"h": _state_hash(state)}).mappings().first()
    if pending is None:
        raise McpOAuthError("invalid_state", "This sign-in link is not one BOSS started, or it "
                                             "was already used. Press Sign in again.")
    if pending["expired"]:
        raise McpOAuthError("expired", "This sign-in took longer than ten minutes. Press Sign in "
                                       "again.")
    if pending["started_by_principal"] and pending["started_by_principal"] != completed_by:
        raise McpOAuthError("not_the_starter", "This sign-in was started by someone else. Press "
                                               "Sign in yourself, signed in to BOSS as you.")
    if error:
        raise McpOAuthError("denied", f"The provider did not sign BOSS in: {str(error)[:120]}.")
    if not str(code or "").strip():
        raise McpOAuthError("no_code", "The provider sent no authorization code.")
    project_id, server_id = str(pending["project_id"]), str(pending["server_id"])
    with begin() as connection:
        server, auth = _server_and_auth(connection, project_id, server_id)
        client_secret = _client_secret(connection, settings, auth)
    oauth = auth.get("oauth") or {}
    body = _tokens(_token_request(oauth, client_secret, {
        "grant_type": "authorization_code", "code": str(code),
        "redirect_uri": str(pending["redirect_uri"]), "code_verifier": str(pending["code_verifier"])}))
    expires = (datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
               if str(body.get("expires_in") or "").isdigit() else None)
    connected_as = _connected_as(body) or (str(pending["created_by"])
                                           if pending["subject_kind"] != "agent" else None)
    scopes = [s for s in str(body.get("scope") or oauth.get("scope") or "").split() if s] or None
    with begin() as connection:
        stored = creds.store(connection, project_id, server_id=server_id,
                             subject_kind=str(pending["subject_kind"]),
                             subject_id=str(pending["subject_id"]),
                             secret=str(body["access_token"]),
                             refresh=str(body["refresh_token"]) if body.get("refresh_token") else None,
                             access_expires_at=expires, connected_as=connected_as, scopes=scopes,
                             actor=str(pending["created_by"]), kind="oauth")
    return {"ok": True, "project_id": project_id, "server_id": server_id,
            "server": server["name"], "credential": stored}


def refresher(connection: Connection, settings: Any, project_id: str, server: dict[str, Any],
              raw_auth: dict[str, Any]) -> Callable[[str], tuple[str, Optional[str], int]]:
    """The refresh the vault calls (single flight, under its row lock) for this server's tokens:
    (access, rotated refresh or None, expires_in). A refusal (invalid_grant and kin) raises
    RefreshRejected, which leaves the credential needs_reauth; a network failure raises
    McpOAuthError and changes nothing (the next call tries again)."""
    from ..identity import vault

    oauth = dict(raw_auth.get("oauth") or {})
    client_secret = _client_secret(connection, settings, raw_auth)

    def refresh(refresh_token: str) -> tuple[str, Optional[str], int]:
        if not oauth.get("token_endpoint") or not oauth.get("client_id"):
            raise vault.RefreshRejected("not_configured")
        resp = _token_request(oauth, client_secret, {"grant_type": "refresh_token",
                                                     "refresh_token": refresh_token})
        if resp.status in (400, 401):
            raise vault.RefreshRejected(str(_safe_error(resp)))
        body = _tokens(resp)
        expires_in = int(body["expires_in"]) if str(body.get("expires_in") or "").isdigit() \
            else 3600
        return (str(body["access_token"]),
                str(body["refresh_token"]) if body.get("refresh_token") else None, expires_in)

    return refresh


def _safe_error(resp: Any) -> str:
    try:
        return str((resp.json() or {}).get("error") or resp.status)
    except Exception:  # noqa: BLE001
        return str(resp.status)


def callback_url(base: str) -> str:
    """The redirect URI for an install whose public origin is `base`."""
    return f"{str(base).rstrip('/')}{CALLBACK_PATH}"
