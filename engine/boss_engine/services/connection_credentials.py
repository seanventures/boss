"""Whose credential an MCP server or a Kit connection call carries (BOSSAPP-847).

WHY THIS EXISTS
---------------
An MCP server had ONE sign-in: a bearer token or a header in `mcp_servers.auth`, shared by every
agent and every person. A hosted Linear, Atlassian, Notion or Sentry server is somebody's
ACCOUNT: an agent filing a ticket "as Alice" must carry Alice's sign-in, an agent with its own
service account must carry its own, and a team that shares one bot account keeps doing that.

THE MODEL
---------
* `credential_policy` on the server (or a Kit connection's stored config) says which:

    shared             the server's one sign-in (its static auth, or the org's OAuth sign-in)
    per_user           the acting PERSON's own credential; nothing else
    per_user_fallback  the acting person's own when they have one AND the caller may lend it,
                       else the shared one
    per_agent          the calling AGENT's own credential; nothing else

* `connection_credentials` holds one row per (server or connection, subject); the secret is a
  vault item (identity/vault.py, provider `mcp:<server id>` / `kit:<ability>`), never a column.
* THE RUN'S PRINCIPAL decides, never a request field: the identity context this code runs under
  (a run token acting for a person, or the person's own session), else the agent named by
  :func:`acting_agent` for an in-process agent run. An AGENT may carry its person's credential
  only when the person's delegation grant says so (identity grants: ``resources.mcp.as_user``,
  optionally narrowed to ``servers``) — the same "act as me on <provider>" rule GitHub uses.

The resolution is ONE function (:func:`resolve`) that `mcp_registry.auth_headers`, the Kit's
connection reads and the console's "whose credential would this use" answer all go through.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

POLICIES = ("shared", "per_user", "per_user_fallback", "per_agent")
SUBJECT_KINDS = ("org", "user", "agent")

#: The delegation-grant resource that lets an agent carry its person's credential.
GRANT_RESOURCE = "mcp"
KIT_GRANT_RESOURCE = "kit"


class CredentialError(ValueError):
    """Something a person can act on, said in their words. `status` is the HTTP answer."""

    status = 422


class CredentialUnavailable(CredentialError):
    """No credential this caller may use under the server's policy (409: connect one)."""

    status = 409


class CredentialNeedsReauth(CredentialUnavailable):
    """The credential exists but its sign-in expired or was revoked: Reconnect."""


# --------------------------------------------------------------------------- #
# The run's principal                                                           #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CredentialPrincipal:
    """Who a call is for. `person_id` is a usr_ principal; `agent_role` the calling agent's role
    key (agent_id its Passport id). `grant_id` is the delegation grant an agent acts under."""

    person_id: Optional[str] = None
    agent_role: Optional[str] = None
    agent_id: Optional[str] = None
    grant_id: Optional[str] = None

    @property
    def via_agent(self) -> bool:
        return bool(self.agent_role or self.agent_id)

    def describe(self) -> str:
        if self.via_agent and self.person_id:
            return f"{self.agent_role or self.agent_id} for {self.person_id}"
        if self.via_agent:
            return str(self.agent_role or self.agent_id)
        return str(self.person_id or "nobody")


_ACTING_AGENT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "boss_mcp_acting_agent", default=None)


@contextlib.contextmanager
def acting_agent(role_key: Optional[str]) -> Iterator[None]:
    """Name the agent role an in-process agent run calls tools as (agent_loop wraps its drive in
    this), for the runs that carry no run token: a per_agent server then finds its credential."""
    token = _ACTING_AGENT.set(str(role_key).strip().lower() if role_key else None)
    try:
        yield
    finally:
        _ACTING_AGENT.reset(token)


