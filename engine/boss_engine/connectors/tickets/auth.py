"""Whose credential a ticket-system call carries, and a person's own OAuth sign-in.

AUTH KINDS (manifest ``auth_kinds``) — a connection may hold several:

    {kind: api_token, fields: [email, api_token]}             the connection's bot / API key
    {kind: oauth2, authorize_url, token_url, scopes, audience, pkce, per_user,
     resource_discovery_url, api_base_template}                a person's own sign-in (3LO)

PER CALL (:func:`select_credential`, used by the shared HTTP helper): the acting person's OAuth
token if they have one (and, for an agent acting for them, their delegation grant lends it),
otherwise the connection's bot token, otherwise :class:`AuthFailed` with reason
``needs_person_sign_in``. The connection's ``credential_policy`` (shared | per_user |
per_user_fallback) narrows that. Every call records the identity it acted as.

STORAGE: nothing new. A person's tokens are a ``connection_credentials`` row (connection_id
``kit:tickets:<provider>``, subject ``user``/usr_...) whose secret is an identity-vault item
(provider = the same string). Refresh is the vault's single-flight refresh: the row is locked
(SELECT ... FOR UPDATE in the vault's own transaction), re-read, refreshed once, and the ROTATED
refresh token committed before the call goes on — two refreshes never race, and a rollback of the
caller never loses a refresh token the provider has already burned.

The sign-in itself is three functions Phase B wires to routes: :func:`begin_oauth`,
:func:`complete_oauth`, :func:`disconnect_person`. The pending sign-in (state hash, PKCE verifier,
redirect URI, who started it) reuses ``mcp_oauth_pending`` (its ``connection_id`` column).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlsplit

from .contract import (
    CREDENTIAL_POLICIES,
    ActingCredential,
    AuthFailed,
    TicketConnection,
    TicketSystemError,
)

STATE_KEY_LABEL = "tickets.oauth.state"
STATE_TTL_SECONDS = 600
CALLBACK_PATH = "/v1/tickets/oauth/callback"

AUTH_KIND_NAMES = ("api_token", "oauth2")
OAUTH_FIELDS = {"kind", "authorize_url", "token_url", "scopes", "audience", "pkce", "per_user",
                "resource_discovery_url", "api_base_template", "extra_authorize_params"}


class OAuthError(TicketSystemError):
    """A sign-in step failed. ``code`` is stable (invalid_state, expired, not_the_starter,
    denied, no_code, exchange_failed, no_site, not_configured); the message is for a person."""

    def __init__(self, code: str, message: str, *, provider: str = ""):
        super().__init__(message, provider=provider)
        self.code = code


# ---------------------------------------------------------------------------------------------
# Manifest: auth kinds
# ---------------------------------------------------------------------------------------------


def validate_auth_kinds(raw: Any, field_names: list[str], hosts: list[str]) -> list[str]:
    """Problems with a manifest's ``auth_kinds`` (empty = valid)."""
    from .http import _host_matches

    problems: list[str] = []
    if not isinstance(raw, list) or not raw:
        return ["auth_kinds must be a non-empty list"]
    seen = set()
    for index, item in enumerate(raw):
        where = f"auth_kinds[{index}]"
        if not isinstance(item, dict) or item.get("kind") not in AUTH_KIND_NAMES:
            problems.append(f"{where}.kind must be one of {', '.join(AUTH_KIND_NAMES)}")
            continue
        if item["kind"] in seen:
            problems.append(f"{where}: kind '{item['kind']}' repeats")
        seen.add(item["kind"])
        if item["kind"] == "api_token":
            unknown = set(item) - {"kind", "fields"}
            fields = item.get("fields")
            if unknown:
                problems.append(f"{where}: unknown {sorted(unknown)}")
            if not isinstance(fields, list) or not all(f in field_names for f in fields):
                problems.append(f"{where}.fields must name auth_fields")
            continue
        unknown = set(item) - OAUTH_FIELDS
        if unknown:
            problems.append(f"{where}: unknown {sorted(unknown)}")
        for name in ("authorize_url", "token_url"):
            url = item.get(name)
            host = (urlsplit(str(url or "")).hostname or "").lower()
            if not (isinstance(url, str) and url.startswith("https://") and host):
                problems.append(f"{where}.{name} must be an https URL")
            elif not any(_host_matches(host, h) for h in hosts):
                problems.append(f"{where}.{name} host {host} is not in default_hosts")
        discovery = item.get("resource_discovery_url")
        if discovery is not None:
            host = (urlsplit(str(discovery)).hostname or "").lower()
            if not any(_host_matches(host, h) for h in hosts):
                problems.append(f"{where}.resource_discovery_url host is not in default_hosts")
            if not isinstance(item.get("api_base_template"), str) or \
                    "{resource_id}" not in item["api_base_template"]:
                problems.append(f"{where}.api_base_template needs {{resource_id}}")
        if not isinstance(item.get("scopes"), list) or not item["scopes"]:
            problems.append(f"{where}.scopes must be a non-empty list")
        for flag in ("pkce", "per_user"):
            if not isinstance(item.get(flag), bool):
                problems.append(f"{where}.{flag} must be true/false")
    return problems


