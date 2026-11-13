"""The MCP server model: registration, editing, reachability, references and removal.

An MCP server is where a connector's tools live — a third-party account, a data source — and
BOSS reaches it over JSON-RPC. This module is the one owner of `mcp_servers`; every screen that
lists servers (Skills and MCP, Capabilities, System Configuration), the custom-step builder, the
agent loop and the chat tools read and write through it, so they cannot disagree about what a
server is, whether it is reachable, or whether it may be removed.

WHAT CHANGED, AND THE INCIDENTS BEHIND IT
-----------------------------------------
* A server could be registered and switched on or off, and nothing else. Its URL, name, tier,
  per-tool tiers and review state could not be changed without deleting it — which silently
  broke every custom step bound to it (ON DELETE SET NULL). :func:`update_mcp_server` edits in
  place and keeps the id everything points at.
* Names were not unique. A double-pressed Register made two servers with one name, and the
  agent loop namespaces tools by the server's NAME, so the second silently overwrote the first.
  Names are now unique per project, case-insensitively, in code and in the schema.
* Delete checked nothing. Authored skills kept a dangling server id and custom steps lost
  theirs. :func:`delete_mcp_server` now refuses while anything points at the server and names
  what does.
* The three servers BOSS seeds (127.0.0.1:8765/8770/8771) are not launched by the community
  compose, so on boss-1 every one refused connections — and every screen showed them as
  enabled and healthy. Reachability is now RECORDED (`reachable`, `last_checked_at`,
  `last_error`, and the last tool list) by every probe — the builder, the Capabilities screen,
  the agent loop and an explicit check — and shown with a sentence saying what to do.
* A re-seed undid the operator: `ensure_mcp_server` overwrote an edited URL and recreated a
  server the operator had deleted. Seeding now respects both (see :func:`ensure_mcp_server`);
  a deletion is remembered in seed_exclusions, the record shared with every other seed path.
* What points at a server is declared once, in :data:`MCP_REFERRERS`; a test fails when the
  schema grows a referrer that list does not name.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, ContextManager, Optional
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .argument_binding import binding_holds
from .core import create_blocking_decision, emit_event, make_id, normalize_data_boundary
from .tool_call_audit import acting_principal, record_tool_call
from .tool_catalog import (
    HASH_VERSION,
    QUARANTINE_KEYS,
    apply_quarantine,
    carry_quarantine,
    changed_tool_decisions,
    compare_catalog,
    description_sha256,
    is_quarantined,
    review_item,
    schema_sha256,
)
from .tool_result_screen import screen_result


_MCP_COLUMNS = (
    "id, name, url, transport, risk_tier, data_boundary, enabled, description, "
    "publisher, provenance, tool_risk_overrides, created_at, updated_at, reachable, "
    "last_checked_at, last_error, tools, auth, tool_policy"
)

#: How long a recorded probe is good for before a screen asks the server again. Short, because
#: an operator who has just started a server expects the next page load to see it — and long
#: enough that a builder opened twice in a row does not wait on a dead server twice.
REACHABILITY_FRESH_SECONDS = 60

#: How long one probe may take. The builder used to wait 6 seconds PER SERVER on every load.
PROBE_TIMEOUT_SECONDS = 4.0


class McpServerError(ValueError):
    """Something the operator can fix, phrased for them. The API answers 422."""


class McpServerConflict(McpServerError):
    """The name is taken. The API answers 409."""


class McpServerInUse(McpServerError):
    """Something still points at the server. The API answers 409."""


def _validated_risk(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 3:
        raise McpServerError("risk_tier must be an integer between 0 and 3")
    return value


def _validated_overrides(value: Optional[dict[str, int]]) -> dict[str, int]:
    overrides = value or {}
    if not isinstance(overrides, dict) or any(
        not isinstance(name, str)
        or not name.strip()
        or isinstance(risk, bool)
        or not isinstance(risk, int)
        or risk < 0
        or risk > 3
        for name, risk in overrides.items()
    ):
        raise McpServerError("tool risk overrides must map tool names to integers 0-3")
    return {name.strip(): risk for name, risk in overrides.items()}


def _validated_server_identity(name: str, url: str, transport: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise McpServerError("MCP server name must be a non-empty string")
    if not isinstance(transport, str) or not transport.strip():
        raise McpServerError("MCP transport must be a non-empty string")
    if transport.strip() == "stdio" or (isinstance(url, str) and url.startswith("stdio://")):
        # A STDIO SERVER (BOSSAPP-850): its address names an operator's launch template; the
        # supervisor bridges it to HTTP at call time (mcp_stdio_supervisor). That the template
        # exists in the server's own project is checked by _validated_stdio_target.
        from .mcp_stdio_templates import parse_stdio_url

        if transport.strip() != "stdio":
            raise McpServerError("a stdio:// URL needs transport 'stdio'")
        if parse_stdio_url(url) is None:
            raise McpServerError(
                "a stdio server's URL is stdio://<project_id>/<template name>")
        return
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise McpServerError(
            "MCP server url must be an absolute HTTP(S) URL, e.g. http://web-intel:8770/mcp")


def _validated_stdio_target(connection: Connection, project_id: str, url: str,
                            transport: str) -> None:
    """A stdio server's address must name a template of its OWN project (BOSSAPP-850)."""
    if str(transport or "").strip() != "stdio":
        return
    from .mcp_stdio_templates import StdioTemplateError, require_template_for_url

    try:
        require_template_for_url(connection, project_id, url)
    except StdioTemplateError as error:
        raise McpServerError(str(error)) from error


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row(r) -> dict[str, Any]:
    """One server as every screen sees it. Columns beyond the identifying ones are optional, so a
    row read by an older or narrower SELECT still renders rather than raising KeyError."""
    keys = set(r.keys()) if hasattr(r, "keys") else set()

    def col(name: str, default: Any = None) -> Any:
        return r[name] if (not keys or name in keys) else default

    tools = _loads(col("tools"), [])
    reachable = col("reachable")
    item = {
        "id": r["id"],
        "name": r["name"],
        "url": r["url"],
        "transport": col("transport", "http"),
        "risk_tier": int(r["risk_tier"]),
        "data_boundary": col("data_boundary", "internal"),
        "enabled": bool(col("enabled", True)),
        "description": col("description"),
        "publisher": _loads(col("publisher"), {}) or {},
        "provenance": _loads(col("provenance"), {}) or {},
        "tool_risk_overrides": _loads(col("tool_risk_overrides"), {}) or {},
        "created_at": _iso(col("created_at")),
        "updated_at": _iso(col("updated_at")),
        # None = never checked. Kept distinct from False: "we have not looked" and "we looked
        # and it was not there" call for different sentences.
        "reachable": None if reachable is None else bool(reachable),
        "last_checked_at": _iso(col("last_checked_at")),
        "last_error": col("last_error"),
        "tools": tools if isinstance(tools, list) else [],
        # HOW BOSS SIGNS IN TO IT, without the secret: the kind, the header and whether one is
        # set (with its last four, which tell two tokens apart and are not one). The secret
        # itself lives in the secret store and is resolved to a header only at call time
        # (:func:`auth_headers`). Never the ref id either: a list response names nothing a
        # reader could resolve.
        "auth": public_auth(_loads(col("auth"), {}) or {}),
        # {tool: allow|ask|off} as the operator set it; :func:`tool_policy` applies defaults.
        "tool_policy": _loads(col("tool_policy"), {}) or {},
    }
    # The same answer the agent loop, the catalogue and resolve_risk get (reviewed), so a badge
    # cannot say "unreviewed" about a server the runtime treats as reviewed.
    item["trusted"] = reviewed(item)
    item["status_note"] = status_note(item)
    return item


def tool_tier(server: dict[str, Any], tool_name: str, declared: Any = None) -> int:
    """The tier a server declares for one of its tools: the per-tool override, else the server's
    blanket tier, else the approval floor.

    THE ONE PLACE THIS RULE LIVES. A step bound straight to the tool (resolve_risk), an authored
    mcp_tool skill (skill_catalogue), the builder's picker and the chat's server list all read
    it here, so the same tool cannot be tier 0 on one screen and tier 2 on another.
    `server["risk_tier"] or FLOOR` would be wrong — tier 0 is falsy, and every read-only server
    would be promoted to the floor.

    `declared` is what the TOOL says about itself (an MCP annotation). It may raise the tier and
    never lower it: the agent loop used to let it replace the operator's blanket tier, so a
    server could mark its own tools tier 0 and walk past an approval the operator had set.
    An operator's per-tool override is the one thing that may go lower than the blanket tier.
    """
    from .workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR

    def _tier(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, min(int(value), 3))
        except (TypeError, ValueError):
            return None

    overrides = _loads(server.get("tool_risk_overrides"), {}) or {}
    chosen = _tier(overrides.get(tool_name)) if isinstance(overrides, dict) else None
    if chosen is not None:
        return chosen
    blanket = _tier(server.get("risk_tier"))
    base = MANUAL_APPROVAL_RISK_FLOOR if blanket is None else blanket
    claimed = _tier(declared)
    return base if claimed is None else max(base, claimed)


# --------------------------------------------------------------------------- #
# Auth: how BOSS signs in to a server (skills-mcp spec E2)                       #
# --------------------------------------------------------------------------- #

#: The kinds of sign-in a server may need. OAuth is later (spec §7): BOSS's agents run headless.
AUTH_TYPES = ("none", "bearer", "header")

#: Header names a server may NOT be given through `auth`: BOSS sets these itself.
_RESERVED_HEADERS = {"accept", "content-type", "mcp-session-id", "mcp-protocol-version",
                     "content-length", "host"}


def public_auth(auth: dict[str, Any]) -> dict[str, Any]:
    """The auth a screen may see: kind, header, whether a secret is set and its last four."""
    kind = str(auth.get("type") or "none")
    return {
        "type": kind if kind in AUTH_TYPES else "none",
        "header_name": auth.get("header_name") or None,
        "set": bool(auth.get("secret_ref_id")),
        "last4": auth.get("last4") if auth.get("secret_ref_id") else None,
        "headers": {str(k): str(v) for k, v in (auth.get("headers") or {}).items()},
    }


