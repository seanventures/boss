"""Document-system connections (BOSSAPP-1384): Confluence, connected once for the org.

THE MODEL is the ticket systems' (ticket_reach): one org-wide connection that every project of
the org reads through. It lives in ``docs_connections`` (one row per org and provider) with a HOME
project, the one it was connected from — where its token is kept in the vault and where it is
managed (integration:manage there).

THE SECRET is stored by reference: the API token goes to the secret backend, the row keeps
``configuration.secret_ref_id`` plus ``last4`` and a fingerprint. The token is resolved only when a
request is sent; it never enters settings, events, logs or an API answer.

SIGN-IN. Connect = email + API token (``basic``), or a bearer token (``bearer``: a Data Center PAT
or an OAuth 2.0 3LO access token — the structure is ready, the sign-in flow is not built). The
operator may reuse the org's Jira credential for the same Atlassian site (``use_jira_credential``):
the email and token are COPIED into this connection's own vault entry, so disconnecting Jira
later does not break Confluence.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.docs import (
    AUTH_METHODS,
    DEPLOYMENTS,
    NOT_CONNECTED,
    ConfluenceClient,
    ConfluenceError,
    ConfluenceNotConnected,
    validate_site,
)
from ..connectors.tickets.contract import AuthFailed, TicketSystemError
from .core import emit_event, make_id

PROVIDER = "confluence"
PROVIDERS = (PROVIDER,)
TOKEN_KEYS = ("api_token", "access_token", "token", "pat", "password")
MAX_SPACES = 20


class DocsConnectionError(Exception):
    """A refusal in words a person can act on. ``code`` is the API's error name."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self)}


def catalog() -> list[dict[str, Any]]:
    """The document systems BOSS can connect, with the fields Setup shows."""
    return [{
        "provider_key": PROVIDER, "label": "Confluence", "icon": "confluence",
        "description": "Atlassian Confluence Cloud: search and read pages, publish pages "
                       "(with your approval), and keep chosen spaces in BOSS's knowledge.",
        "docs_url": "https://developer.atlassian.com/cloud/confluence/rest/v2/intro/",
        "auth_fields": [
            {"name": "base_url", "label": "Site URL", "placeholder": "https://your-team.atlassian.net/wiki"},
            {"name": "email", "label": "Atlassian account email"},
            {"name": "api_token", "label": "API token", "secret": True,
             "help": "Create at id.atlassian.com → Security → API tokens"},
        ],
        "auth_methods": list(AUTH_METHODS),
        "deployments": {k: {"implemented": v["implemented"]} for k, v in DEPLOYMENTS.items()},
        "capabilities": {"read": True, "search": True, "publish": True, "knowledge": True},
    }]


def _config(row: Any) -> dict[str, Any]:
    raw = row["configuration"]
    return json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})