def _role_of_agent(connection: Connection, project_id: str, agent_id: str) -> Optional[str]:
    try:
        with connection.begin_nested():
            found = connection.execute(text(
                "SELECT role_key FROM roles WHERE agent_id = :a "
                "ORDER BY (project_id = :p) DESC LIMIT 1"), {"a": agent_id, "p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — an install whose roles predate agent ids
        return None
    return str(found) if found else None


def principal_from_context(connection: Connection, project_id: str) -> CredentialPrincipal:
    """The principal this code runs under: the verified identity context, else the acting agent.

    * a run token in OBO mode: the person (sub) and the current agent of its chain, with its grant;
    * an autonomous run token: the agent alone;
    * an always-on launch: the approving person, as an agent with no grant (cannot lend);
    * a person's own session or key: the person, no agent (they use their own credential);
    * nothing verified: only the agent :func:`acting_agent` named, if any.
    """
    from ..identity import lineage
    from ..identity.principal import PrincipalKind, RunMode

    role = _ACTING_AGENT.get()
    ctx = lineage.current_context()
    if ctx is None:
        return CredentialPrincipal(agent_role=role)
    rt = ctx.run_token
    if rt is not None:
        current = rt.chain.current
        agent_id = current.agent_id if current is not None else (
            rt.sub if str(rt.sub).startswith("agt_") else None)
        person = rt.sub if rt.mode == RunMode.OBO and str(rt.sub).startswith("usr_") else None
        if agent_id and not role:
            role = _role_of_agent(connection, project_id, agent_id)
        return CredentialPrincipal(person_id=person, agent_role=role, agent_id=agent_id,
                                   grant_id=rt.grant_id)
    if ctx.ambient is not None:
        return CredentialPrincipal(person_id=ctx.on_behalf_of, agent_role=role or "ambient")
    if ctx.principal.kind == PrincipalKind.USER:
        return CredentialPrincipal(person_id=ctx.principal.id, agent_role=role)
    return CredentialPrincipal(agent_role=role)


def may_lend(connection: Connection, principal: CredentialPrincipal, *,
             resource: str = GRANT_RESOURCE, target: str = "") -> bool:
    """May this caller carry `principal.person_id`'s own credential for `target`?

    The person themself always may. An agent may only under a LIVE delegation grant from that
    same person whose resources say ``{<resource>: {as_user: true}}`` (optionally
    ``servers: [<id>, ...]`` naming which), or ``{"<resource>:<id>": {as_user: true}}``."""
    if not principal.person_id:
        return False
    if not principal.via_agent:
        return True
    if not principal.grant_id:
        return False
    from ..identity import grants

    grant = grants.load(principal.grant_id, db=connection)
    if not grants.is_live(grant) or grant.user_principal_id != principal.person_id:
        return False
    resources = grant.resources or {}
    broad = resources.get(resource) if isinstance(resources.get(resource), dict) else {}
    if broad.get("as_user") is True:
        named = broad.get("servers")
        if not named or target in {str(x) for x in named}:
            return True
    narrow = resources.get(f"{resource}:{target}")
    return isinstance(narrow, dict) and narrow.get("as_user") is True


# --------------------------------------------------------------------------- #
# Rows                                                                          #
# --------------------------------------------------------------------------- #

def org_of_project(connection: Connection, project_id: str) -> str:
    found = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()
    if not found:
        raise CredentialError("that project has no organisation.")
    return str(found)


def _target(server_id: Optional[str], connection_id: Optional[str]) -> tuple[str, str]:
    if bool(server_id) == bool(connection_id):
        raise CredentialError("name exactly one of an MCP server or a Kit connection.")
    return ("server_id", str(server_id)) if server_id else ("connection_id", str(connection_id))


def provider_for(server_id: Optional[str] = None, connection_id: Optional[str] = None) -> str:
    """The vault provider a credential is stored under (its AAD binds it there)."""
    column, value = _target(server_id, connection_id)
    return f"mcp:{value}" if column == "server_id" else str(value)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else value


def _public(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "server_id": row["server_id"], "connection_id": row["connection_id"],
            "subject_kind": row["subject_kind"], "subject_id": row["subject_id"],
            "kind": row["kind"], "connected_as": row["connected_as"],
            "scopes": list(row["scopes"] or []), "status": row["status"],
            "created_by": row["created_by"], "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"])}


_COLUMNS = ("id, project_id, server_id, connection_id, subject_kind, subject_id, vault_item_id, "
            "kind, connected_as, scopes, status, created_by, created_at, updated_at")


def find(connection: Connection, project_id: str, *, server_id: Optional[str] = None,
         connection_id: Optional[str] = None, subject_kind: str, subject_id: str
         ) -> Optional[dict[str, Any]]:
    column, value = _target(server_id, connection_id)
    row = connection.execute(text(
        f"SELECT {_COLUMNS} FROM connection_credentials WHERE project_id = :p AND {column} = :t "
        "AND subject_kind = :k AND subject_id = :s AND status <> 'revoked'"),
        {"p": project_id, "t": value, "k": subject_kind, "s": subject_id}).mappings().first()
    return dict(row) if row else None