def default_auth_kinds(auth_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "api_token", "fields": [f["name"] for f in auth_fields if f.get("secret")]}]


def oauth_spec(adapter: Any) -> Optional[dict[str, Any]]:
    for kind in getattr(getattr(adapter, "manifest", None), "auth_kinds", ()) or ():
        if kind.get("kind") == "oauth2":
            return dict(kind)
    return None


def credential_policy(conn: TicketConnection, adapter: Any) -> str:
    said = str(conn.configuration.get("credential_policy") or "").strip().lower()
    if said in CREDENTIAL_POLICIES:
        return said
    spec = oauth_spec(adapter)
    return "per_user_fallback" if spec and spec.get("per_user") else "shared"


# ---------------------------------------------------------------------------------------------
# Per call: whose credential
# ---------------------------------------------------------------------------------------------


def _bot(conn: TicketConnection) -> Optional[ActingCredential]:
    token = conn.credential()
    if not token:
        return None
    return ActingCredential("api_token", "org", conn.connection_id, token)


def _principal(conn: TicketConnection) -> Any:
    from ...services.connection_credentials import CredentialPrincipal, principal_from_context

    if conn.principal is not None:
        return conn.principal
    if conn.person_id:
        return CredentialPrincipal(person_id=conn.person_id)
    return principal_from_context(conn.db, str(conn.project_id))


def select_credential(conn: TicketConnection, adapter: Any) -> ActingCredential:
    """THE answer to "which credential does this call carry" for a ticket connection."""
    provider = conn.provider
    spec = oauth_spec(adapter)
    policy = credential_policy(conn, adapter)
    needs_person = AuthFailed(
        f"{provider}: this needs a person's own {getattr(adapter, 'display_name', provider)} "
        "sign-in, and the person this work acts for has not signed in (or their sign-in "
        "expired). Sign in with your own account to continue.",
        reason="needs_person_sign_in", provider=provider)
    if spec is None or policy == "shared":
        bot = _bot(conn)
        if bot is None:
            raise AuthFailed(f"{provider}: no credential is configured for this connection",
                             reason="no_credential", provider=provider)
        return bot
    if conn.db is None or not conn.project_id:
        bot = _bot(conn) if policy == "per_user_fallback" else None
        if bot is None:
            raise needs_person
        return bot
    from ...identity import vault
    from ...services import connection_credentials as creds

    try:
        found = creds.resolve(conn.db, str(conn.project_id), policy=policy,
                              principal=_principal(conn), connection_id=conn.connection_id,
                              static_shared=True)
    except creds.CredentialUnavailable:
        raise needs_person from None
    if found.credential is None:
        bot = _bot(conn)
        if bot is None:
            raise needs_person
        return bot
    row = found.credential
    try:
        token = vault.connection_token(row["vault_item_id"], provider=conn.connection_id,
                                       db=conn.db, refresher=oauth_refresher(conn, adapter))
    except vault.VaultNeedsReauth as gone:
        creds.mark_needs_reauth(conn.db, str(conn.project_id), row["id"], reason=gone.code)
        bot = _bot(conn) if policy == "per_user_fallback" else None
        if bot is None:
            raise needs_person from None
        return bot
    extra: dict[str, Any] = {}
    if spec.get("resource_discovery_url"):
        extra["resource_id"] = resource_id(conn, adapter, token)
    return ActingCredential("oauth2", "user", str(row["subject_id"]), token, extra)


