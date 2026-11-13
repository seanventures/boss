"""CURATED GATEWAYS FOR OUTSIDE MCP CLIENTS (BOSSAPP-851).

BOSS's own MCP server (engine/boss_engine/mcp/server.py) exposes BOSS's tools. A GATEWAY exposes a
chosen slice of a project's REGISTERED MCP servers — the ones BOSS already reviewed, signs in to
and audits — to a client outside BOSS (Claude Code, Cursor), at ``/mcp/gw/{gateway_id}``, over
MCP streamable HTTP. (Not the BOSSAPP-814 build gateway in mcp_gateway.py, which serves BOSS's own
running agents.)

* PROFILE. ``{"servers": [{"server_id", "tools": [..] | null, "caps": {tool: "allow" | "off"}}]}``:
  which servers, which of their tools (null = all), and a per-tool cap that can only make a tool
  stricter than the registry's own Allow / Ask / Off. Nobody is present to answer an Ask, so a
  gateway lists only tools that run without asking; the registry's backstop refuses the rest.
* TOKENS. ``boss_gw_…``, shown once at mint; only the SHA-256 is stored, with the last four, an
  expiry and a revocation. A request presents it as ``Authorization: Bearer``; the hash is looked
  up, compared in constant time, and must belong to THIS gateway, be unexpired and unrevoked, and
  the gateway must be active — anything else is 401.
* CALLS. ``tools/call`` goes through ``mcp_registry.call_tool_with_receipt``: review, policy,
  quarantine, the receipt, the injection screen and the tool_call_audit row are exactly what an
  inside call gets. The audit row's run is ``mcpgw:<gateway_id>``.
* META-TOOLS. ``search_tools`` (find a tool in the profile by words) and ``run_tool`` (call one by
  name), for clients that work better with two tools than with fifty.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

TOKEN_PREFIX = "boss_gw_"
SEPARATOR = "__"
ACTIONS = ("tools/list", "tools/call", "search_tools", "run_tool")
CAPS = ("allow", "off")
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TOKEN_DAYS = 365
META_TOOLS = ("search_tools", "run_tool")


class GatewayError(ValueError):
    """Something the operator can fix. 422."""


class GatewayConflict(GatewayError):
    """The name is taken. 409."""


class GatewayAuthError(PermissionError):
    """The presented token does not open this gateway. 401, and never says which part failed."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------ #
# the record
# ------------------------------------------------------------------------------------------ #

def _gateway_row(r: Any) -> dict[str, Any]:
    item = dict(r)
    item["profile"] = _loads(item.get("profile"), {"servers": []}) or {"servers": []}
    item["allowed_actions"] = _loads(item.get("allowed_actions"), list(ACTIONS)) or []
    for key in ("created_at", "updated_at", "revoked_at"):
        item[key] = _iso(item.get(key))
    item["path"] = f"/mcp/gw/{item['id']}"
    return item


def _token_public(r: Any) -> dict[str, Any]:
    """A token as a screen sees it: never the token or its hash."""
    expires = r["expires_at"]
    return {"id": r["id"], "label": r["label"], "last4": r["last4"],
            "expires_at": _iso(expires), "revoked_at": _iso(r["revoked_at"]),
            "last_used_at": _iso(r["last_used_at"]), "created_by": r["created_by"],
            "created_at": _iso(r["created_at"]),
            "status": ("revoked" if r["revoked_at"] else
                       "expired" if expires is not None and expires <= _now() else "active")}


def validate_profile(connection: Connection, project_id: str, profile: Any) -> dict[str, Any]:
    """The profile normalized, or :class:`GatewayError` naming the problem. Every server must be
    a registered server of THIS project."""
    from .mcp_registry import get_mcp_server

    if not isinstance(profile, dict) or not isinstance(profile.get("servers"), list) \
            or not profile["servers"]:
        raise GatewayError("profile.servers must list at least one server")
    seen: set[str] = set()
    out = []
    for entry in profile["servers"]:
        if not isinstance(entry, dict) or not str(entry.get("server_id") or "").strip():
            raise GatewayError("each profile server needs a server_id")
        server_id = str(entry["server_id"]).strip()
        if server_id in seen:
            raise GatewayError(f"server {server_id} is listed twice")
        seen.add(server_id)
        if get_mcp_server(connection, project_id, server_id) is None:
            raise GatewayError(f"no MCP server {server_id} in this project")
        tools = entry.get("tools")
        if tools is not None and (not isinstance(tools, list)
                                  or not all(isinstance(t, str) and t for t in tools)):
            raise GatewayError("profile tools must be a list of tool names, or null for all")
        caps = entry.get("caps") or {}
        if not isinstance(caps, dict) or any(v not in CAPS for v in caps.values()):
            raise GatewayError("caps map tool names to 'allow' or 'off'")
        out.append({"server_id": server_id, "tools": list(tools) if tools is not None else None,
                    "caps": {str(k): str(v) for k, v in caps.items()}})
    return {"servers": out}