def list_credentials(connection: Connection, project_id: str, *, server_id: Optional[str] = None,
                     connection_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every live credential of one server or connection, as a screen may see it (no secret)."""
    column, value = _target(server_id, connection_id)
    rows = connection.execute(text(
        f"SELECT {_COLUMNS} FROM connection_credentials WHERE project_id = :p AND {column} = :t "
        "AND status <> 'revoked' ORDER BY subject_kind, subject_id"),
        {"p": project_id, "t": value}).mappings().all()
    return [_public(r) for r in rows]


def _set_server_auth_status(connection: Connection, server_id: Optional[str],
                            status: Optional[str]) -> None:
    from .mcp_registry import set_auth_status

    set_auth_status(connection, server_id, status)


def store(connection: Connection, project_id: str, *, server_id: Optional[str] = None,
          connection_id: Optional[str] = None, subject_kind: str, subject_id: str, secret: str,
          actor: str, kind: str = "oauth", refresh: Optional[str] = None,
          access_expires_at: Optional[datetime] = None, connected_as: Optional[str] = None,
          scopes: Optional[list[str]] = None) -> dict[str, Any]:
    """Keep one subject's credential: the secret goes to the vault, this row says whose it is.

    A subject that already had one gets it REPLACED: the row is updated in place and the old
    vault item revoked (crypto-shredded), so a reconnect never leaves a second usable token."""
    from ..identity import vault

    if subject_kind not in SUBJECT_KINDS:
        raise CredentialError("a credential belongs to the org, a person or an agent.")
    if not str(subject_id or "").strip():
        raise CredentialError("say whose credential this is.")
    if subject_kind == "user" and not str(subject_id).startswith("usr_"):
        raise CredentialError("a person's credential is kept under their BOSS identity.")
    if kind not in ("oauth", "token"):
        raise CredentialError("a credential is an OAuth sign-in or a token.")
    if not str(secret or "").strip():
        raise CredentialError("the credential is empty.")
    org_id = org_of_project(connection, project_id)
    column, value = _target(server_id, connection_id)
    item = vault.store(
        org_id=org_id, owner_principal_id=subject_id if subject_kind == "user" else None,
        kind=vault.VaultItemKind.OAUTH_USER if kind == "oauth"
        else vault.VaultItemKind.PROVIDER_CREDENTIAL,
        provider=provider_for(server_id, connection_id), secret=str(secret).strip(),
        refresh=refresh, access_expires_at=access_expires_at, db=connection,
        provider_subject=(connected_as or None), connection_name=f"{subject_kind}:{subject_id}",
        granted_scopes=scopes or None)
    current = find(connection, project_id, server_id=server_id, connection_id=connection_id,
                   subject_kind=subject_kind, subject_id=subject_id)
    if current is not None:
        row = connection.execute(text(
            f"UPDATE connection_credentials SET vault_item_id = :v, kind = :kind, "
            "connected_as = :as, scopes = :sc, status = 'active', updated_at = now() "
            f"WHERE id = :id RETURNING {_COLUMNS}"),
            {"v": item, "kind": kind, "as": connected_as, "sc": scopes, "id": current["id"]},
        ).mappings().one()
        vault.revoke(current["vault_item_id"], ctx=None, reason="replaced by a new sign-in",
                     db=connection)
    else:
        row = connection.execute(text(
            f"INSERT INTO connection_credentials (id, project_id, {column}, subject_kind, "
            "subject_id, vault_item_id, kind, connected_as, scopes, status, created_by) VALUES "
            "(:id, :p, :t, :k, :s, :v, :kind, :as, :sc, 'active', :by) "
            f"RETURNING {_COLUMNS}"),
            {"id": make_id("ccred"), "p": project_id, "t": value, "k": subject_kind,
             "s": subject_id, "v": item, "kind": kind, "as": connected_as, "sc": scopes,
             "by": actor}).mappings().one()
    if subject_kind == "org":
        _set_server_auth_status(connection, server_id, "connected")
    emit_event(connection, project_id, "connection.credential_connected", actor,
               "connection_credential", row["id"],
               {"server_id": server_id, "connection_id": connection_id,
                "subject_kind": subject_kind, "subject_id": subject_id, "kind": kind,
                "connected_as": connected_as})
    return _public(row)


def forget_server(connection: Connection, project_id: str, server_id: str, *, actor: str
                  ) -> int:
    """A server being removed: every credential of it revoked in the vault (its rows then go
    with the server). Returns how many."""
    ids = connection.execute(text(
        "SELECT id FROM connection_credentials WHERE project_id = :p AND server_id = :s"),
        {"p": project_id, "s": server_id}).scalars().all()
    for credential_id in ids:
        remove(connection, project_id, str(credential_id), actor=actor)
    return len(ids)


def remove(connection: Connection, project_id: str, credential_id: str, *, actor: str
           ) -> Optional[dict[str, Any]]:
    """Disconnect one credential: its vault item revoked and the row gone."""
    from ..identity import vault

    row = connection.execute(text(
        f"DELETE FROM connection_credentials WHERE id = :id AND project_id = :p "
        f"RETURNING {_COLUMNS}"), {"id": credential_id, "p": project_id}).mappings().first()
    if row is None:
        return None
    vault.revoke(row["vault_item_id"], ctx=None, reason="disconnected", db=connection)
    if row["subject_kind"] == "org":
        _set_server_auth_status(connection, row["server_id"], "not_connected")
    emit_event(connection, project_id, "connection.credential_removed", actor,
               "connection_credential", row["id"],
               {"server_id": row["server_id"], "connection_id": row["connection_id"],
                "subject_kind": row["subject_kind"], "subject_id": row["subject_id"]})
    return _public(row)


def mark_needs_reauth(connection: Connection, project_id: str, credential_id: str, *,
                      reason: str) -> None:
    """The credential's sign-in stopped working. Written in its OWN transaction where the
    connection allows one, so the caller's rollback (the failed call) does not un-say it."""
    engine = getattr(connection, "engine", None)
    ctx = engine.begin() if engine is not None and hasattr(engine, "begin") \
        else contextlib.nullcontext(connection)
    with ctx as own:
        row = own.execute(text(
            "UPDATE connection_credentials SET status = 'needs_reauth', updated_at = now() "
            "WHERE id = :id AND project_id = :p AND status = 'active' "
            "RETURNING server_id, subject_kind, subject_id"),
            {"id": credential_id, "p": project_id}).mappings().first()
        if row is None:
            return
        if row["subject_kind"] == "org":
            _set_server_auth_status(own, row["server_id"], "needs_reauth")
        emit_event(own, project_id, "connection.credential_needs_reauth", "system",
                   "connection_credential", credential_id,
                   {"server_id": row["server_id"], "subject_kind": row["subject_kind"],
                    "subject_id": row["subject_id"], "reason": reason[:200]})


def set_policy(connection: Connection, project_id: str, server_id: str, policy: str, *,
               actor: str) -> str:
    """Set an MCP server's credential policy."""
    policy = str(policy or "").strip().lower()
    if policy not in POLICIES:
        raise CredentialError("credential policy must be shared, per_user, per_user_fallback "
                              "or per_agent.")
    from .mcp_registry import set_credential_policy

    if not set_credential_policy(connection, project_id, server_id, policy):
        raise CredentialError("MCP server not found.")
    emit_event(connection, project_id, "mcp.server_credential_policy_set", actor, "mcp_server",
               server_id, {"credential_policy": policy})
    return policy


# --------------------------------------------------------------------------- #
# Resolution                                                                    #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Resolution:
    """Which credential a call will carry. `credential` is None for the server's static shared
    sign-in (or none at all); `subject_kind` says whose it is."""

    subject_kind: str
    subject_id: str
    credential: Optional[dict[str, Any]]


def _candidates(connection: Connection, project_id: str, policy: str,
                principal: CredentialPrincipal, *, resource: str, target: str
                ) -> list[tuple[str, str]]:
    """(subject_kind, subject_id) in the order the policy tries them."""
    org = ("org", org_of_project(connection, project_id))
    if policy == "shared":
        return [org]
    if policy == "per_agent":
        return [("agent", principal.agent_role)] if principal.agent_role else []
    mine = ([("user", principal.person_id)]
            if principal.person_id and may_lend(connection, principal, resource=resource,
                                                target=target) else [])
    return mine + [org] if policy == "per_user_fallback" else mine


def resolve(connection: Connection, project_id: str, *, policy: str,
            principal: CredentialPrincipal, server_id: Optional[str] = None,
            connection_id: Optional[str] = None, static_shared: bool = False) -> Resolution:
    """THE ONE ANSWER to "whose credential does this call carry". Raises CredentialUnavailable
    with the sentence to act on when the policy leaves this caller nothing.

    `static_shared`: the server's own auth column IS its shared sign-in (bearer/header/none), so
    the org candidate resolves to it without a credential row."""
    policy = policy if policy in POLICIES else "shared"
    resource, target = ((GRANT_RESOURCE, str(server_id)) if server_id
                        else (KIT_GRANT_RESOURCE, str(connection_id)))
    tried_reauth: Optional[dict[str, Any]] = None
    for kind, subject in _candidates(connection, project_id, policy, principal,
                                     resource=resource, target=target):
        if kind == "org" and static_shared:
            return Resolution("org", subject, None)
        row = find(connection, project_id, server_id=server_id, connection_id=connection_id,
                   subject_kind=kind, subject_id=subject)
        if row is None:
            continue
        if row["status"] == "needs_reauth":
            tried_reauth = tried_reauth or row
            continue
        return Resolution(kind, subject, row)
    if tried_reauth is not None:
        whose = {"org": "The shared sign-in", "user": "Your sign-in",
                 "agent": "This agent's sign-in"}[tried_reauth["subject_kind"]]
        raise CredentialNeedsReauth(f"{whose} for this server has expired or was revoked. "
                                    "Reconnect it in Skills & MCP.")
    raise CredentialUnavailable(_unavailable_sentence(policy, principal))


def _unavailable_sentence(policy: str, principal: CredentialPrincipal) -> str:
    if policy == "per_agent":
        if not principal.agent_role:
            return ("This server uses each agent's own account, and this call was not made by an "
                    "agent. Ask an agent that holds it.")
        return (f"This server uses each agent's own account, and {principal.agent_role} has none. "
                "Sign it in from the server's drawer in Skills & MCP.")
    if policy in ("per_user", "per_user_fallback"):
        if not principal.person_id:
            return ("This server uses each person's own account, and this call is not acting "
                    "for a person.")
        if principal.via_agent:
            return ("This server uses each person's own account. The agent may use yours only if "
                    "your delegation grant allows it (act as me on MCP), and you have signed in "
                    "with Use my own account.")
        return "This server uses your own account: press Use my own account to sign in."
    return "This server has no shared sign-in yet: press Sign in in its drawer."


def _bearer_for(connection: Connection, settings: Any, project_id: str, server: dict[str, Any],
                raw_auth: dict[str, Any], row: dict[str, Any]) -> str:
    """The token of one credential row, refreshed when due (OAuth) and marked needs_reauth when
    the provider says it is gone."""
    from ..identity import vault

    provider = provider_for(server_id=str(server.get("id") or ""))
    refresher = None
    if row["kind"] == "oauth":
        from . import mcp_oauth

        refresher = mcp_oauth.refresher(connection, settings, project_id, server, raw_auth)
    try:
        return vault.connection_token(row["vault_item_id"], provider=provider, db=connection,
                                      refresher=refresher)
    except vault.VaultNeedsReauth as gone:
        mark_needs_reauth(connection, project_id, row["id"], reason=gone.code)
        whose = {"org": "The shared sign-in", "user": "Your sign-in",
                 "agent": "This agent's sign-in"}.get(row["subject_kind"], "The sign-in")
        raise CredentialNeedsReauth(f"{whose} for {server.get('name') or 'this server'} has "
                                    "expired or was revoked. Reconnect it in Skills & MCP.") \
            from gone


def headers_for(connection: Connection, settings: Any, project_id: str, server: dict[str, Any],
                raw_auth: dict[str, Any], *, principal: Optional[CredentialPrincipal] = None
                ) -> dict[str, str]:
    """The headers one call to `server` carries, by its credential policy and the run's
    principal. A shared server with static auth is exactly `headers_for_auth` (unchanged)."""
    from .mcp_registry import headers_for_auth

    kind = str(raw_auth.get("type") or "none")
    policy = str(server.get("credential_policy") or _policy_of(connection, server) or "shared")
    static_shared = kind != "oauth"
    if policy == "shared" and static_shared:
        return headers_for_auth(connection, settings, raw_auth)
    who = principal if principal is not None else principal_from_context(connection, project_id)
    found = resolve(connection, project_id, policy=policy, principal=who,
                    server_id=str(server.get("id") or ""), static_shared=static_shared)
    if found.credential is None:
        return headers_for_auth(connection, settings, raw_auth)
    headers = {str(k): str(v) for k, v in (raw_auth.get("headers") or {}).items()}
    token = _bearer_for(connection, settings, project_id, server, raw_auth, found.credential)
    if kind == "header" and raw_auth.get("header_name") and found.credential["kind"] == "token":
        headers[str(raw_auth["header_name"])] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _policy_of(connection: Connection, server: dict[str, Any]) -> Optional[str]:
    try:
        with connection.begin_nested():
            return connection.execute(text(
                "SELECT credential_policy FROM mcp_servers WHERE id = :id"),
                {"id": server.get("id")}).scalar()
    except Exception:  # noqa: BLE001 — an install that predates the column: shared
        return None


def explain(connection: Connection, project_id: str, server: dict[str, Any],
            principal: CredentialPrincipal) -> dict[str, Any]:
    """What a call by `principal` would carry, for the console ("uses: your account")."""
    from .mcp_registry import _raw_auth

    raw = _raw_auth(connection, project_id, str(server.get("id") or ""))
    try:
        found = resolve(connection, project_id,
                        policy=str(server.get("credential_policy") or "shared"),
                        principal=principal, server_id=str(server.get("id") or ""),
                        static_shared=str(raw.get("type") or "none") != "oauth")
    except CredentialUnavailable as unavailable:
        return {"ok": False, "reason": str(unavailable),
                "needs_reauth": isinstance(unavailable, CredentialNeedsReauth)}
    return {"ok": True, "subject_kind": found.subject_kind, "subject_id": found.subject_id}


# --------------------------------------------------------------------------- #
# Kit connections (the same policy for a Kit ability's token)                   #
# --------------------------------------------------------------------------- #

def kit_connection_id(key: str) -> str:
    return f"kit:{str(key).strip().lower()}"


def kit_secret(connection: Connection, settings: Any, project_id: str, key: str, *,
               policy: Optional[str], shared: Optional[str],
               principal: Optional[CredentialPrincipal] = None) -> str:
    """A Kit connection's token for this call: the shared one stored with the connection (the
    resolved `shared`), or the acting person's / agent's own under the connection's policy."""
    from ..identity import vault

    policy = str(policy or "shared")
    if policy == "shared":
        return shared or ""
    who = principal if principal is not None else principal_from_context(connection, project_id)
    cid = kit_connection_id(key)
    found = resolve(connection, project_id, policy=policy, principal=who, connection_id=cid,
                    static_shared=True)
    if found.credential is None:
        return shared or ""
    try:
        return vault.connection_token(found.credential["vault_item_id"], provider=cid,
                                      db=connection)
    except vault.VaultNeedsReauth as gone:
        mark_needs_reauth(connection, project_id, found.credential["id"], reason=gone.code)
        raise CredentialNeedsReauth("That sign-in has expired or was revoked. Connect it "
                                    "again in Skills & MCP.") from gone


def set_kit_policy(connection: Connection, project_id: str, key: str, policy: str, *,
                   actor: str) -> str:
    """A Kit connection's credential policy, kept in its stored config (project_configurations)."""
    from . import kit_connections

    policy = str(policy or "").strip().lower()
    if policy not in POLICIES:
        raise CredentialError("credential policy must be shared, per_user, per_user_fallback "
                              "or per_agent.")
    config_key = kit_connections.CREDENTIAL_POLICY_KEYS.get(str(key))
    if config_key is None:
        raise CredentialError("that Kit connection keeps one shared credential.")
    body = kit_connections._config(connection, project_id, config_key)
    if body is None:
        raise CredentialError("connect it first, then choose whose credential it uses.")
    kit_connections._put_config(connection, project_id, config_key,
                                {**body, "credential_policy": policy}, actor)
    emit_event(connection, project_id, "kit.credential_policy_set", actor, "kit_ability",
               str(key), {"credential_policy": policy})
    return policy