def record_call(conn: TicketConnection, credential: ActingCredential, method: str,
                path: str, status: Optional[int]) -> None:
    """Every call's identity: on the connection (for the caller), and as an event when a project
    database is at hand."""
    entry = {"method": method, "path": path, "status": status, **credential.describe()}
    conn.attribution.append(entry)
    if conn.db is None or not conn.project_id:
        return
    try:
        from ...services.core import emit_event

        with conn.db.begin_nested():
            emit_event(conn.db, str(conn.project_id), "ticket_system.call",
                       credential.subject_id, "ticket_connection", conn.connection_id, entry)
    except Exception:  # noqa: BLE001 — the in-memory record stands; the log is best effort
        pass


# ---------------------------------------------------------------------------------------------
# OAuth plumbing (every request through the shared transport + host check)
# ---------------------------------------------------------------------------------------------


def _oauth_request(adapter: Any, method: str, url: str, *, body: Optional[dict] = None,
                   bearer: Optional[str] = None) -> tuple[int, Any]:
    from . import http

    manifest = adapter.manifest
    http.check_host(url, tuple(manifest.default_hosts), (), adapter.provider_key)
    headers = {"Accept": "application/json", "User-Agent": "BOSS-ticket-connector/2"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        response = http.TRANSPORT(method, url, headers, data, http.DEFAULT_TIMEOUT_S, None)
    except Exception as error:  # noqa: BLE001
        raise OAuthError("unreachable", f"{adapter.provider_key}: the sign-in service could not "
                         f"be reached ({type(error).__name__})",
                         provider=adapter.provider_key) from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status, payload


def _client(conn: TicketConnection, adapter: Any) -> tuple[str, Optional[str]]:
    """(client id, client secret) of the OAuth app: the connection's own, else the operator's
    host settings BOSS_<PROVIDER>_OAUTH_CLIENT_ID / _SECRET."""
    prefix = f"BOSS_{adapter.provider_key.upper()}_OAUTH_CLIENT"
    client_id = str(conn.configuration.get("oauth_client_id") or os.environ.get(f"{prefix}_ID")
                    or "").strip()
    # A secret given with the configuration (one-shot / tests, like api_token), else the
    # project's stored secret reference, else the operator's host setting.
    secret: Optional[str] = str(conn.configuration.get("oauth_client_secret") or "").strip() \
        or None
    ref = str(conn.configuration.get("oauth_client_secret_ref") or "").strip()
    if not secret and ref and conn.db is not None and conn.project_id:
        from ...config import get_settings
        from ...services.secret_backends import resolve_secret_ref

        secret = resolve_secret_ref(conn.db, ref, conn.app_settings or get_settings(),
                                    project_id=str(conn.project_id)) or None
    secret = secret or (os.environ.get(f"{prefix}_SECRET") or "").strip() or None
    if not client_id:
        raise OAuthError("not_configured", f"{adapter.display_name} sign-in is not set up: the "
                         "connection needs an OAuth app (client id and secret).",
                         provider=adapter.provider_key)
    return client_id, secret


def oauth_refresher(conn: TicketConnection, adapter: Any
                    ) -> Callable[[str], tuple[str, Optional[str], int]]:
    """The refresh the vault runs under its row lock: (access, ROTATED refresh, expires_in).
    A refusal raises vault.RefreshRejected (the credential becomes needs_reauth)."""
    from ...identity import vault

    spec = oauth_spec(adapter) or {}

    def refresh(refresh_token: str) -> tuple[str, Optional[str], int]:
        client_id, client_secret = _client(conn, adapter)
        body = {"grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": refresh_token}
        if client_secret:
            body["client_secret"] = client_secret
        status, payload = _oauth_request(adapter, "POST", spec["token_url"], body=body)
        if status in (400, 401, 403):
            raise vault.RefreshRejected(str((payload or {}).get("error") or status))
        if status >= 300 or not isinstance(payload, dict) or not payload.get("access_token"):
            raise OAuthError("exchange_failed", f"{adapter.provider_key}: refresh failed "
                             f"(HTTP {status})", provider=adapter.provider_key)
        return (str(payload["access_token"]),
                str(payload["refresh_token"]) if payload.get("refresh_token") else None,
                int(payload.get("expires_in") or 3600))

    return refresh


def resource_id(conn: TicketConnection, adapter: Any, token: str) -> str:
    """The provider's id for this connection's site (Atlassian cloudId): configured, cached, or
    found once from the accessible-resources list by matching the connection's site URL."""
    saved = str(conn.configuration.get("oauth_resource_id") or "").strip()
    if saved:
        return saved
    cached = conn.cache.get("oauth_resource_id")
    if cached:
        return str(cached)
    spec = oauth_spec(adapter) or {}
    status, payload = _oauth_request(adapter, "GET", spec["resource_discovery_url"], bearer=token)
    if status in (401, 403):
        raise AuthFailed(f"{adapter.provider_key}: the sign-in was refused", status=status,
                         reason="needs_person_sign_in", provider=adapter.provider_key)
    site = (urlsplit(adapter.api_base(conn)).hostname or "").lower()
    for item in payload if isinstance(payload, list) else []:
        if (urlsplit(str(item.get("url") or "")).hostname or "").lower() == site and item.get("id"):
            conn.cache["oauth_resource_id"] = str(item["id"])
            return str(item["id"])
    raise OAuthError("no_site", f"{adapter.display_name}: the signed-in account cannot reach "
                     f"{site or 'this connection’s site'}.", provider=adapter.provider_key)


# ---------------------------------------------------------------------------------------------
# Sign-in: begin / complete / disconnect (Phase B wires these to routes)
# ---------------------------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _state_key() -> bytes:
    from ...identity import vault

    return vault.derived_key(STATE_KEY_LABEL)


def sign_state(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(_state_key(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def read_state(state: str) -> dict[str, Any]:
    body, _, mac = str(state or "").partition(".")
    if not body or not mac:
        raise OAuthError("invalid_state", "This sign-in link is not one BOSS started.")
    expected = hmac.new(_state_key(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _unb64(mac)
    except (ValueError, TypeError):
        given = b""
    if not hmac.compare_digest(expected, given):
        raise OAuthError("invalid_state", "This sign-in link is not one BOSS started.")
    payload = json.loads(_unb64(body))
    if int(payload.get("exp") or 0) < int(time.time()):
        raise OAuthError("expired", "This sign-in took longer than ten minutes. Sign in again.")
    return payload


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _need_db(conn: TicketConnection) -> None:
    if conn.db is None or not conn.project_id:
        raise OAuthError("not_configured", "A person's sign-in needs the project's database.",
                         provider=conn.provider)


def default_redirect_uri() -> str:
    base = (os.environ.get("BOSS_PUBLIC_URL") or os.environ.get("BOSS_PUBLIC_BASE_URL")
            or "").rstrip("/")
    return f"{base}{CALLBACK_PATH}" if base else CALLBACK_PATH


def begin_oauth(connection: TicketConnection, person: str, *,
                redirect_uri: Optional[str] = None) -> dict[str, str]:
    """Start a person's own sign-in. Returns {authorize_url, state}.

    The state is HMAC-signed (a vault-derived key) and names the project, the connection and the
    person; its hash, the PKCE verifier (when the provider supports PKCE) and the redirect URI
    are kept single-use for ten minutes. Only that person can complete it."""
    from sqlalchemy import text

    from ...services.core import emit_event
    from .registry import get_ticket_adapter

    adapter = get_ticket_adapter(connection.provider)
    spec = oauth_spec(adapter)
    if spec is None:
        raise OAuthError("not_configured", f"{adapter.display_name} has no per-person sign-in.",
                         provider=connection.provider)
    _need_db(connection)
    person = str(person or "").strip()
    if not person.startswith("usr_"):
        raise OAuthError("bad_subject", "A sign-in belongs to a person (a BOSS user).",
                         provider=connection.provider)
    client_id, _ = _client(connection, adapter)
    redirect = redirect_uri or default_redirect_uri()
    state = sign_state({"p": str(connection.project_id), "c": connection.connection_id,
                        "u": person, "n": secrets.token_urlsafe(16),
                        "exp": int(time.time()) + STATE_TTL_SECONDS})
    verifier, challenge = "", ""
    if spec.get("pkce"):
        verifier = _b64(secrets.token_bytes(48))
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    db = connection.db
    db.execute(text("DELETE FROM mcp_oauth_pending WHERE expires_at < now()"))
    db.execute(text(
        "INSERT INTO mcp_oauth_pending (state_hash, project_id, server_id, connection_id, "
        "subject_kind, subject_id, code_verifier, redirect_uri, created_by, started_by_principal, "
        "expires_at) VALUES (:h, :p, NULL, :c, 'user', :u, :v, :r, :u, :u, "
        "now() + make_interval(secs => :ttl))"),
        {"h": _state_hash(state), "p": connection.project_id, "c": connection.connection_id,
         "u": person, "v": verifier, "r": redirect, "ttl": STATE_TTL_SECONDS})
    query = {"audience": spec.get("audience") or "", "client_id": client_id,
             "scope": " ".join(spec.get("scopes") or []), "redirect_uri": redirect,
             "state": state, "response_type": "code", "prompt": "consent"}
    query.update({str(k): str(v) for k, v in (spec.get("extra_authorize_params") or {}).items()})
    if challenge:
        query.update(code_challenge=challenge, code_challenge_method="S256")
    query = {k: v for k, v in query.items() if v}
    emit_event(db, str(connection.project_id), "ticket_system.oauth_started", person,
               "ticket_connection", connection.connection_id, {"provider": connection.provider})
    sep = "&" if urlsplit(spec["authorize_url"]).query else "?"
    return {"authorize_url": spec["authorize_url"] + sep + urlencode(query), "state": state}


def complete_oauth(connection: TicketConnection, state: str, code: str, *,
                   completed_by: Optional[str] = None) -> dict[str, Any]:
    """The callback: check the signed state, spend the pending sign-in (single use), trade the
    code, find the site's resource id, and keep the tokens as the person's credential.

    ``completed_by``: the verified BOSS principal of the browser that came back; when given it
    must be the person who started (a consent link handed to someone else is refused)."""
    from sqlalchemy import text

    from ...services import connection_credentials as creds
    from .registry import get_ticket_adapter

    adapter = get_ticket_adapter(connection.provider)
    spec = oauth_spec(adapter)
    if spec is None:
        raise OAuthError("not_configured", f"{adapter.display_name} has no per-person sign-in.",
                         provider=connection.provider)
    _need_db(connection)
    payload = read_state(state)
    if payload.get("p") != str(connection.project_id) or payload.get("c") != \
            connection.connection_id:
        raise OAuthError("invalid_state", "This sign-in belongs to another connection.")
    db = connection.db
    pending = db.execute(text(
        "DELETE FROM mcp_oauth_pending WHERE state_hash = :h RETURNING subject_id, "
        "code_verifier, redirect_uri, started_by_principal, (expires_at < now()) AS expired"),
        {"h": _state_hash(state)}).mappings().first()
    if pending is None:
        raise OAuthError("invalid_state", "This sign-in link was already used. Sign in again.")
    if pending["expired"]:
        raise OAuthError("expired", "This sign-in took longer than ten minutes. Sign in again.")
    person = str(pending["subject_id"])
    if person != payload.get("u") or (completed_by is not None and completed_by != person):
        raise OAuthError("not_the_starter", "This sign-in was started by someone else. Sign in "
                         "yourself, signed in to BOSS as you.")
    if not str(code or "").strip():
        raise OAuthError("no_code", "The provider sent no authorization code.")
    client_id, client_secret = _client(connection, adapter)
    body = {"grant_type": "authorization_code", "client_id": client_id, "code": str(code),
            "redirect_uri": str(pending["redirect_uri"])}
    if client_secret:
        body["client_secret"] = client_secret
    if pending["code_verifier"]:
        body["code_verifier"] = str(pending["code_verifier"])
    status, tokens = _oauth_request(adapter, "POST", spec["token_url"], body=body)
    if status >= 300 or not isinstance(tokens, dict) or not tokens.get("access_token"):
        error = str((tokens or {}).get("error") or f"HTTP {status}") if isinstance(tokens, dict) \
            else f"HTTP {status}"
        raise OAuthError("exchange_failed", f"The provider refused the sign-in ({error[:80]}).",
                         provider=connection.provider)
    access = str(tokens["access_token"])
    found_resource = None
    if spec.get("resource_discovery_url"):
        connection.cache.pop("oauth_resource_id", None)
        found_resource = resource_id(connection, adapter, access)
        _save_resource_id(connection, found_resource)
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in") or 3600))
    scopes = [s for s in str(tokens.get("scope") or " ".join(spec.get("scopes") or [])).split()
              if s] or None
    stored = creds.store(db, str(connection.project_id), connection_id=connection.connection_id,
                         subject_kind="user", subject_id=person, secret=access,
                         refresh=str(tokens["refresh_token"]) if tokens.get("refresh_token")
                         else None, access_expires_at=expires, kind="oauth",
                         connected_as=person, scopes=scopes, actor=person)
    return {"ok": True, "person": person, "provider": connection.provider,
            "resource_id": found_resource, "credential": stored}


def _save_resource_id(connection: TicketConnection, value: str) -> None:
    """Keep the site's resource id on the connection row (non-secret), so later calls skip the
    discovery request. A connection without a row (not saved yet) just keeps it in memory."""
    from sqlalchemy import text

    connection.configuration["oauth_resource_id"] = value
    try:
        with connection.db.begin_nested():
            connection.db.execute(text(
                "UPDATE work_tracker_providers SET configuration = configuration || "
                "jsonb_build_object('oauth_resource_id', CAST(:v AS TEXT)), updated_at = now() "
                "WHERE project_id = :p AND provider_key = :k"),
                {"v": value, "p": connection.project_id, "k": connection.provider})
    except Exception:  # noqa: BLE001 — memory still holds it
        pass


def disconnect_person(connection: TicketConnection, person: str, *,
                      actor: Optional[str] = None) -> bool:
    """Remove a person's own sign-in for this connection: the vault item is revoked (shredded)
    and the row deleted. True when there was one."""
    from ...services import connection_credentials as creds

    _need_db(connection)
    row = creds.find(connection.db, str(connection.project_id),
                     connection_id=connection.connection_id, subject_kind="user",
                     subject_id=str(person))
    if row is None:
        return False
    creds.remove(connection.db, str(connection.project_id), row["id"], actor=actor or str(person))
    return True