def _validated_actions(actions: Any) -> list[str]:
    chosen = list(actions) if actions is not None else list(ACTIONS)
    if not chosen or any(a not in ACTIONS for a in chosen):
        raise GatewayError(f"allowed_actions are {', '.join(ACTIONS)}")
    return sorted(set(chosen), key=ACTIONS.index)


def _expiry(days: Any) -> Optional[datetime]:
    if days is None:
        return _now() + timedelta(days=90)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_TOKEN_DAYS:
        raise GatewayError(f"expires_in_days must be 1-{MAX_TOKEN_DAYS}")
    return _now() + timedelta(days=days)


def mint_token(connection: Connection, project_id: str, gateway_id: str, *, actor: str,
               expires_in_days: Optional[int] = None, label: Optional[str] = None
               ) -> dict[str, Any]:
    """A new token for an active gateway. The token is in the answer ONCE; only its hash stays."""
    gateway = get_gateway(connection, project_id, gateway_id)
    if gateway is None:
        raise LookupError("gateway not found")
    if gateway["status"] != "active":
        raise GatewayError("this gateway is revoked; create a new one")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = make_id("mcpgwtok")
    connection.execute(text(
        "INSERT INTO mcp_gateway_tokens (id, gateway_id, project_id, token_hash, last4, label, "
        "expires_at, created_by) VALUES (:id, :g, :p, :h, :l4, :label, :exp, :actor)"),
        {"id": token_id, "g": gateway_id, "p": project_id, "h": hash_token(token),
         "l4": token[-4:], "label": (label or "").strip()[:80] or None,
         "exp": _expiry(expires_in_days), "actor": actor})
    emit_event(connection, project_id, "mcp.gateway.token_minted", actor, "mcp_gateway",
               gateway_id, {"token_id": token_id, "last4": token[-4:]})
    row = connection.execute(text("SELECT * FROM mcp_gateway_tokens WHERE id = :id"),
                             {"id": token_id}).mappings().one()
    return {"token": token, "record": _token_public(row)}


def create_gateway(connection: Connection, project_id: str, *, name: str, profile: Any,
                   actor: str, allowed_actions: Any = None,
                   expires_in_days: Optional[int] = None) -> dict[str, Any]:
    """A gateway and its first token (shown once)."""
    name = str(name or "").strip()
    if not name or len(name) > 80:
        raise GatewayError("a gateway needs a name (up to 80 characters)")
    checked = validate_profile(connection, project_id, profile)
    actions = _validated_actions(allowed_actions)
    _expiry(expires_in_days)
    taken = connection.execute(text(
        "SELECT 1 FROM mcp_gateways WHERE project_id = :p AND lower(name) = lower(:n) "
        "AND status = 'active'"), {"p": project_id, "n": name}).first()
    if taken:
        raise GatewayConflict(f"an active gateway named '{name}' already exists")
    gateway_id = make_id("mcpgw")
    connection.execute(text(
        "INSERT INTO mcp_gateways (id, project_id, name, profile, allowed_actions, created_by) "
        "VALUES (:id, :p, :n, CAST(:profile AS JSONB), CAST(:actions AS JSONB), :actor)"),
        {"id": gateway_id, "p": project_id, "n": name, "profile": json.dumps(checked),
         "actions": json.dumps(actions), "actor": actor})
    emit_event(connection, project_id, "mcp.gateway.created", actor, "mcp_gateway", gateway_id,
               {"name": name, "servers": [s["server_id"] for s in checked["servers"]],
                "allowed_actions": actions})
    minted = mint_token(connection, project_id, gateway_id, actor=actor,
                        expires_in_days=expires_in_days, label="first")
    return {"gateway": get_gateway(connection, project_id, gateway_id), **minted}