def _validated_auth(auth: Optional[dict[str, Any]]) -> dict[str, Any]:
    """{type, header_name?, secret?, headers?} → a checked copy. The secret is kept only long
    enough to be written to the store by :func:`_stored_auth`."""
    if auth is None:
        return {"type": "none"}
    if not isinstance(auth, dict):
        raise McpServerError("auth must be {type: none | bearer | header, ...}.")
    kind = str(auth.get("type") or "none").strip().lower()
    if kind not in AUTH_TYPES:
        raise McpServerError("auth type must be none, bearer or header.")
    out: dict[str, Any] = {"type": kind}
    if kind == "header":
        name = str(auth.get("header_name") or "").strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) \
                or name.lower() in _RESERVED_HEADERS:
            raise McpServerError("a header sign-in needs a header name, e.g. X-API-Key.")
        out["header_name"] = name
    secret = auth.get("secret")
    if secret is not None and not isinstance(secret, str):
        raise McpServerError("the token must be text.")
    if isinstance(secret, str) and secret.strip():
        if kind == "none":
            raise McpServerError("choose Bearer token or Header to give the server a token.")
        out["secret"] = secret.strip()
    static = auth.get("headers") or {}
    if not isinstance(static, dict):
        raise McpServerError("headers must map a header name to a value.")
    clean: dict[str, str] = {}
    for key, value in static.items():
        name = str(key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) or name.lower() in _RESERVED_HEADERS \
                or name.lower() == "authorization":
            raise McpServerError(f"header {name!r} cannot be set on an MCP server.")
        clean[name] = str(value or "")[:200]
    if clean:
        out["headers"] = clean
    return out