def org_of(db: Connection, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON "
                           "w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).scalar()


def get_row(db: Connection, project_id: str, provider: str = PROVIDER, *,
            include_disconnected: bool = False) -> Optional[dict[str, Any]]:
    """The org's connection as project ``project_id`` sees it (the org's, whichever project it
    was connected from), or None."""
    org = org_of(db, project_id)
    if org is None:
        return None
    row = db.execute(text(
        "SELECT * FROM docs_connections WHERE org_id = :o AND provider_key = :k"
        + ("" if include_disconnected else " AND status = 'active'")),
        {"o": org, "k": provider}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["configuration"] = _config(row)
    return out


def is_connected(row: Optional[dict[str, Any]]) -> bool:
    return bool(row and row["status"] == "active" and row["configuration"].get("secret_ref_id"))


def public_view(db: Connection, row: Optional[dict[str, Any]], project_id: str, *,
                can_manage: bool = False) -> Optional[dict[str, Any]]:
    """The row as the console reads it. Never the token; last4 and the fingerprint only to a
    person who manages the connection."""
    if row is None:
        return None
    config = row["configuration"]
    name = db.execute(text("SELECT name FROM projects WHERE id = :p"),
                      {"p": row["home_project_id"]}).scalar()
    status = "connected" if is_connected(row) else "disconnected"
    if status == "connected" and row.get("last_test_ok") is False:
        status = "needs_attention"
    return {
        "id": row["id"], "provider": row["provider_key"], "label": "Confluence", "status": status,
        "base_url": config.get("base_url"), "deployment": config.get("deployment", "cloud"),
        "auth_method": config.get("auth_method", "basic"), "email": config.get("email"),
        "acts_as": config.get("acts_as"),
        "default_spaces": list(config.get("default_spaces") or []),
        "knowledge_spaces": list(config.get("knowledge_spaces") or []),
        "sync_state": ((config.get("sync_states") or {}).get(project_id)) or {},
        "home_project": {"id": row["home_project_id"], "name": name},
        "reach": "project" if row["home_project_id"] == project_id else "org",
        "last_tested_at": row["last_tested_at"].isoformat() if row.get("last_tested_at") else None,
        "last_test_ok": row.get("last_test_ok"), "can_manage": can_manage,
        "last4": config.get("last4") if can_manage else None,
        "fingerprint": config.get("fingerprint") if can_manage else None,
        "credential_from": config.get("credential_from"),
    }


def _fingerprint(token: str) -> str:
    from ..identity.authn_support import keyed_hash

    return keyed_hash("docs_connection_token", token)[:16]


def _spaces(values: Any) -> list[str]:
    seen: list[str] = []
    for v in values or []:
        key = str(v).strip()
        if key and key not in seen:
            seen.append(key)
    if len(seen) > MAX_SPACES:
        raise DocsConnectionError("invalid_request", f"Pick at most {MAX_SPACES} spaces.")
    return seen


def explain(error: BaseException) -> DocsConnectionError:
    """A typed API refusal for a connector failure, in words (no credential, no raw body)."""
    if isinstance(error, DocsConnectionError):
        return error
    if isinstance(error, ConfluenceNotConnected):
        return DocsConnectionError("not_connected", NOT_CONNECTED, 424)
    if isinstance(error, AuthFailed):
        return DocsConnectionError("auth_failed", str(error), 424)
    from ..connectors.docs import VersionConflict
    from ..connectors.tickets.contract import HostNotAllowed, NotFound, RateLimited

    if isinstance(error, VersionConflict):
        return DocsConnectionError("version_conflict", str(error), 409)
    if isinstance(error, NotFound):
        return DocsConnectionError("not_found", str(error), 404)
    if isinstance(error, RateLimited):
        return DocsConnectionError("rate_limited", "Confluence is rate-limiting BOSS. Try again "
                                                   "shortly.", 429)
    if isinstance(error, HostNotAllowed):
        return DocsConnectionError("host_not_allowed", str(error), 422)
    if isinstance(error, TicketSystemError):
        return DocsConnectionError("upstream_error", str(error)[:300], 502)
    return DocsConnectionError("upstream_error", "Confluence did not accept the request.", 502)


# ---- the Jira credential, when the operator chooses to reuse it ------------------------------


def jira_credential(db: Connection, app_settings: Any, project_id: str
                    ) -> Optional[dict[str, str]]:
    """The org's connected Jira (its site, email and token) as this project can use it, else None."""
    from . import ticket_intake, ticket_reach

    for row in ticket_reach.reachable_rows(db, project_id):
        if str(row["provider_key"]) != "jira" or not ticket_intake.is_connected(row):
            continue
        from . import ticket_sync

        # The credential and the permission are the HOME row's, never a mirror's.
        row = ticket_sync.home_row(db, row) or row
        config = row["configuration"]
        token = ticket_intake.stored_token(db, app_settings, row)
        if token and config.get("base_url") and config.get("email"):
            return {"base_url": str(config["base_url"]), "email": str(config["email"]),
                    "token": token, "home_project": str(row.get("project_id") or "")}
    return None


# ---- connect / test / settings / disconnect ---------------------------------------------------


def prepare(db: Connection, app_settings: Any, *, project_id: str, base_url: str = "",
            auth_method: str = "basic", deployment: str = "cloud", email: str = "",
            token: str = "", use_jira_credential: bool = False,
            may_use_jira: Optional[Any] = None) -> tuple[dict[str, Any], str]:
    """(configuration without the token, the token) from a connect request, validated."""
    deployment = (deployment or "cloud").strip().lower()
    if deployment not in DEPLOYMENTS:
        raise DocsConnectionError("invalid_request", f"Confluence deployment is one of "
                                                     f"{', '.join(DEPLOYMENTS)}.")
    if not DEPLOYMENTS[deployment]["implemented"]:
        raise DocsConnectionError(
            "capability_not_supported",
            "Confluence Data Center / Server is not supported yet; BOSS connects to Confluence "
            "Cloud (https://<site>.atlassian.net/wiki).")
    auth_method = (auth_method or "basic").strip().lower()
    if auth_method not in AUTH_METHODS:
        raise DocsConnectionError("invalid_request", f"Sign-in is one of {', '.join(AUTH_METHODS)}.")
    credential_from = ""
    base_url, email, token = (base_url or "").strip(), (email or "").strip(), (token or "").strip()
    if use_jira_credential:
        jira = jira_credential(db, app_settings, project_id)
        if jira is None:
            raise DocsConnectionError(
                "not_connected", "No Jira is connected to reuse. Connect Jira in Setup first, or "
                                 "enter the Confluence email and API token.", 424)
        # Using Jira's credential needs integration:manage where Jira lives, and the site is
        # Jira's own: the token is never sent to a host the caller picked.
        if may_use_jira is not None and jira["home_project"]:
            may_use_jira(jira["home_project"])
        base_url, email, token, auth_method = jira["base_url"], jira["email"], jira["token"], "basic"
        credential_from = "jira"
    try:
        site = validate_site(base_url, deployment)
    except ConfluenceError as error:
        raise DocsConnectionError("invalid_request", str(error)) from None
    if auth_method == "basic" and not email:
        raise DocsConnectionError("invalid_request", "Confluence needs the Atlassian account "
                                                     "email that owns the API token.")
    config = {"base_url": site, "deployment": deployment, "auth_method": auth_method}
    if email:
        config["email"] = email
    if credential_from:
        config["credential_from"] = credential_from
    return config, token


def client_for_config(config: dict[str, Any], token: str) -> ConfluenceClient:
    return ConfluenceClient(config, lambda: token)


def run_test(config: dict[str, Any], token: str) -> dict[str, Any]:
    """The cheap check against the site. Typed refusals; nothing is stored."""
    if not token:
        raise DocsConnectionError("invalid_request", "Confluence needs its API token to connect.")
    try:
        return client_for_config(config, token).test()
    except Exception as error:  # noqa: BLE001 — every failure becomes a typed refusal
        raise explain(error) from None


def stored_token(db: Connection, app_settings: Any, row: dict[str, Any]) -> str:
    ref = row["configuration"].get("secret_ref_id")
    if not ref:
        return ""
    from .secret_backends import resolve_secret_ref

    return resolve_secret_ref(db, str(ref), app_settings,
                              project_id=str(row["home_project_id"])) or ""


def store(db: Connection, app_settings: Any, *, project_id: str, config: dict[str, Any],
          token: str, outcome: dict[str, Any], actor: str,
          default_spaces: Optional[list[str]] = None) -> dict[str, Any]:
    """Write the (already tested) connection: the token to the vault, the row keeps its reference.
    Reconnecting keeps the chosen spaces and the sync state."""
    from .secret_backends import default_secret_backend
    from .secrets_service import put_secret

    org = org_of(db, project_id)
    if org is None:
        raise DocsConnectionError("not_found", "That project is not in an organisation.", 404)
    existing = get_row(db, project_id, include_disconnected=True)
    previous = existing["configuration"] if existing else {}
    merged = {k: v for k, v in config.items() if k not in TOKEN_KEYS}
    for keep in ("default_spaces", "knowledge_spaces", "sync_states", "sync_jobs"):
        if previous.get(keep) is not None:
            merged[keep] = previous[keep]
    if default_spaces is not None:
        merged["default_spaces"] = _spaces(default_spaces)
    written = put_secret(db, app_settings, project_id=project_id, name="docs_confluence_token",
                         value=token, secret_kind="api_token",
                         backend=default_secret_backend(app_settings), actor=actor)
    merged.update(secret_ref_id=written["secret_ref_id"], last4=written.get("last4"),
                  fingerprint=_fingerprint(token),
                  acts_as=outcome.get("account") or previous.get("acts_as"))
    if existing:
        db.execute(text(
            "UPDATE docs_connections SET status = 'active', home_project_id = :h, "
            "configuration = CAST(:c AS JSONB), last_tested_at = now(), last_test_ok = true, "
            "updated_at = now() WHERE id = :i"),
            {"h": project_id, "c": json.dumps(merged), "i": existing["id"]})
        row_id = existing["id"]
    else:
        row_id = make_id("dcn")
        db.execute(text(
            "INSERT INTO docs_connections (id, org_id, home_project_id, provider_key, status, "
            "configuration, created_by, last_tested_at, last_test_ok) VALUES (:i, :o, :h, :k, "
            "'active', CAST(:c AS JSONB), :a, now(), true)"),
            {"i": row_id, "o": org, "h": project_id, "k": PROVIDER, "c": json.dumps(merged),
             "a": actor})
    emit_event(db, project_id, "docs_connection.connected", actor, "docs_connection", row_id,
               {"provider": PROVIDER, "site": merged["base_url"], "account": merged.get("acts_as"),
                "credential_from": merged.get("credential_from")})
    return get_row(db, project_id, include_disconnected=True)  # type: ignore[return-value]


def record_test(db: Connection, row: dict[str, Any], ok: bool, actor: str,
                account: Optional[str] = None) -> None:
    db.execute(text("UPDATE docs_connections SET last_tested_at = now(), last_test_ok = :ok, "
                    "configuration = CASE WHEN CAST(:a AS TEXT) IS NULL THEN configuration ELSE "
                    "configuration || jsonb_build_object('acts_as', CAST(:a AS TEXT)) END, "
                    "updated_at = now() WHERE id = :i"),
               {"ok": ok, "a": account, "i": row["id"]})
    emit_event(db, row["home_project_id"], "docs_connection.tested", actor, "docs_connection",
               row["id"], {"ok": ok})


def update_settings(db: Connection, row: dict[str, Any], *, actor: str,
                    default_spaces: Optional[list[str]] = None,
                    knowledge_spaces: Optional[list[str]] = None) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if default_spaces is not None:
        patch["default_spaces"] = _spaces(default_spaces)
    if knowledge_spaces is not None:
        patch["knowledge_spaces"] = _spaces(knowledge_spaces)
    if not patch:
        raise DocsConnectionError("invalid_request", "Nothing to change: give default_spaces or "
                                                     "knowledge_spaces.")
    db.execute(text("UPDATE docs_connections SET configuration = configuration || "
                    "CAST(:p AS JSONB), updated_at = now() WHERE id = :i"),
               {"p": json.dumps(patch), "i": row["id"]})
    emit_event(db, row["home_project_id"], "docs_connection.settings_changed", actor,
               "docs_connection", row["id"], {"changed": sorted(patch)})
    return dict(row, configuration={**row["configuration"], **patch})


def disconnect(db: Connection, row: dict[str, Any], actor: str, app_settings: Any = None) -> None:
    """Disconnect: the row stays (spaces and sync state are kept for a reconnect), with its token
    reference removed AND the vault secret this connection stored revoked and deleted (a copy of
    Jira's credential is its own entry; Jira's is never touched). Knowledge already synced stays."""
    from .ticket_intake import _retire_secret_ref

    _retire_secret_ref(db, app_settings, str(row["configuration"].get("secret_ref_id") or ""),
                       actor=actor)
    db.execute(text(
        "UPDATE docs_connections SET status = 'disconnected', configuration = configuration "
        "- 'secret_ref_id' - 'last4' - 'fingerprint' - 'acts_as', last_test_ok = NULL, "
        "updated_at = now() WHERE id = :i"), {"i": row["id"]})
    emit_event(db, row["home_project_id"], "docs_connection.disconnected", actor,
               "docs_connection", row["id"], {"provider": row["provider_key"]})


# ---- the client every reader uses -------------------------------------------------------------


def client_for(db: Connection, app_settings: Any, project_id: str) -> tuple[dict[str, Any], ConfluenceClient]:
    """(row, a client) for the org's Confluence, as ``project_id`` reaches it. Raises
    :class:`ConfluenceNotConnected` ("Connect Confluence in Setup › Connections") when there is
    no connection or it holds no credential."""
    row = get_row(db, project_id)
    if not is_connected(row):
        raise ConfluenceNotConnected(NOT_CONNECTED, provider=PROVIDER)
    assert row is not None
    # The token is resolved HERE, once, so the caller can close its transaction before any HTTP.
    token = stored_token(db, app_settings, row)
    return row, ConfluenceClient(row["configuration"], lambda: token)