def get_gateway(connection: Connection, project_id: str, gateway_id: str
                ) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM mcp_gateways WHERE id = :id AND project_id = :p"),
        {"id": gateway_id, "p": project_id}).mappings().first()
    if row is None:
        return None
    item = _gateway_row(row)
    tokens = connection.execute(text(
        "SELECT * FROM mcp_gateway_tokens WHERE gateway_id = :g ORDER BY created_at DESC"),
        {"g": gateway_id}).mappings().all()
    item["tokens"] = [_token_public(t) for t in tokens]
    return item


def list_gateways(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    ids = connection.execute(text(
        "SELECT id FROM mcp_gateways WHERE project_id = :p ORDER BY status, created_at DESC"),
        {"p": project_id}).scalars().all()
    return [g for g in (get_gateway(connection, project_id, i) for i in ids) if g]


def revoke_token(connection: Connection, project_id: str, gateway_id: str, token_id: str, *,
                 actor: str) -> dict[str, Any]:
    changed = connection.execute(text(
        "UPDATE mcp_gateway_tokens SET revoked_at = now(), revoked_by = :actor "
        "WHERE id = :t AND gateway_id = :g AND project_id = :p AND revoked_at IS NULL"),
        {"t": token_id, "g": gateway_id, "p": project_id, "actor": actor}).rowcount
    if not changed and not connection.execute(text(
            "SELECT 1 FROM mcp_gateway_tokens WHERE id = :t AND gateway_id = :g "
            "AND project_id = :p"), {"t": token_id, "g": gateway_id, "p": project_id}).first():
        raise LookupError("token not found")
    if changed:
        emit_event(connection, project_id, "mcp.gateway.token_revoked", actor, "mcp_gateway",
                   gateway_id, {"token_id": token_id})
    gateway = get_gateway(connection, project_id, gateway_id)
    assert gateway is not None
    return gateway


def revoke_gateway(connection: Connection, project_id: str, gateway_id: str, *,
                   actor: str) -> dict[str, Any]:
    if get_gateway(connection, project_id, gateway_id) is None:
        raise LookupError("gateway not found")
    connection.execute(text(
        "UPDATE mcp_gateways SET status = 'revoked', revoked_at = COALESCE(revoked_at, now()), "
        "revoked_by = COALESCE(revoked_by, :actor), updated_at = now() "
        "WHERE id = :g AND project_id = :p"), {"g": gateway_id, "p": project_id, "actor": actor})
    connection.execute(text(
        "UPDATE mcp_gateway_tokens SET revoked_at = now(), revoked_by = :actor "
        "WHERE gateway_id = :g AND revoked_at IS NULL"), {"g": gateway_id, "actor": actor})
    emit_event(connection, project_id, "mcp.gateway.revoked", actor, "mcp_gateway", gateway_id,
               {})
    gateway = get_gateway(connection, project_id, gateway_id)
    assert gateway is not None
    return gateway


# ------------------------------------------------------------------------------------------ #
# authentication
# ------------------------------------------------------------------------------------------ #

def bearer_of(headers: Any) -> str:
    lowered = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    scheme, _, token = lowered.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def authenticate(connection: Connection, gateway_id: str, presented: str) -> dict[str, Any]:
    """The gateway a presented token opens, or :class:`GatewayAuthError`.

    The token is hashed and looked up by hash (an index, never a scan); the stored hash is then
    compared to the presented one in constant time, and the token must belong to THIS gateway,
    be unrevoked and unexpired, and its gateway active. Every refusal reads the same."""
    refused = GatewayAuthError("a valid gateway token is required")
    if not presented.startswith(TOKEN_PREFIX) or len(presented) > 200:
        raise refused
    digest = hash_token(presented)
    row = connection.execute(text(
        "SELECT t.id AS token_id, t.token_hash, t.gateway_id, t.expires_at, t.revoked_at, "
        "g.status, g.project_id FROM mcp_gateway_tokens t JOIN mcp_gateways g "
        "ON g.id = t.gateway_id WHERE t.token_hash = :h"), {"h": digest}).mappings().first()
    if row is None or not hmac.compare_digest(str(row["token_hash"]), digest):
        raise refused
    if not hmac.compare_digest(str(row["gateway_id"]), str(gateway_id)):
        raise refused
    if row["revoked_at"] is not None or row["status"] != "active":
        raise refused
    if row["expires_at"] is not None and row["expires_at"] <= _now():
        raise refused
    connection.execute(text("UPDATE mcp_gateway_tokens SET last_used_at = now() WHERE id = :id"),
                       {"id": row["token_id"]})
    gateway = get_gateway(connection, str(row["project_id"]), str(gateway_id))
    if gateway is None:
        raise refused
    gateway["token_id"] = row["token_id"]
    return gateway


def verify_request(engine: Any, gateway_id: str, headers: Any) -> dict[str, Any]:
    """The identity guard's verifier for /mcp/gw/{gateway_id} (public:mcp_gateway_token)."""
    with engine.begin() as connection:
        return authenticate(connection, gateway_id, bearer_of(headers))


# ------------------------------------------------------------------------------------------ #
# the profile as tools
# ------------------------------------------------------------------------------------------ #

def _profile_entries(connection: Connection, gateway: dict[str, Any]
                     ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    from .mcp_registry import get_mcp_server

    out = []
    for entry in gateway["profile"].get("servers") or []:
        server = get_mcp_server(connection, gateway["project_id"], entry["server_id"])
        if server is not None and server.get("enabled"):
            out.append((entry, server))
    return out


def _runs_unattended(server: dict[str, Any], entry: dict[str, Any], tool: str) -> bool:
    """Would this tool run through the gateway with nobody present? The cap, then the
    registry's own review and Allow/Ask/Off (read, never re-decided here)."""
    from .mcp_registry import needs_person, tool_policy, tool_quarantined

    if entry.get("caps", {}).get(tool) == "off":
        return False
    wanted = entry.get("tools")
    if wanted is not None and not any(fnmatch.fnmatchcase(tool, w) for w in wanted):
        return False
    if needs_person(server):
        return False
    declared = next((t.get("risk_tier") for t in server.get("tools") or []
                     if isinstance(t, dict) and t.get("name") == tool), None)
    return tool_policy(server, tool, declared) == "allow" and not tool_quarantined(server, tool)


def exposed_tools(connection: Connection, gateway: dict[str, Any]) -> list[dict[str, Any]]:
    """Every tool the gateway serves, as MCP tool descriptors named `<server>__<tool>`."""
    out = []
    for entry, server in _profile_entries(connection, gateway):
        for tool in server.get("tools") or []:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            if not _runs_unattended(server, entry, str(tool["name"])):
                continue
            out.append({
                "name": f"{server['name']}{SEPARATOR}{tool['name']}",
                "description": f"[{server['name']}] {tool.get('description') or ''}".strip(),
                "inputSchema": tool.get("inputSchema") or {"type": "object"},
            })
    return out


def _meta_tools(gateway: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = set(gateway["allowed_actions"])
    tools = []
    if "search_tools" in allowed:
        tools.append({"name": "search_tools",
                      "description": "Find tools in this gateway by words; returns names, "
                                     "descriptions and input schemas.",
                      "inputSchema": {"type": "object", "properties": {
                          "query": {"type": "string"},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                          "required": ["query"]}})
    if "run_tool" in allowed:
        tools.append({"name": "run_tool",
                      "description": "Call one tool of this gateway by its full name "
                                     "(<server>__<tool>) with its arguments.",
                      "inputSchema": {"type": "object", "properties": {
                          "name": {"type": "string"},
                          "arguments": {"type": "object"}}, "required": ["name"]}})
    return tools


def search(connection: Connection, gateway: dict[str, Any], query: str, limit: int = 10
           ) -> list[dict[str, Any]]:
    words = [w for w in str(query or "").lower().split() if w]
    found = []
    for tool in exposed_tools(connection, gateway):
        hay = f"{tool['name']} {tool['description']}".lower()
        score = sum(hay.count(w) for w in words) if words else 1
        if score:
            found.append((score, tool))
    found.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [t for _, t in found[:max(1, min(int(limit or 10), 50))]]


def _resolve(connection: Connection, gateway: dict[str, Any], name: str
             ) -> Optional[tuple[dict[str, Any], dict[str, Any], str]]:
    server_name, sep, tool = str(name or "").partition(SEPARATOR)
    if not sep or not tool:
        return None
    for entry, server in _profile_entries(connection, gateway):
        if server["name"] == server_name:
            return entry, server, tool
    return None


# ------------------------------------------------------------------------------------------ #
# the JSON-RPC surface
# ------------------------------------------------------------------------------------------ #

def _ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_text(payload: Any, *, is_error: bool = False, meta: Optional[dict] = None) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    out: dict[str, Any] = {"content": [{"type": "text", "text": body}], "isError": is_error}
    if meta:
        out["_meta"] = meta
    return out


def _tenant_scope(connection: Connection, project_id: str) -> tuple[str, str]:
    row = connection.execute(text(
        "SELECT w.org_id, p.workspace_id FROM projects p JOIN workspaces w "
        "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).mappings().first()
    if not row:
        raise RuntimeError(f"project {project_id} has no workspace")
    return str(row["org_id"]), str(row["workspace_id"])


def _call(engine: Any, settings: Any, gateway: dict[str, Any], name: str,
          arguments: Any) -> dict[str, Any]:
    """One proxied call: the cap here, then call_tool_with_receipt for everything else."""
    from .mcp_registry import McpServerError, call_tool_with_receipt
    from .tool_call_audit import record_tool_call

    if not isinstance(arguments, dict):
        return _tool_text("arguments must be an object", is_error=True)
    run_ref = f"mcpgw:{gateway['id']}"
    with engine.begin() as connection:
        resolved = _resolve(connection, gateway, name)
        if resolved is None:
            return _tool_text(f"no tool '{name}' in this gateway", is_error=True)
        entry, server, tool = resolved
        wanted = entry.get("tools")
        capped = entry.get("caps", {}).get(tool) == "off" or (
            wanted is not None and not any(fnmatch.fnmatchcase(tool, w) for w in wanted))
        if capped:
            # The gateway's own refusal, audited like the registry's.
            record_tool_call(connection, project_id=gateway["project_id"], run_id=run_ref,
                             agent="mcp_curated_gateway", principal=None,
                             server_id=server["id"], tool=tool, arguments=arguments,
                             decision="off", matched_policy_ids=["gateway_cap"],
                             decision_id=None, outcome="refused", latency_ms=0)
            return _tool_text(f"{tool} on '{server['name']}' is not offered by this gateway",
                              is_error=True)
        call_ref = secrets.token_hex(8)
        try:
            called = call_tool_with_receipt(
                connection, settings, gateway["project_id"], server, tool, arguments,
                dedupe_key=f"mcpgw-ext:{gateway['id']}:{server['id']}:{tool}:{call_ref}",
                tenant_scope=_tenant_scope(connection, gateway["project_id"]),
                approved_by=None, approval_about={"run_id": run_ref})
        except McpServerError as refused:
            return _tool_text(str(refused), is_error=True)
        except Exception as exc:  # noqa: BLE001 — the client reads it; the receipt kept it
            return _tool_text(f"{tool} failed: {str(exc)[:1500]}", is_error=True)
    meta: dict[str, Any] = {"boss/receipt_id": called.get("receipt_id")}
    if called.get("untrusted"):
        meta["boss/untrusted"] = True
    return _tool_text(called.get("output"), meta=meta)


def handle(engine: Any, settings: Any, gateway: dict[str, Any], message: Any
           ) -> Optional[dict[str, Any]]:
    """One JSON-RPC message from an outside client; None for a notification."""
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return _err(None, -32600, "invalid request")
    method, rid = message["method"], message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if rid is None:
        return None
    allowed = set(gateway["allowed_actions"])
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return _ok(rid, {
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"boss-gateway:{gateway['name']}", "version": "1.0.0"},
            "instructions": ("A curated BOSS gateway. Every call is governed and audited by "
                             "BOSS; tools that need a person's approval are not offered here."),
        })
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        if "tools/list" not in allowed:
            return _err(rid, -32601, "tools/list is not allowed on this gateway")
        with engine.begin() as connection:
            tools = exposed_tools(connection, gateway)
        return _ok(rid, {"tools": tools + _meta_tools(gateway)})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if params.get("arguments") is not None else {}
        if name == "search_tools":
            if "search_tools" not in allowed:
                return _err(rid, -32601, "search_tools is not allowed on this gateway")
            with engine.begin() as connection:
                found = search(connection, gateway, str((arguments or {}).get("query") or ""),
                               int((arguments or {}).get("limit") or 10))
            return _ok(rid, _tool_text({"tools": found}))
        if name == "run_tool":
            if "run_tool" not in allowed:
                return _err(rid, -32601, "run_tool is not allowed on this gateway")
            inner = arguments if isinstance(arguments, dict) else {}
            return _ok(rid, _call(engine, settings, gateway, str(inner.get("name") or ""),
                                  inner.get("arguments") if inner.get("arguments") is not None
                                  else {}))
        if "tools/call" not in allowed:
            return _err(rid, -32601, "tools/call is not allowed on this gateway")
        return _ok(rid, _call(engine, settings, gateway, name, arguments))
    return _err(rid, -32601, f"method {method} is not supported")