def _stored_auth(connection: Connection, settings: Any, project_id: str, name: str,
                 auth: dict[str, Any], *, actor: str,
                 keep: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """What goes in the `auth` column: the secret written THROUGH to the store and replaced by
    its reference and last four. `keep` is the current column, so an edit that does not retype
    the token keeps the one already stored (the same courtesy the email form gives)."""
    stored = {k: v for k, v in auth.items() if k != "secret"}
    secret = auth.get("secret")
    if secret:
        from .secret_backends import default_secret_backend
        from .secrets_service import put_secret

        settings = settings if settings is not None else _settings_now()
        written = put_secret(connection, settings, project_id=project_id,
                             name=f"mcp_{name}_token", value=secret, secret_kind="api_token",
                             backend=default_secret_backend(settings), actor=actor)
        stored["secret_ref_id"] = written["secret_ref_id"]
        stored["last4"] = written.get("last4") or (secret[-4:] if len(secret) >= 8 else None)
    elif keep and keep.get("secret_ref_id") and stored["type"] != "none":
        stored["secret_ref_id"] = keep["secret_ref_id"]
        stored["last4"] = keep.get("last4")
    return stored


def _settings_now() -> Any:
    from ..config import get_settings

    return get_settings()


def _raw_auth(connection: Connection, project_id: str, server_id: str) -> dict[str, Any]:
    try:
        found = connection.execute(
            text("SELECT auth FROM mcp_servers WHERE id = :id AND project_id = :p"),
            {"id": server_id, "p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — an install that predates the column has no auth
        return {}
    return _loads(found, {}) or {}


def headers_for_auth(connection: Connection, settings: Any, auth: dict[str, Any]
                     ) -> dict[str, str]:
    """The request headers one stored auth means, the secret resolved from the store NOW."""
    headers = {str(k): str(v) for k, v in (auth.get("headers") or {}).items()}
    kind = str(auth.get("type") or "none")
    ref = str(auth.get("secret_ref_id") or "")
    if kind == "none" or not ref:
        return headers
    from .secret_backends import resolve_secret_ref

    secret = resolve_secret_ref(connection, ref, settings if settings is not None
                                else _settings_now())
    if kind == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif kind == "header" and auth.get("header_name"):
        headers[str(auth["header_name"])] = secret
    return headers


def auth_headers(connection: Connection, settings: Any, project_id: str,
                 server: dict[str, Any]) -> dict[str, str]:
    """The headers a call to `server` carries. Empty for a server that needs no sign-in.

    A token that cannot be resolved raises: calling a server that needs one without it gets a
    401 that reads like the server's fault."""
    return headers_for_auth(connection, settings,
                            _raw_auth(connection, project_id, str(server.get("id") or "")))


# --------------------------------------------------------------------------- #
# Tool policy: Allow / Ask / Off per tool (skills-mcp spec E7)                    #
# --------------------------------------------------------------------------- #

POLICIES = ("allow", "ask", "off")


class PolicyBelowFloor(McpServerError):
    """A tool that changes the world was set to run without asking. The API answers 409."""


def policy_floor(tier: int) -> Optional[str]:
    """The least a tool of this tier may be set to: tier >= 2 always asks a person."""
    return "ask" if int(tier) >= 2 else None


def default_policy(tier: int) -> str:
    """Reads run (0-1), writes ask (2), the irreversible start off (3)."""
    tier = int(tier)
    return "allow" if tier <= 1 else ("ask" if tier == 2 else "off")


def effective_policy(tier: int, chosen: Optional[str]) -> str:
    """What the runtime does: the operator's choice, never below the floor; else the default."""
    policy = chosen if chosen in POLICIES else default_policy(tier)
    if policy == "allow" and policy_floor(tier) == "ask":
        return "ask"
    return policy


class ToolQuarantined(PolicyBelowFloor):
    """Allow was chosen for a tool whose definition changed and waits for review (BOSSAPP-818).
    A PolicyBelowFloor, so every surface that answers that one 409 answers this one too."""


def checked_policy(tier: int, policy: str, *, quarantined: bool = False) -> str:
    """A policy an operator may set on a tool of this tier, or PolicyBelowFloor.

    A QUARANTINED tool (its schema or description changed since a person last accepted it) may
    not be set to Allow either: the quarantine is lifted only by approving the "Tool changed"
    decision, never by flipping the policy back."""
    if policy not in POLICIES:
        raise McpServerError("policy must be allow, ask or off.")
    if policy == "allow" and policy_floor(tier) == "ask":
        raise PolicyBelowFloor(
            "this tool changes the world, so it always asks a person; choose Ask or Off.")
    if policy == "allow" and quarantined:
        raise ToolQuarantined(
            "this tool's definition changed and waits for review, so it asks a person until the "
            "\"Tool changed\" decision is approved; choose Ask or Off, or answer that decision.")
    return policy


def tool_quarantined(server: dict[str, Any], tool_name: str) -> bool:
    """Whether the server's stored entry for this tool carries an open quarantine."""
    return any(isinstance(t, dict) and t.get("name") == tool_name and is_quarantined(t)
               for t in server.get("tools") or [])


def tool_policy(server: dict[str, Any], tool_name: str, declared: Any = None) -> str:
    """The effective policy of one MCP tool on one server.

    THE ONE STATEMENT every road reads (call_tool_with_receipt, call_tool_unattended, the agent
    loop's offer, code_task_mcp, the Kit, the MCP gateway's tool_policy_verdict), so a
    QUARANTINED tool answers Ask here whatever its stored policy says (BOSSAPP-818): a tool set
    to Allow whose definition changed under it must not keep running unattended. Off stays Off.
    """
    chosen = (server.get("tool_policy") or {}).get(tool_name)
    policy = effective_policy(tool_tier(server, tool_name, declared), chosen)
    if policy == "allow" and tool_quarantined(server, tool_name):
        return "ask"
    return policy


def set_tool_policy(connection: Connection, project_id: str, server_id: str, tool_name: str,
                    policy: str, *, actor: str) -> dict[str, Any]:
    """Allow / Ask / Off for one tool of one server. Refuses Allow below the floor."""
    server = get_mcp_server(connection, project_id, server_id)
    if server is None:
        raise McpServerError("MCP server not found.")
    tool = str(tool_name or "").strip()
    if not tool:
        raise McpServerError("tool is required.")
    declared = next((t.get("annotations", {}).get("risk_tier") for t in server["tools"]
                     if t.get("name") == tool and isinstance(t.get("annotations"), dict)), None)
    checked_policy(tool_tier(server, tool, declared), policy,
                   quarantined=tool_quarantined(server, tool))
    current = dict(server.get("tool_policy") or {})
    current[tool] = policy
    connection.execute(
        text("UPDATE mcp_servers SET tool_policy = CAST(:tp AS JSONB), updated_at = now() "
             "WHERE id = :id AND project_id = :p"),
        {"tp": json.dumps(current), "id": server_id, "p": project_id})
    emit_event(connection, project_id, "mcp.tool_policy_set", actor, "mcp_server", server_id,
               {"tool": tool, "policy": policy})
    return get_mcp_server(connection, project_id, server_id)  # type: ignore[return-value]


def set_tool_policies(connection: Connection, project_id: str, server_id: str,
                      policies: dict[str, str], *, actor: str) -> None:
    """Replace a server's whole policy map (the Kit turning an ability off or back on). The
    floor still holds: the runtime reads it through :func:`effective_policy`."""
    clean = {str(k): v for k, v in (policies or {}).items() if v in POLICIES}
    connection.execute(
        text("UPDATE mcp_servers SET tool_policy = CAST(:tp AS JSONB), updated_at = now() "
             "WHERE id = :id AND project_id = :p"),
        {"tp": json.dumps(clean), "id": server_id, "p": project_id})
    emit_event(connection, project_id, "mcp.tool_policy_set", actor, "mcp_server", server_id,
               {"policies": clean})


def set_seeded_auth(connection: Connection, project_id: str, server_id: str,
                    auth: dict[str, Any], *, actor: str) -> None:
    """Give a server BOSS seeded a sign-in that a CONNECTION decides (web-intel's search key,
    from Skills & MCP › Web search) — by reference, never a value.

    Not :func:`update_mcp_server`: that is a person editing the server, which takes a built-in
    over (provenance "builtin_edited") and stops the release refreshing it. Configuring a search
    key is not taking the server over."""
    stored = {k: v for k, v in (auth or {}).items()
              if k in ("type", "header_name", "secret_ref_id", "last4", "headers")}
    connection.execute(
        text("UPDATE mcp_servers SET auth = CAST(:a AS JSONB), updated_at = now() "
             "WHERE id = :id AND project_id = :p"),
        {"a": json.dumps(stored), "id": server_id, "p": project_id})
    emit_event(connection, project_id, "mcp.server_auth_set", actor, "mcp_server", server_id,
               {"type": stored.get("type"), "set": bool(stored.get("secret_ref_id"))})


def migrate_seeded_server(connection: Connection, project_id: str, server_id: str, *,
                          url: str, risk_tier: int, description: Optional[str],
                          provenance: dict[str, Any]) -> None:
    """Move a server the release seeded (and no person touched) to the release's current
    address and tier (bootstrap.migrate_builtin_mcp_servers). The recorded reachability
    described the old address, so it is forgotten."""
    _validated_server_identity("seeded", url, "http")
    connection.execute(
        text("UPDATE mcp_servers SET url = :url, risk_tier = :risk, description = :d, "
             "provenance = CAST(:prov AS JSONB), reachable = NULL, last_checked_at = NULL, "
             "last_error = NULL, updated_at = now() WHERE id = :id AND project_id = :p"),
        {"url": url, "risk": _validated_risk(risk_tier), "d": description,
         "prov": json.dumps(provenance), "id": server_id, "p": project_id})


class McpToolOff(McpServerError):
    """A tool the operator turned off was called anyway. Not retryable: only a person lifts it."""

    retryable = False


class McpToolDenied(McpToolOff):
    """A tool-policy rule (BOSSAPP-815) denied this call: a deny rule, or a rate limit whose
    window is spent. An McpToolOff, so every road that answers Off answers this the same way."""


class McpArgumentsChanged(McpServerError):
    """The arguments are not the call a person approved. Not retryable."""

    retryable = False


def review_mcp_server(connection: Connection, project_id: str, server_id: str, *,
                      actor: str) -> Optional[dict[str, Any]]:
    """A person vouches for a server: trusted, with who, when, and the URL they looked at.

    The console's one road to "reviewed" (spec §6.5). A later URL change voids it
    (:func:`update_mcp_server`)."""
    from datetime import datetime, timezone

    server = get_mcp_server(connection, project_id, server_id)
    if server is None:
        return None
    provenance = dict(server["provenance"])
    provenance.update({"trusted": True, "reviewed_by": actor,
                       "reviewed_at": datetime.now(timezone.utc).isoformat(),
                       "reviewed_url": server["url"]})
    provenance.pop("review_voided_by_url_change", None)
    connection.execute(
        text("UPDATE mcp_servers SET provenance = CAST(:prov AS JSONB), updated_at = now() "
             "WHERE id = :id AND project_id = :p"),
        {"prov": json.dumps(provenance), "id": server_id, "p": project_id})
    emit_event(connection, project_id, "mcp.server_reviewed", actor, "mcp_server", server_id,
               {"name": server["name"], "url": server["url"]})
    return get_mcp_server(connection, project_id, server_id)


def status_note(server: dict[str, Any]) -> Optional[str]:
    """One sentence on what is wrong with a server and what to do, or None when nothing is."""
    if not server.get("enabled", True):
        return "Turned off: no agent or step can call it until it is turned back on."
    if server.get("reachable") is False:
        return (f"Unreachable from the BOSS API ({server.get('last_error') or 'no answer'}). "
                "Start the server, or edit its URL to an address the API container can reach "
                "— 127.0.0.1 inside the container is the container itself.")
    return None


def get_mcp_server(connection: Connection, project_id: str, server_id: str) -> Optional[dict]:
    row = connection.execute(
        text(f"SELECT {_MCP_COLUMNS} FROM mcp_servers WHERE id = :id AND project_id = :p"),
        {"id": server_id, "p": project_id},
    ).mappings().first()
    return _row(row) if row else None


def find_mcp_server(connection: Connection, project_id: str, ref: str) -> Optional[dict]:
    """A server named by id or by name — what an operator or a chat model actually knows."""
    wanted = str(ref or "").strip()
    if not wanted:
        return None
    row = connection.execute(
        text(f"SELECT {_MCP_COLUMNS} FROM mcp_servers WHERE project_id = :p "
             "AND (id = :ref OR lower(name) = lower(:ref)) ORDER BY (id = :ref) DESC LIMIT 1"),
        {"p": project_id, "ref": wanted},
    ).mappings().first()
    return _row(row) if row else None


def list_mcp_servers(connection: Connection, project_id: str, enabled_only: bool = False) -> list[dict]:
    sql = (
        f"SELECT {_MCP_COLUMNS} "
        "FROM mcp_servers WHERE project_id = :p"
    )
    if enabled_only:
        sql += " AND enabled = true"
    sql += " ORDER BY created_at"
    rows = connection.execute(text(sql), {"p": project_id}).mappings().all()
    return [_row(r) for r in rows]


def reviewed(server: dict[str, Any]) -> bool:
    """Has a person vouched for this server? The servers BOSS ships count as reviewed by the
    release; every other server counts only once somebody marks it trusted.

    THE ONE QUESTION every unattended use asks. Round-1 review of this milestone: a chat model
    could register any URL at tier 0 with no human step, and agent_loop then offered that
    server's tools — and its tool descriptions — to every unbounded run, calling any tool at or
    below the approval threshold without asking. `trusted` only gated PLACING tier >= 2 tools in
    a workflow. Now an unreviewed server's tool never runs from an agent turn without a person's
    approval (agent_loop), and a server registered from chat lands switched off.

    THE FLAG DECIDES, whoever wrote it. Round-2 review: a built-in counted as reviewed because
    its provenance said source "builtin" — whatever `trusted` said — so an operator who
    re-pointed a seeded 127.0.0.1 server at another host, or unticked Reviewed, saw
    "unreviewed" on the panel while the agent loop ran its tools unattended. BOSS seeds its own
    servers with trusted=True, so the release's review is the flag like anybody else's, and
    :func:`update_mcp_server` drops it (and the "builtin" source) when a person re-points one.
    A legacy built-in row written before the flag existed — source "builtin" and no `trusted`
    key at all — still counts, because nobody has touched it since the release seeded it.
    """
    provenance = server.get("provenance") or {}
    if isinstance(provenance, str):
        provenance = _loads(provenance, {}) or {}
    if "trusted" in provenance:
        return provenance.get("trusted") is True
    return provenance.get("source") == "builtin"


class McpServerUnreviewed(McpServerError):
    """A tool on an unreviewed server was about to run with nobody's approval. The API answers
    409; a run records the sentence.

    NOT RETRYABLE. Round-3 review: SkillRuntimeService retried this refusal under the MCP
    spec's retry policy, so the run recorded a second attempt at a call that could not succeed
    until a person acted. The runtime reads ``retryable`` and stops at the first attempt."""

    retryable = False


def unreviewed_sentence(server: dict[str, Any]) -> str:
    """What to tell a person when an unreviewed server's tool is held for them — one wording for
    every road (agent turn, skill, custom step), so the operator learns one fix."""
    return (f"MCP server '{server.get('name') or server.get('id')}' has not been reviewed, so "
            "its tools only run with a person's approval. Check it is the server you meant and "
            "mark it Reviewed on the Skills and MCP page.")


def needs_person(server: Optional[dict[str, Any]]) -> bool:
    """THE RULE, for every road to an MCP tool: a tool on an unreviewed server never runs
    unattended, whatever tier it claims.

    Round-2 review: this was asked only at agent_loop's direct-call site, so the same tool
    reached through an authored ``mcp_tool`` skill, or a custom workflow step bound to it, ran
    on an unreviewed server with nobody asked. Every gate now asks this function — the skill
    catalogue (``requires_person`` on the entry and the SkillSpec), ``resolve_risk`` for custom
    steps, the workflow ``kind: skill`` validator and agent_loop's skill branch — and the effect
    itself (:func:`call_tool_with_receipt`) refuses without an approved decision, so a road
    nobody has thought of yet fails closed with :func:`unreviewed_sentence`.
    """
    return server is not None and not reviewed(server)


def approval_stands(connection: Connection, project_id: str,
                    decision_id: Optional[str],
                    about: Optional[dict[str, Any]] = None) -> bool:
    """Is ``decision_id`` a decision in this project that a person (or the policy acting for
    them) APPROVED — about THIS call? Read, never assumed: a decision id that is pending,
    rejected or from another project is not an approval.

    ``about`` names what the approval has to be for, as fields of the decision's
    recommendation: ``{"run_id": …}`` for an agent run's resume decision,
    ``{"workflow_run_id": …, "step_id": …}`` for a workflow step's gate. Every field must
    match. Round-3 review: this asked only "approved, in this project?", so an unrelated
    approved decision — another run's, another step's — would have released an unreviewed
    server's tool if a caller ever passed the wrong id. With nothing named there is nothing
    to match, and the answer is no — as it is when a named field is blank, because a blank
    run id would otherwise match any decision that lacks one.
    """
    wanted = str(decision_id or "").strip()
    scope = {k: str(v or "").strip() for k, v in (about or {}).items()}
    if not wanted or not scope or not all(scope.values()):
        return False
    row = connection.execute(
        text("SELECT status, recommendation FROM decisions WHERE id = :id AND project_id = :p"),
        {"id": wanted, "p": project_id},
    ).mappings().first()
    if row is None or row["status"] != "approved":
        return False
    recommendation = _loads(row["recommendation"], {}) or {}
    if not isinstance(recommendation, dict):
        return False
    return all(str(recommendation.get(k) or "") == v for k, v in scope.items())


def approved_arguments_match(
    connection: Connection,
    project_id: str,
    decision_id: Optional[str],
    tool_name: str,
    arguments: Any,
) -> bool:
    """True when this call is the one the approval digested.

    A missing decision, or an unstamped (v1) decision with no args_sha256, still
    matches: older approvals did not store a digest, and a caller that does not
    pass one is unchanged. A present digest matches only the same canonical
    arguments. A v2 decision (BOSSAPP-816, argument_binding.binding_holds) fails
    closed: it needs the digest AND a valid server-key signature.
    """
    wanted = str(decision_id or "").strip()
    if not wanted:
        return True
    row = connection.execute(
        text("SELECT recommendation FROM decisions WHERE id = :id AND project_id = :p"),
        {"id": wanted, "p": project_id},
    ).mappings().first()
    if row is None:
        return True
    recommendation = _loads(row["recommendation"], {}) or {}
    if not isinstance(recommendation, dict):
        return True
    # The agent loop binds an MCP hold under the name it OFFERED the model (`tool`, e.g.
    # mcp_github_create_issue) and records the server's own name beside it (`tool_name`); this
    # door is called with the server's name. Check under the bound name when the two name the
    # same tool, else under this call's own name (a different tool then never matches).
    bound_name = tool_name
    offered = recommendation.get("tool")
    if offered and isinstance(offered, str) and recommendation.get("tool_name") == tool_name:
        bound_name = offered
    return binding_holds(bound_name, arguments, recommendation)


def _name_taken(
    connection: Connection, project_id: str, name: str, *, except_id: Optional[str] = None,
) -> Optional[str]:
    row = connection.execute(
        text("SELECT id FROM mcp_servers WHERE project_id = :p AND lower(name) = lower(:n) "
             "AND (CAST(:x AS TEXT) IS NULL OR id <> :x) LIMIT 1"),
        {"p": project_id, "n": name.strip(), "x": except_id},
    ).first()
    return str(row[0]) if row else None


def _name_taken_sentence(name: str) -> str:
    return (f"an MCP server called '{name}' is already registered in this project. Edit that "
            "one, or register this under another name — two servers with one name make their "
            "tools indistinguishable to an agent.")


def _write_unique_name(connection: Connection, name: str, statement: Callable[[], Any]) -> Any:
    """Run a write that may collide on uq_mcp_servers_project_name, and say so in words.

    `_name_taken` is a check-then-write, so two registrations of one name racing each other
    (a double-pressed Register, two tabs) both pass it and the second INSERT hits the unique
    index. Round-2 review: that IntegrityError escaped as a bare 500. Run inside a savepoint so
    the caller's transaction is still usable, and turned into the same McpServerConflict (409)
    the check raises.
    """
    from sqlalchemy.exc import IntegrityError

    savepoint = connection.begin_nested()
    try:
        result = statement()
    except IntegrityError as error:
        savepoint.rollback()
        if "uq_mcp_servers_project_name" in str(error):
            raise McpServerConflict(_name_taken_sentence(name)) from error
        raise
    savepoint.commit()
    return result


def _reviewed(provenance: dict[str, Any], actor: str) -> dict[str, Any]:
    """Record WHO vouched for a server when it is marked trusted.

    `trusted` is what lets a server's tools be placed at tier 2 or above, so it is a review —
    and a review with no reviewer on record is indistinguishable from a flag somebody set.

    Except the servers BOSS ships (source "builtin"): their trust is the release's, already
    recorded as such, and stamping the bootstrap seed as a "reviewer" would claim a person
    looked at them when nobody did.
    """
    if provenance.get("source") == "builtin":
        return provenance
    if provenance.get("trusted") and not provenance.get("reviewed_by"):
        from datetime import datetime, timezone

        return {**provenance, "reviewed_by": actor,
                "reviewed_at": datetime.now(timezone.utc).isoformat()}
    return provenance


def register_mcp_server(
    connection: Connection,
    project_id: str,
    name: str,
    url: str,
    *,
    transport: str = "http",
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
    publisher: Optional[dict[str, Any]] = None,
    provenance: Optional[dict[str, Any]] = None,
    tool_risk_overrides: Optional[dict[str, int]] = None,
    actor: str = "system",
    enabled: bool = True,
    auth: Optional[dict[str, Any]] = None,
    tool_policy: Optional[dict[str, str]] = None,
    settings: Any = None,
) -> dict:
    _validated_server_identity(name, url, transport)
    _validated_stdio_target(connection, project_id, url, transport)
    risk_tier = _validated_risk(risk_tier)
    data_boundary = normalize_data_boundary(data_boundary)
    tool_risk_overrides = _validated_overrides(tool_risk_overrides)
    checked_auth = _validated_auth(auth)
    policies = {str(k): v for k, v in (tool_policy or {}).items() if v in POLICIES}
    name = name.strip()
    if _name_taken(connection, project_id, name):
        raise McpServerConflict(_name_taken_sentence(name))
    stored_auth = _stored_auth(connection, settings, project_id, name, checked_auth,
                               actor=actor)
    server_id = make_id("mcp")
    _write_unique_name(connection, name, lambda: connection.execute(
        text(
            """
            INSERT INTO mcp_servers
              (id, project_id, name, url, transport, risk_tier, data_boundary,
               description, publisher, provenance, tool_risk_overrides, enabled, auth,
               tool_policy)
            VALUES (:id, :p, :name, :url, :transport, :risk, :db, :desc,
                    CAST(:publisher AS JSONB), CAST(:provenance AS JSONB),
                    CAST(:tool_risk_overrides AS JSONB), :enabled, CAST(:auth AS JSONB),
                    CAST(:tool_policy AS JSONB))
            """
        ),
        {
            "id": server_id,
            "p": project_id,
            "name": name,
            "url": url.strip(),
            "transport": transport,
            "risk": int(risk_tier),
            "db": data_boundary,
            "desc": description,
            "publisher": json.dumps(publisher or {}),
            "provenance": json.dumps(_reviewed(dict(provenance or {}), actor)),
            "tool_risk_overrides": json.dumps(tool_risk_overrides or {}),
            "enabled": bool(enabled),
            "auth": json.dumps(stored_auth),
            "tool_policy": json.dumps(policies),
        },
    ))
    # REGISTERED BY HAND, so a deletion recorded earlier no longer stands in its way: the newer
    # decision wins (seed_exclusions.lift).
    if actor != "bootstrap":
        from . import seed_exclusions

        seed_exclusions.lift(connection, project_id, "mcp_server", name)
    emit_event(
        connection,
        project_id,
        "mcp.server_registered",
        actor,
        "mcp_server",
        server_id,
        {"name": name, "url": url, "risk_tier": int(risk_tier)},
    )
    return get_mcp_server(connection, project_id, server_id)  # type: ignore[return-value]


#: The fields an edit may change. Everything a server IS, except its id — the id is what custom
#: steps and authored skills point at, which is the whole reason to edit rather than re-register.
UPDATABLE_FIELDS = ("name", "url", "transport", "description", "risk_tier", "data_boundary",
                    "tool_risk_overrides", "enabled", "trusted", "auth")


def update_mcp_server(
    connection: Connection,
    project_id: str,
    server_id: str,
    *,
    actor: str,
    settings: Any = None,
    **changes: Any,
) -> Optional[dict]:
    """Change a registered server in place. Only the fields given are touched.

    Changing the URL forgets the recorded reachability and tools: they described the old
    address, and showing them against the new one would claim a check that never happened.
    It VOIDS THE REVIEW too, for the same reason: a person vouched for what answered at the old
    address, and a reviewed server re-pointed at another host stayed "reviewed" (round-1
    review). The same edit may review it again by saying ``trusted=True`` explicitly — the
    console's form clears its Reviewed choice when the URL is changed, so that is a deliberate
    second choice — and the new review is stamped with this actor.

    Marking a server trusted records the reviewer. Every edit records `edited_by`, which is
    what tells the next seed that this server is the operator's now (see ensure_mcp_server).
    """
    unknown = sorted(set(changes) - set(UPDATABLE_FIELDS))
    if unknown:
        raise McpServerError(f"cannot change {', '.join(unknown)} on an MCP server.")
    current = get_mcp_server(connection, project_id, server_id)
    if current is None:
        return None
    given = {k: v for k, v in changes.items() if v is not None}
    name = str(given.get("name", current["name"])).strip()
    url = str(given.get("url", current["url"])).strip()
    transport = str(given.get("transport", current["transport"])).strip()
    _validated_server_identity(name, url, transport)
    _validated_stdio_target(connection, project_id, url, transport)
    if name.lower() != current["name"].lower() and _name_taken(
            connection, project_id, name, except_id=server_id):
        raise McpServerConflict(_name_taken_sentence(name))
    risk = _validated_risk(given.get("risk_tier", current["risk_tier"]))
    boundary = normalize_data_boundary(given.get("data_boundary", current["data_boundary"]))
    overrides = _validated_overrides(
        given.get("tool_risk_overrides", current["tool_risk_overrides"]))
    enabled = bool(given.get("enabled", current["enabled"]))
    provenance = dict(current["provenance"])
    url_changed = url != current["url"]
    if provenance.get("source") == "builtin" and (url_changed or "trusted" in given):
        # A PERSON HAS TAKEN OVER A SERVER BOSS SHIPPED. The release vouched for the address
        # it seeded, not for wherever the server points now, and an operator's own Reviewed
        # choice is theirs to record. Round-2 review: "builtin" stayed on the row, so a
        # re-pointed built-in kept the release's review — the panel said unreviewed while the
        # agent loop ran its tools unattended. The seeded address is kept for the record.
        provenance["source"] = "builtin_edited"
        provenance.setdefault("seeded_url", current["url"])
    if url_changed and reviewed({"provenance": provenance}):
        provenance = {k: v for k, v in provenance.items()
                      if k not in ("reviewed_by", "reviewed_at")}
        provenance["trusted"] = False
        provenance["review_voided_by_url_change"] = current["url"]
    if "trusted" in given:
        if bool(given["trusted"]):
            # Recorded when it BECOMES trusted. The edit form sends `trusted` on every save, and
            # re-recording then would hand the review to whoever last fixed a typo.
            if not provenance.get("trusted"):
                provenance = _reviewed({**provenance, "trusted": True, "reviewed_by": None},
                                       actor)
        else:
            provenance = {k: v for k, v in provenance.items()
                          if k not in ("reviewed_by", "reviewed_at")}
            provenance["trusted"] = False
    from datetime import datetime, timezone

    provenance["edited_by"] = actor
    provenance["edited_at"] = datetime.now(timezone.utc).isoformat()
    # A NEW SIGN-IN IS WRITTEN THROUGH TO THE STORE; a form that does not retype the token
    # keeps the one already stored. Changing the kind to `none` drops it.
    current_auth = _raw_auth(connection, project_id, server_id)
    auth = current_auth
    if "auth" in given:
        auth = _stored_auth(connection, settings, project_id, name,
                            _validated_auth(given["auth"]), actor=actor, keep=current_auth)
    row = _write_unique_name(connection, name, lambda: connection.execute(
        text(
            "UPDATE mcp_servers SET name = :name, url = :url, transport = :transport, "
            "description = :desc, risk_tier = :risk, data_boundary = :db, "
            "tool_risk_overrides = CAST(:o AS JSONB), enabled = :enabled, "
            "provenance = CAST(:prov AS JSONB), auth = CAST(:auth AS JSONB), updated_at = now()"
            + (", reachable = NULL, last_checked_at = NULL, last_error = NULL, "
               "tools = '[]'::jsonb" if url_changed else "")
            + f" WHERE id = :id AND project_id = :p RETURNING {_MCP_COLUMNS}"
        ),
        {"name": name, "url": url, "transport": transport,
         "desc": given.get("description", current["description"]),
         "risk": risk, "db": boundary, "o": json.dumps(overrides), "enabled": enabled,
         "prov": json.dumps(provenance), "auth": json.dumps(auth), "id": server_id,
         "p": project_id},
    ).mappings().first())
    changed = sorted(k for k in given if k in UPDATABLE_FIELDS)
    emit_event(
        connection, project_id, "mcp.server_updated", actor, "mcp_server", server_id,
        {"name": name, "changed": changed,
         # RECORDED WHEN IT MOVES, like a custom step's tier: a tier change alters the approval
         # behaviour of every step and skill bound to this server without touching them.
         "risk_tier_before": current["risk_tier"], "risk_tier_after": risk,
         "trusted": bool(provenance.get("trusted", False))},
    )
    return _row(row)


def set_mcp_enabled(connection: Connection, project_id: str, server_id: str, enabled: bool, actor: str = "system") -> Optional[dict]:
    row = connection.execute(
        text(
            "UPDATE mcp_servers SET enabled = :e, updated_at = now() "
            "WHERE id = :id AND project_id = :p "
            f"RETURNING {_MCP_COLUMNS}"
        ),
        {"e": bool(enabled), "id": server_id, "p": project_id},
    ).mappings().first()
    if row is None:
        return None
    emit_event(
        connection, project_id, "mcp.server_toggled", actor, "mcp_server", server_id,
        {"enabled": bool(enabled)},
    )
    return _row(row)


#: EVERY PLACE THAT POINTS AT AN MCP SERVER, declared once.
#:
#: Each entry is (table, the column that names the server, the key of its list in
#: :func:`mcp_server_references`). The delete check, the "used by" line every screen shows and
#: the release option all read through it, and ``test_mcp_servers_are_managed`` parses
#: db/schema.sql for every column that REFERENCES mcp_servers(id) and fails when one is missing
#: here. That test is the reason this is a list: round 1 of this milestone checked custom steps
#: and skills only, and the agents stream's ``role_mcp_servers`` (an agent HOLDING a server)
#: arrived beside it unseen — deleting a server an agent held would have cascaded its grants
#: away with nothing said.
#:
#: A referrer table that does not exist yet on an install is read as "nothing points from
#: there", which is true.
MCP_REFERRERS: tuple[tuple[str, str, str], ...] = (
    ("workflow_custom_steps", "mcp_server_id", "custom_steps"),
    ("skill_definitions", "config->>'mcp_server_id'", "skills"),
    ("role_mcp_servers", "mcp_server_id", "agents"),
)


def _table_ready(connection: Connection, table: str) -> bool:
    try:
        return connection.execute(
            text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None
    except Exception:  # noqa: BLE001 — a non-Postgres test double
        return False


def mcp_server_references(
    connection: Connection, project_id: str, server_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Everything that points at one server, one list per entry of :data:`MCP_REFERRERS`:
    custom steps bound to its tools, authored skills whose behaviour calls one of them, and the
    agents that hold it."""
    steps = connection.execute(
        text("SELECT step_key, label, mcp_tool_name FROM workflow_custom_steps "
             "WHERE project_id = :p AND mcp_server_id = :s ORDER BY step_key"),
        {"p": project_id, "s": server_id},
    ).mappings().all()
    skills = connection.execute(
        text("SELECT skill_key, label, config->>'tool_name' AS tool_name FROM skill_definitions "
             "WHERE project_id = :p AND behaviour_kind = 'mcp_tool' "
             "AND config->>'mcp_server_id' = :s ORDER BY skill_key"),
        {"p": project_id, "s": server_id},
    ).mappings().all()
    agents = [] if not _table_ready(connection, "role_mcp_servers") else connection.execute(
        text("SELECT role_key, tools FROM role_mcp_servers "
             "WHERE project_id = :p AND mcp_server_id = :s ORDER BY role_key"),
        {"p": project_id, "s": server_id},
    ).mappings().all()
    return {
        "custom_steps": [{"step_key": r["step_key"], "label": r["label"],
                          "tool": r["mcp_tool_name"]} for r in steps],
        "skills": [{"skill_key": r["skill_key"], "label": r["label"], "tool": r["tool_name"]}
                   for r in skills],
        "agents": [{"role_key": str(r["role_key"]), "tools": _loads(r["tools"], []) or []}
                   for r in agents],
    }


def delete_mcp_server(
    connection: Connection, project_id: str, server_id: str, actor: str = "system", *,
    release_grants: bool = False,
) -> bool:
    """Remove a server, refusing while anything in :data:`MCP_REFERRERS` still points at it.

    Refusing rather than cascading: a custom step whose server vanished became unusable with no
    warning (its FK is ON DELETE SET NULL) and every workflow placing it stopped publishing, and
    an authored skill kept an id that pointed at nothing. Turning the server off is the
    non-destructive alternative and the refusal says so.

    AN AGENT HOLDING THE SERVER is the one reference the operator may release in the same
    breath, exactly like a skill (skill_definitions.delete_skill): taking a server off an agent
    breaks nothing published, so ``release_grants`` removes those grants first, on purpose and
    named in the event. Without it the refusal names the agents. Before this the
    role_mcp_servers FK cascaded and the agents lost the server with nothing said.

    A server BOSS seeds stays deleted: its name goes to seed_exclusions, the one record every
    seed path asks (services/seed_exclusions.py), so the next install upgrade does not bring it
    back.
    """
    current = get_mcp_server(connection, project_id, server_id)
    if current is None:
        return False
    refs = mcp_server_references(connection, project_id, server_id)
    users = ([f"step '{s['label'] or s['step_key']}'" for s in refs["custom_steps"]]
             + [f"skill '{s['label'] or s['skill_key']}'" for s in refs["skills"]])
    if users:
        raise McpServerInUse(
            f"MCP server '{current['name']}' is still used by {', '.join(users[:8])}. "
            "Re-point or delete those first — or turn the server off instead of removing it.")
    holders = [a["role_key"] for a in refs["agents"]]
    if holders and not release_grants:
        raise McpServerInUse(
            f"MCP server '{current['name']}' is held by {', '.join(holders[:8])}. Removing it "
            f"takes it off {'that agent' if len(holders) == 1 else 'those agents'}; confirm to "
            "go ahead, or turn the server off instead.")
    if holders:
        connection.execute(
            text("DELETE FROM role_mcp_servers WHERE project_id = :p AND mcp_server_id = :s"),
            {"p": project_id, "s": server_id},
        )
    connection.execute(
        text("DELETE FROM mcp_servers WHERE id = :id AND project_id = :p"),
        {"id": server_id, "p": project_id},
    )
    from . import seed_exclusions

    seed_exclusions.exclude(connection, project_id, "mcp_server", str(current["name"]),
                            actor=actor, reason="deleted")
    emit_event(connection, project_id, "mcp.server_removed", actor, "mcp_server", server_id,
               {"name": current["name"], "url": current["url"], "released_from": holders,
                "builtin": (current["provenance"] or {}).get("source")
                           in ("builtin", "builtin_edited")})
    return True


def ensure_mcp_server(
    connection: Connection,
    project_id: str,
    name: str,
    url: str,
    *,
    transport: str = "http",
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
    publisher: Optional[dict[str, Any]] = None,
    provenance: Optional[dict[str, Any]] = None,
    tool_risk_overrides: Optional[dict[str, int]] = None,
    respect_operator: bool = False,
) -> Optional[dict]:
    """Idempotently register a server by (project, name).

    Returns None, and writes nothing, for a server an operator deleted: the seed is additive
    only and never resurrects what a person removed. That memory is seed_exclusions (kind
    ``mcp_server``, subject the NAME, because built-ins are ensured by name), the one record
    every seed path asks. Round 1 of this milestone scanned the events table for
    ``mcp.server_removed`` instead, which made two memories of one decision once the agents
    stream added seed_exclusions. An explicit bundle IMPORT is an operator's decision and lifts
    the record before it calls this.

    For a server that EXISTS the two callers want different things. An import is an operator
    saying "make it this", so it overwrites. The bootstrap SEED (``respect_operator=True``)
    leaves a server a person EDITED alone: before this a re-seed put 127.0.0.1 back over the
    reachable address the operator had just given a built-in server on boss-1.
    """
    from . import seed_exclusions

    if seed_exclusions.is_excluded(connection, project_id, "mcp_server", name):
        return None
    _validated_server_identity(name, url, transport)
    risk_tier = _validated_risk(risk_tier)
    data_boundary = normalize_data_boundary(data_boundary)
    tool_risk_overrides = _validated_overrides(tool_risk_overrides)
    existing = connection.execute(
        text("SELECT id, provenance FROM mcp_servers WHERE project_id = :p "
             "AND lower(name) = lower(:n)"),
        {"p": project_id, "n": name},
    ).mappings().first()
    if existing:
        current_provenance = _loads(existing["provenance"], {}) or {}
        if respect_operator and current_provenance.get("edited_by"):
            return get_mcp_server(connection, project_id, str(existing["id"]))
        row = connection.execute(
            text(
                "UPDATE mcp_servers SET url = :url, transport = :transport, risk_tier = :risk, data_boundary = :db, description = :desc, "
                "publisher = CAST(:publisher AS JSONB), provenance = CAST(:provenance AS JSONB), "
                "tool_risk_overrides = CAST(:tool_risk_overrides AS JSONB), updated_at = now() "
                "WHERE id = :id "
                f"RETURNING {_MCP_COLUMNS}"
            ),
            {
                "url": url, "transport": transport, "risk": int(risk_tier), "db": data_boundary,
                "desc": description, "id": existing["id"],
                "publisher": json.dumps(publisher or {}),
                "provenance": json.dumps(provenance or {}),
                "tool_risk_overrides": json.dumps(tool_risk_overrides or {}),
            },
        ).mappings().first()
        return _row(row)
    return register_mcp_server(
        connection, project_id, name, url,
        transport=transport, risk_tier=risk_tier, data_boundary=data_boundary, description=description,
        publisher=publisher, provenance=provenance,
        tool_risk_overrides=tool_risk_overrides, actor="bootstrap",
    )


# --------------------------------------------------------------------------- #
# Reachability                                                                  #
# --------------------------------------------------------------------------- #

#: How much of a tool's description the registry STORES (for screens and the "Tool changed"
#: diff). Large on purpose: :func:`probe` hands its summary to :func:`record_reachability`, which
#: summarises it again, so a cut a real description reaches would hash differently on the two
#: roads. Past it that can only quarantine MORE (a spurious change), never let a change through.
DESCRIPTION_STORED_CHARS = 20000


def _tool_summary(tools: Any) -> list[dict[str, Any]]:
    """What the registry stores of a server's tool list.

    WHAT IS HASHED IS WHAT THE MODEL IS SHOWN (BOSSAPP-818). agent_loop offers the model the
    LIVE description and inputSchema untruncated, so both hashes cover the whole of each; the
    stored ``description`` is cut at DESCRIPTION_STORED_CHARS for screens and diffs only. The
    hash used to be taken over a 500-character cut, so a poisoned sentence appended past
    character 500 changed what the model read without changing the hash."""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        full = str(tool.get("description") or "")
        schema = (tool.get("inputSchema")
                  if isinstance(tool.get("inputSchema"), dict) else None)
        out.append({"name": str(tool["name"]),
                    "description": full[:DESCRIPTION_STORED_CHARS],
                    "inputSchema": schema,
                    "schema_sha256": schema_sha256(schema),
                    "description_sha256": description_sha256(full),
                    "hash_version": HASH_VERSION})
    return out


def probe(settings: Any, url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS,
          headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Ask a server for its tools. Never raises: the answer is reachable or not, with why.

    `headers` is the server's sign-in (:func:`auth_headers`). The error is the server's own
    words where it gave some — "401 Unauthorized" is what tells an operator the token is wrong.
    """
    import time as _time

    from .mcp_client import MCPClient

    started = _time.monotonic()
    try:
        tools = MCPClient(settings, timeout=timeout).list_tools(url, headers=headers) or []
        return {"reachable": True, "tools": _tool_summary(tools), "error": None,
                "latency_ms": int((_time.monotonic() - started) * 1000)}
    except Exception as error:  # noqa: BLE001 — the reason IS the result
        return {"reachable": False, "tools": [], "latency_ms":
                int((_time.monotonic() - started) * 1000),
                "error": _said(error)[:500]}


def _said(error: BaseException) -> str:
    """An error as an operator reads it: an HTTP status with its reason, not a traceback name."""
    try:
        import httpx

        if isinstance(error, httpx.HTTPStatusError):
            return f"{error.response.status_code} {error.response.reason_phrase}".strip()
        if isinstance(error, httpx.ConnectError):
            return f"ConnectError: {error}"
        if isinstance(error, httpx.TimeoutException):
            return "Timed out"
    except Exception:  # noqa: BLE001
        pass
    return f"{type(error).__name__}: {error}"


def test_entered(settings: Any, connection: Optional[Connection], project_id: str, url: str,
                 auth: Optional[dict[str, Any]], *, timeout: float = 15.0) -> dict[str, Any]:
    """Probe the URL and sign-in an operator TYPED, before anything is stored (Save & test).

    The token is used for this one call and dropped; nothing is written."""
    transport = "stdio" if str(url or "").startswith("stdio://") else "http"
    _validated_server_identity("test", url, transport)
    if transport == "stdio":
        from .mcp_stdio_templates import parse_stdio_url

        if (parse_stdio_url(url) or ("", ""))[0] != project_id:
            raise McpServerError("a stdio server can only use a template of its own project")
        if connection is not None:
            _validated_stdio_target(connection, project_id, url, transport)
    checked = _validated_auth(auth)
    headers = {str(k): str(v) for k, v in (checked.get("headers") or {}).items()}
    secret = checked.get("secret")
    if secret and checked["type"] == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif secret and checked["type"] == "header":
        headers[str(checked["header_name"])] = secret
    found = probe(settings, url, timeout=timeout, headers=headers)
    return {"ok": bool(found["reachable"]),
            "tools": [{"name": t["name"], "description": t["description"]}
                      for t in found["tools"]],
            "latency_ms": found.get("latency_ms") or 0, "error": found["error"]}


def record_reachability(
    connection: Connection,
    project_id: str,
    server_id: str,
    *,
    reachable: bool,
    tools: Optional[list[dict[str, Any]]] = None,
    error: Optional[str] = None,
) -> None:
    """Write down what a probe found. A failed probe keeps the last-known tool list, because an
    operator fixing a server wants to see which tools it HAD, not an empty box.

    A reachable probe compares the new summary with the stored tools first. A same-named tool
    whose schema or description hash changed is stored as ask, and one decision is opened per
    such tool before the update. An unreachable probe does not compare and does not open a
    decision. A decision that cannot be opened is not swallowed, so the new list is not stored
    without it.

    THE MARK OUTLIVES THE PROBE (BOSSAPP-818): an open quarantine on the stored entry is carried
    forward (tool_catalog.carry_quarantine) until a person answers the decision
    (:func:`accept_tool_change` / :func:`refuse_tool_change`). A tool that changes AGAIN while
    quarantined does not open a second card: its one open card is rewritten to baseline -> now.

    Returns the list it stored (None for an unreachable probe, which stores none).
    """
    summaries = _tool_summary(tools or [])
    stored = summaries
    if reachable:
        # FOR UPDATE: a run's probe and a screen's probe of one server must not both read the
        # old list — the second write would drop the first one's quarantine.
        row = connection.execute(
            text("SELECT name, tools FROM mcp_servers WHERE id = :id AND project_id = :p "
                 "FOR UPDATE"),
            {"id": server_id, "p": project_id},
        ).mappings().first()
        previous = _loads(row["tools"], []) if row is not None else None
        quarantined = compare_catalog(previous, summaries)["quarantined"]
        marked = apply_quarantine(summaries, quarantined)
        from datetime import datetime, timezone

        stored = carry_quarantine(previous, marked, quarantined,
                                  now=datetime.now(timezone.utc).isoformat())
        server_name = str(row["name"]) if row is not None and row["name"] else ""
        fresh = {str(item["name"]) for item in quarantined}
        items = [review_item(entry) for entry in stored
                 if isinstance(entry, dict) and str(entry.get("name")) in fresh]
        for decision in changed_tool_decisions(server_name, items, server_id=server_id):
            _open_tool_change_decision(connection, project_id, server_id, decision)
        # AN "ALWAYS ALLOW" NEVER OUTLIVES THE DEFINITION IT WAS GIVEN FOR (BOSSAPP-817): every
        # decision rule pinned to this tool's old schema is revoked now, audited. decide() also
        # refuses a stale pin at call time, so a failure here can only delay the bookkeeping.
        from .tool_policy_engine import drop_pinned_rules

        for item in quarantined:
            name = str(item.get("name") or "")
            now_hash = next((e.get("schema_sha256") for e in stored
                             if isinstance(e, dict) and e.get("name") == name), None)
            try:
                with connection.begin_nested():
                    drop_pinned_rules(connection, project_id, server_id, name, now_hash)
            except Exception:  # noqa: BLE001 — see above: the call-time check still holds
                import logging

                logging.getLogger(__name__).warning(
                    "always-allow rules for %s not dropped", name, exc_info=True)
    connection.execute(
        text(
            "UPDATE mcp_servers SET reachable = :r, last_checked_at = now(), last_error = :e"
            + (", tools = CAST(:t AS JSONB)" if reachable else "")
            + " WHERE id = :id AND project_id = :p"
        ),
        {"r": bool(reachable), "e": None if reachable else (error or "no answer")[:500],
         "t": json.dumps(stored), "id": server_id, "p": project_id},
    )
    return stored if reachable else None


TOOL_CHANGE_ACTION = "review_tool_change"


def _pending_tool_change(connection: Connection, project_id: str, server_id: str,
                         tool: str) -> Optional[str]:
    row = connection.execute(
        text("SELECT id FROM decisions WHERE project_id = :p AND status = 'pending' "
             "AND recommendation->>'action' = :a AND recommendation->>'server_id' = :sid "
             "AND recommendation->>'tool' = :tool ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "a": TOOL_CHANGE_ACTION, "sid": server_id, "tool": tool},
    ).first()
    return str(row[0]) if row is not None else None


def _open_tool_change_decision(connection: Connection, project_id: str, server_id: str,
                               decision: dict[str, Any]) -> str:
    """ONE OPEN CARD PER QUARANTINED TOOL. A tool that changed again while its card waits gets
    that card rewritten (question and hashes are baseline -> now), so the person never approves
    a definition older than the one the server now sends."""
    rec = {**decision["recommendation"], "action": "review_tool_change"}
    existing = _pending_tool_change(connection, project_id, server_id, str(rec["tool"]))
    if existing is None:
        return create_blocking_decision(connection, project_id, None, decision["question"], rec,
                                        {}, "tool_schema_changed", risk_tier=2)
    prior = connection.execute(
        text("SELECT recommendation FROM decisions WHERE id = :id"), {"id": existing},
    ).scalar()
    prior = _loads(prior, {}) or {}
    merged = {**prior, **rec, "occurrences": int(prior.get("occurrences") or 1) + 1}
    connection.execute(
        text("UPDATE decisions SET question = :q, recommendation = CAST(:r AS JSONB), "
             "updated_at = now() WHERE id = :id"),
        {"q": decision["question"], "r": json.dumps(merged), "id": existing})
    return existing


def _tool_entry_update(connection: Connection, project_id: str, server_id: str, tool: str,
                       change: Callable[[dict[str, Any]], Optional[dict[str, Any]]]
                       ) -> Optional[dict[str, Any]]:
    """Rewrite one stored tool entry under a row lock. ``change`` returns the new entry, or
    None to leave the list as it is. Returns the entry as it was before."""
    row = connection.execute(
        text("SELECT tools FROM mcp_servers WHERE id = :id AND project_id = :p FOR UPDATE"),
        {"id": server_id, "p": project_id},
    ).first()
    if row is None:
        raise McpServerError("MCP server not found.")
    tools = _loads(row[0], []) or []
    before = None
    out = []
    for entry in tools:
        if isinstance(entry, dict) and entry.get("name") == tool and before is None:
            before = dict(entry)
            replaced = change(dict(entry))
            out.append(replaced if replaced is not None else entry)
        else:
            out.append(entry)
    if before is not None:
        connection.execute(
            text("UPDATE mcp_servers SET tools = CAST(:t AS JSONB), updated_at = now() "
                 "WHERE id = :id AND project_id = :p"),
            {"t": json.dumps(out), "id": server_id, "p": project_id})
    return before


def tool_change_server_id(connection: Connection, project_id: str,
                          recommendation: dict[str, Any]) -> str:
    """The server a "Tool changed" card is about. Cards raised before BOSSAPP-818 carry only the
    server's name, so that is looked up; '' when the server is gone."""
    sid = str(recommendation.get("server_id") or "")
    if sid:
        return sid
    found = find_mcp_server(connection, project_id, str(recommendation.get("server") or ""))
    return str(found["id"]) if found else ""


class ToolChangedAgain(McpServerError):
    """The tool's definition is no longer the one the decision showed. Not retryable."""

    retryable = False


def accept_tool_change(connection: Connection, project_id: str, server_id: str, tool: str, *,
                       actor: str, schema_sha256: Optional[str] = None,
                       description_sha256: Optional[str] = None,
                       decision_id: Optional[str] = None) -> bool:
    """A person approved a "Tool changed" decision: the quarantine is cleared, the definition
    the card showed becomes the accepted one, and the entry records who reviewed it and when.

    Refused (ToolChangedAgain) when the stored hashes are not the ones the card showed — the
    server changed the tool again after the person looked. False when there is nothing to clear
    (the tool is gone, or the quarantine was already cleared)."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    cleared: dict[str, bool] = {"done": False}

    def _clear(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not is_quarantined(entry):
            return None
        if ((schema_sha256 and entry.get("schema_sha256") != schema_sha256)
                or (description_sha256 and entry.get("description_sha256") != description_sha256)):
            raise ToolChangedAgain(
                f"{tool} changed again after this card was raised; the card now open for it "
                "shows the current definition.")
        for key in QUARANTINE_KEYS:
            entry.pop(key, None)
        entry["reviewed_by"] = actor
        entry["reviewed_at"] = stamp
        cleared["done"] = True
        return entry

    _tool_entry_update(connection, project_id, server_id, tool, _clear)
    if cleared["done"]:
        emit_event(connection, project_id, "mcp.tool_change_accepted", actor, "mcp_server",
                   server_id, {"tool": tool, "decision_id": decision_id,
                               "schema_sha256": schema_sha256,
                               "description_sha256": description_sha256})
    return cleared["done"]


def refuse_tool_change(connection: Connection, project_id: str, server_id: str, tool: str, *,
                       actor: str, decision_id: Optional[str] = None) -> None:
    """A person rejected a "Tool changed" decision: the quarantine STAYS (a later probe cannot
    lift it) and the tool is turned Off, so no agent is offered it or may call it."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()

    def _refuse(entry: dict[str, Any]) -> dict[str, Any]:
        entry["quarantine"] = entry.get("quarantine") or "ask"
        entry["quarantine_refused_by"] = actor
        entry["quarantine_refused_at"] = stamp
        return entry

    _tool_entry_update(connection, project_id, server_id, tool, _refuse)
    set_tool_policy(connection, project_id, server_id, tool, "off", actor=actor)
    emit_event(connection, project_id, "mcp.tool_change_refused", actor, "mcp_server",
               server_id, {"tool": tool, "decision_id": decision_id})


def record_reachability_quietly(
    connection: Connection,
    project_id: str,
    server: dict[str, Any],
    *,
    reachable: bool,
    tools: Optional[list[dict[str, Any]]] = None,
    error: Optional[str] = None,
) -> None:
    """Record a probe made in passing — by a run, not by a screen — inside a savepoint.

    A run that happened to discover a server is down must not itself fail because writing that
    down failed; the run's own work is what matters there.

    The caller's ``server`` dict is given the list that was stored, so a quarantine this very
    probe opened applies to what the run offers and calls next (BOSSAPP-818) — the run read
    the server before the probe.
    """
    try:
        with connection.begin_nested():
            stored = record_reachability(connection, project_id, str(server.get("id") or ""),
                                         reachable=reachable, tools=tools, error=error)
    except Exception:  # noqa: BLE001
        return
    if stored is not None:
        server["tools"] = stored


def known_unreachable(server: dict[str, Any],
                      max_age_seconds: int = REACHABILITY_FRESH_SECONDS) -> bool:
    """Whether a probe in the last minute found this server down — so a caller can skip it
    rather than pay the timeout again."""
    return server.get("reachable") is False and not _stale(server, max_age_seconds)


def _stale(server: dict[str, Any], max_age_seconds: int) -> bool:
    checked = server.get("last_checked_at")
    if not checked or max_age_seconds <= 0:
        return True
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(str(checked))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() > max_age_seconds


def servers_with_tools(
    begin: Callable[[], ContextManager[Connection]],
    settings: Any,
    project_id: str,
    *,
    enabled_only: bool = False,
    max_age_seconds: int = REACHABILITY_FRESH_SECONDS,
    server_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Every server with its tools and whether it answered, probing only what is stale.

    THE ONE READ every screen and the builder use. The Capabilities screen and the custom-step
    builder each probed every server on every load, inline, and kept nothing — so the same dead
    server cost a 6-second wait per screen per load and no screen could say when it was last
    seen. Probes happen OUTSIDE a transaction (``begin`` is called twice, briefly), because a
    network call held inside one keeps a database connection waiting on a remote server.
    """
    with begin() as connection:
        servers = list_mcp_servers(connection, project_id, enabled_only=enabled_only)
        if server_id:
            servers = [s for s in servers if s["id"] == server_id]
        stale = [s for s in servers if s["enabled"] and _stale(s, max_age_seconds)]
        signed: dict[str, Any] = {}
        for server in stale:
            try:
                signed[server["id"]] = auth_headers(connection, settings, project_id, server)
            except Exception as error:  # noqa: BLE001 — a token that cannot be read is the result
                signed[server["id"]] = error
    found: dict[str, dict[str, Any]] = {}
    for server in stale:
        headers = signed.get(server["id"])
        if isinstance(headers, Exception):
            found[server["id"]] = {"reachable": False, "tools": [],
                                   "error": f"Token unavailable: {_said(headers)}"[:500]}
            continue
        found[server["id"]] = probe(settings, server["url"], headers=headers)
    if found:
        with begin() as connection:
            for sid, result in found.items():
                record_reachability(connection, project_id, sid, reachable=result["reachable"],
                                    tools=result["tools"], error=result["error"])
            servers = [s for s in list_mcp_servers(connection, project_id,
                                                   enabled_only=enabled_only)
                       if not server_id or s["id"] == server_id]
    return servers


# --------------------------------------------------------------------------- #
# Calling a tool                                                                #
# --------------------------------------------------------------------------- #

def call_tool_with_receipt(
    connection: Connection,
    settings: Any,
    project_id: str,
    server: dict[str, Any],
    tool_name: str,
    inputs: dict[str, Any],
    *,
    dedupe_key: str,
    tenant_scope: tuple[str, str],
    created_by_job_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    approval_about: Optional[dict[str, Any]] = None,
    agent: Optional[str] = None,
    principal: Optional[str] = None,
    policy_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The one EFFECT of calling an MCP tool: a receipt, the call, and the receipt settled.

    THE BACKSTOP FOR THE REVIEW RULE (:func:`needs_person`). Every road to an MCP tool ends
    here, so this is where an unreviewed server's tool is refused unless ``approved_by`` names
    an APPROVED decision in this project ABOUT this call (``approval_about``, see
    :func:`approval_stands`) — the agent run's resume decision, or the workflow step's gate.
    The gates upstream (agent_loop, the catalogue, resolve_risk) are what make that decision
    exist; this makes a road that skips them fail closed with a sentence instead
    of running unattended. Round-2 review found two such roads (an authored mcp_tool skill and
    a custom step) after the rule had been written at one call site.

    Shared by the agent loop's MCP tools (`agent_loop._invoke_mcp_skill`) and the authored
    `mcp_tool` skill executor, so a tool reached either way leaves the same evidence and is
    deduplicated the same way. Permission, budget and retry are NOT here — they belong to
    SkillRuntimeService, which both callers go through.

    A transport failure also records the server as unreachable, so the screens learn about a
    dead server from the run that hit it rather than from the next person who opens them.

    Each road also appends one tool_call_audit row. A missing table is not swallowed. A
    delivered result is screened for prompt injection first (BOSSAPP-822,
    :func:`tool_result_screen.screen_result`) and its verdict is on that row (``screen``).
    The row names WHO (BOSSAPP-819): ``agent`` is the caller's actor (the agent's role, the
    skill's caller), ``principal`` the identity it acts as — the caller's, else the one in
    effect (identity.lineage.current: the run's person or agent principal).

    THE ORDERED TOOL POLICY (BOSSAPP-815, :func:`tool_policy_engine.decide`) is asked here, in
    place of the bare Allow / Ask / Off read: a rule may deny, ask or rate-limit, an "Always
    allow" may let a tier 0-1 Ask tool run, and no rule lifts Off, the tier floor or a
    quarantine. ``policy_context`` names who is calling ({role_key, workflow_id, run_id}) so an
    agent- or workflow-scoped rule applies. The audit row carries the REAL matched ids; when a
    rule or a ceiling decided, the explanation rides on the row's ``screen`` as ``policy``.
    """
    from .mcp_client import MCPClient
    from .receipts import ReceiptService
    from .tool_policy_engine import context_for, decide

    about = approval_about if isinstance(approval_about, dict) else {}
    who = policy_context if isinstance(policy_context, dict) else {}
    decided: dict[str, Any] = {}

    audit_agent = (str(agent or "").strip()
                   or str((policy_context or {}).get("role_key") or "").strip()
                   or "mcp_registry")
    audit_principal = str(principal or "").strip() or acting_principal()

    def _audit_tool_call(outcome: str, decision: str, *, latency_ms: int = 0,
                         screen: Optional[dict[str, Any]] = None) -> None:
        ids = list(decided.get("matched_policy_ids") or [])
        if not ids and decision in ("allow", "ask", "off"):
            ids = [decision]
        if decided and (decided.get("rule_id") or any(i.startswith("guard:") for i in ids)):
            screen = {**(screen or {}), "policy": {
                "decision": decided.get("decision"), "rule_id": decided.get("rule_id"),
                "explanation": decided.get("explanation")}}
        record_tool_call(
            connection,
            project_id=project_id,
            run_id=about.get("run_id") if isinstance(about.get("run_id"), str) else None,
            agent=audit_agent,
            principal=audit_principal,
            server_id=server.get("id") if isinstance(server.get("id"), str) else None,
            tool=tool_name,
            arguments=inputs if isinstance(inputs, dict) else {},
            decision=decision,
            matched_policy_ids=ids,
            decision_id=approved_by,
            outcome=outcome,
            latency_ms=latency_ms,
            screen=screen,
        )

    def _screened(delivered: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        # THE INJECTION SCREEN (BOSSAPP-822, tool_result_screen): every result this door hands
        # back is screened before anyone reads it. A hit is still delivered, marked untrusted
        # (a loud first key the model reads), and the verdict lands on the audit row.
        return screen_result(connection, settings, project_id, delivered,
                             ref=str(delivered.get("receipt_id") or tool_name))

    if needs_person(server) and not approval_stands(connection, project_id, approved_by,
                                                    approval_about):
        _audit_tool_call("refused", "unreviewed")
        raise McpServerUnreviewed(_refusal(tool_name, server, approved_by))
    # THE OPERATOR'S POLICY (spec E7 + BOSSAPP-815), the backstop for every road like the review:
    # an Off tool never runs, a denied call never runs, an Ask call runs only with a person's
    # approval about this call. A QUARANTINED tool is Ask whatever it was set to (BOSSAPP-818);
    # its mark is read from the row as it stands now, not from a server dict a caller loaded
    # before a probe changed it — so the decision reads the STORED tool list.
    decided.update(decide(
        context_for(_with_stored_tools(connection, project_id, server), tool_name, inputs,
                    project_id=project_id,
                    run_id=str(who.get("run_id") or about.get("run_id") or "") or None,
                    role_key=str(who.get("role_key") or about.get("role_key") or "") or None,
                    workflow_id=str(who.get("workflow_id") or about.get("workflow_id") or "")
                    or None),
        connection=connection, record=True))
    policy = {"allow": "allow", "ask": "ask"}.get(decided["decision"], "off")
    if decided["decision"] == "deny" and decided.get("reason") == "off":
        _audit_tool_call("refused", "off")
        raise McpToolOff(f"Did not call {tool_name}: it is turned off for "
                         f"'{server.get('name')}' in Skills & MCP.")
    if decided["decision"] == "deny":
        _audit_tool_call("refused", "deny")
        raise McpToolDenied(f"Did not call {tool_name} on '{server.get('name')}': "
                            f"{decided['explanation']}")
    if policy == "ask" and not approval_stands(connection, project_id, approved_by,
                                               approval_about):
        _audit_tool_call("refused", "ask")
        raise McpServerUnreviewed(
            f"Did not call {tool_name}: it is set to ask a person each time, and this call "
            "carried no approval about it.")
    if str(approved_by or "").strip() and not approved_arguments_match(
        connection, project_id, approved_by, tool_name, inputs,
    ):
        _audit_tool_call("refused", "arguments")
        raise McpArgumentsChanged(
            f"Did not call {tool_name}: arguments do not match the approved call."
        )
    headers = auth_headers(connection, settings, project_id, server)
    org_id, workspace_id = tenant_scope
    receipts = ReceiptService()
    receipt, created = receipts.create_or_reuse(
        connection,
        org_id,
        workspace_id,
        project_id,
        dedupe_key,
        "mcp.tool.call",
        {
            "server_id": server["id"],
            "server": server["name"],
            "tool": tool_name,
            "data_boundary": server.get("data_boundary") or "internal",
        },
        inputs,
        created_by_job_id=created_by_job_id,
        data_boundary=server.get("data_boundary") or "internal",
    )
    if not created:
        if receipt.get("status") == "succeeded":
            delivered, verdict = _screened({
                "output": receipt.get("result") or {},
                "receipt_id": receipt["id"],
                "actual_cost_usd_micros": 0,
                "deduplicated": True,
            })
            _audit_tool_call("deduplicated", policy, screen=verdict)
            return delivered
        raise RuntimeError(
            f"MCP effect receipt already exists with status {receipt.get('status')}"
        )
    started = time.monotonic()
    try:
        output = MCPClient(settings).call_tool(server["url"], tool_name, inputs,
                                               headers=headers)
    except Exception as exc:
        receipts.mark_failed(
            connection,
            receipt["id"],
            {"type": type(exc).__name__, "message": str(exc)},
        )
        if _is_transport_failure(exc):
            record_reachability_quietly(connection, project_id, server, reachable=False,
                                        error=f"{type(exc).__name__}: {exc}")
        _audit_tool_call(
            "failed", policy, latency_ms=max(0, int((time.monotonic() - started) * 1000))
        )
        raise
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    receipts.mark_succeeded(
        connection,
        receipt["id"],
        f"{server['id']}:{tool_name}",
        output if isinstance(output, dict) else {"value": output},
    )
    delivered, verdict = _screened({
        "output": output,
        "receipt_id": receipt["id"],
        "actual_cost_usd_micros": 0,
    })
    _audit_tool_call("succeeded", policy, latency_ms=elapsed_ms, screen=verdict)
    return delivered


def _refusal(tool_name: str, server: dict[str, Any], approved_by: Optional[str]) -> str:
    """What a run records when the backstop refuses — and what to do, which depends on whether
    an approval was carried at all.

    Round-3 review: the one sentence ended "approve it when BOSS asks", and a product workflow's
    ``kind: skill`` step reaches the tool through a ``skill.execute`` job that carries no
    approval — so an operator who had just approved the step's gate was told to approve it.
    A road with no approval in hand cannot be fixed by approving; the server has to be
    reviewed."""
    if not str(approved_by or "").strip():
        return (f"Did not call {tool_name}: {unreviewed_sentence(server)} This call reached "
                "the server without a person's approval attached, so approving does not "
                "release it here: mark the server Reviewed, then run it again.")
    return (f"Did not call {tool_name}: {unreviewed_sentence(server)} The approval this call "
            f"carried ({approved_by}) is not an approved decision about this run or step. "
            "Approve the request BOSS raised for it, or mark the server Reviewed, then run it "
            "again.")


def call_tool_unattended(
    settings: Any,
    server: dict[str, Any],
    tool_name: str,
    inputs: dict[str, Any],
    *,
    timeout: Optional[float] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    """Call a REGISTERED server's tool where no person is present to ask and no receipt is
    kept — the meeting's shared research crawl.

    Round-3 review: the meeting research phase called the registered 'web-intel' server with
    ``MCPClient.call_tool`` directly, skipping :func:`needs_person`. Once a server's URL could be
    edited and chat could register servers, an operator could re-point web-intel (or delete it
    and register another 'web-intel'), and every meeting then sent its URL to that server and
    fed the reply into every persona prompt, with nobody asked. There is no one to ask on this
    road, so an unreviewed server is simply refused: the caller records
    :func:`unreviewed_sentence` and carries on without the tool.

    Every call to a registry server's tool goes through this module — this function or
    :func:`call_tool_with_receipt`. The enforcing test
    (test_an_unreviewed_server_never_runs_unattended) fails on a ``.call_tool(`` anywhere else
    in shipping code, apart from a named allowlist of clients whose URL comes from deployment
    configuration rather than the registry.
    """
    from .mcp_client import MCPClient

    if needs_person(server):
        raise McpServerUnreviewed(unreviewed_sentence(server))
    if tool_policy(server, tool_name, _declared_tier(server, tool_name)) != "allow":
        # Nobody is present on this road to ask, so Ask is refused like Off.
        raise McpToolOff(f"Did not call {tool_name}: it is not set to run without asking.")
    client = MCPClient(settings) if timeout is None else MCPClient(settings, timeout=timeout)
    return client.call_tool(server["url"], tool_name, inputs, headers=headers or {})


def _with_stored_tools(connection: Connection, project_id: str,
                       server: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``server`` whose tool list (where the quarantine marks live) is the stored one
    right now. A server with no row (a caller's own dict) is returned as it is, and so is one
    whose row cannot be read: the read is inside a savepoint, and its failure falls back to the
    caller's own list (never to less than that list says)."""
    sid = server.get("id")
    if not isinstance(sid, str) or not sid:
        return server
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT tools FROM mcp_servers WHERE id = :id AND project_id = :p"),
                {"id": sid, "p": project_id},
            ).first()
    except Exception:  # noqa: BLE001 — the caller's own list still applies
        return server
    if row is None:
        return server
    tools = _loads(row[0], [])
    return {**server, "tools": tools if isinstance(tools, list) else []}


def _declared_tier(server: dict[str, Any], tool_name: str) -> Any:
    """The tier a server's last tool list said this tool claims, if any (it may only raise)."""
    for tool in server.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            annotations = tool.get("annotations") or {}
            return tool.get("risk_tier", annotations.get("risk_tier")
                            if isinstance(annotations, dict) else None)
    return None


def _is_transport_failure(error: BaseException) -> bool:
    try:
        import httpx

        return isinstance(error, (httpx.TransportError,))
    except Exception:  # noqa: BLE001
        return False
