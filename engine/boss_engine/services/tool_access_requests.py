"""An agent asks for MCP tools it does not hold; a person grants exactly those (BOSSAPP-848).

WHY THIS EXISTS
---------------
An agent that found the tool it needs on a server it does not hold had two roads, both wrong:
stop and tell a person in prose ("give me Linear"), or have the operator grant the WHOLE server
in Skills & MCP. Neither says which tools, why, or how risky they are, and the whole-server grant
gives far more than was asked.

THE LIFECYCLE
-------------
* REQUEST (`request_tool_access`, the registry tool in tools/builtin.py): the agent names the
  server, the tools and why. A TYPED decision opens: the Grant card lists exactly those tools,
  each with its tier and the policy it would get, the schema hash the person is looking at, and
  the reason in the agent's words.
* GRANT (decisions._handle_approved_follow_on, action `grant_tool_access`): the role gets a
  holder grant (role_mcp_servers) for exactly those tools, plus one `mcp_access_grants` row per
  tool: Allow only for tier <= 1, Ask otherwise (the runtime floor says the same, and the
  server's own per-tool choice still wins when it is stricter), PINNED to the schema hash the
  card showed. A tool that changed while the card waited refuses the approval.
* DROP (`enforce_pins`, run where an agent's holdings are read): a tool whose schema no longer
  matches its pin, or that is gone, loses the grant — the BOSSAPP-818 rule that a changed tool
  is not trusted on an old consent. The agent asks again and a person sees the new definition.
* REFUSE: a No is recorded per tool (status `refused`); asking for the same tools again within a
  day answers with that refusal instead of a new card.
* REVOKE (`revoke_grant`, the console where holders are managed): the grant ends and its tool
  leaves the role, unless the role held it before the grant.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

ACTION = "grant_tool_access"
DECISION_REASON = "tool_access_request"
#: A refused request is not asked again for this long.
REFUSAL_QUIET_HOURS = 24
MAX_TOOLS = 20


class ToolAccessError(ValueError):
    """Something the agent or person can act on, said for them."""


def policy_for_tier(tier: int) -> str:
    """The tier cap: Allow only for tier <= 1, Ask otherwise."""
    return "allow" if int(tier) <= 1 else "ask"


def _role_exists(connection: Connection, project_id: str, role_key: str) -> bool:
    return bool(connection.execute(text(
        "SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key}).scalar())


def requesting_role(connection: Connection, project_id: str, actor: str) -> Optional[str]:
    """The agent role a request comes from: the agent this run acts as, else the actor's name
    (`agent:<role>` or a bare role key). Never a role named in the request itself."""
    from .connection_credentials import _ACTING_AGENT

    for candidate in (_ACTING_AGENT.get(), str(actor or "").removeprefix("agent:")):
        role = str(candidate or "").strip().lower()
        if role and role != "human" and _role_exists(connection, project_id, role):
            return role
    return None


def _entry(server: dict[str, Any], tool: str) -> Optional[dict[str, Any]]:
    return next((t for t in server.get("tools") or []
                 if isinstance(t, dict) and t.get("name") == tool), None)


def _held_tools(connection: Connection, project_id: str, role: str, server_id: str
                ) -> Optional[set[str]]:
    """None: the role does not hold the server; empty set: it holds every tool."""
    row = connection.execute(text(
        "SELECT tools FROM role_mcp_servers WHERE project_id = :p AND role_key = :r "
        "AND mcp_server_id = :s"), {"p": project_id, "r": role, "s": server_id}).first()
    if row is None:
        return None
    tools = row[0]
    if isinstance(tools, str):
        tools = json.loads(tools or "[]")
    return {str(t) for t in tools or []}


def _holds(held: Optional[set[str]], tool: str) -> bool:
    return held is not None and (not held or tool in held)


def request_access(connection: Connection, project_id: str, *, role_key: str, server_ref: str,
                   tools: list[str], reason: str, actor: str,
                   run_id: Optional[str] = None) -> dict[str, Any]:
    """Open the Grant card for exactly these tools (or say why not). Never grants anything."""
    from .mcp_registry import find_mcp_server, tool_quarantined, tool_tier

    role = str(role_key or "").strip().lower()
    if not role or not _role_exists(connection, project_id, role):
        raise ToolAccessError("Only an agent of this project can ask for tools.")
    server = find_mcp_server(connection, project_id, str(server_ref or "").strip())
    if server is None:
        raise ToolAccessError(f"No MCP server {server_ref!r} in this project.")
    wanted = sorted({str(t).strip() for t in tools or [] if str(t).strip()})
    if not wanted:
        raise ToolAccessError("Name the tools you need.")
    if len(wanted) > MAX_TOOLS:
        raise ToolAccessError(f"Ask for at most {MAX_TOOLS} tools at once.")
    unknown = [t for t in wanted if _entry(server, t) is None]
    if unknown:
        raise ToolAccessError(f"{server['name']} has no tool {', '.join(unknown)} (as of its last "
                              "check). List its tools first.")
    why = " ".join(str(reason or "").split())
    if len(why) < 8:
        raise ToolAccessError("Say why you need them, in a sentence.")
    held = _held_tools(connection, project_id, role, server["id"])
    missing = [t for t in wanted if not _holds(held, t)]
    if not missing:
        return {"status": "already_held", "server": server["name"], "tools": wanted}
    refused = connection.execute(text(
        "SELECT DISTINCT decision_id FROM mcp_access_grants WHERE project_id = :p "
        "AND server_id = :s AND role_key = :r AND status = 'refused' AND tool = ANY(:t) "
        "AND ended_at > now() - make_interval(hours => :h)"),
        {"p": project_id, "s": server["id"], "r": role, "t": missing,
         "h": REFUSAL_QUIET_HOURS}).scalars().all()
    if refused:
        return {"status": "refused_recently", "decision_id": str(refused[0]),
                "server": server["name"],
                "message": "A person said no to these tools in the last day. Do the work another "
                           "way, or ask them directly."}
    items = []
    for tool in missing:
        entry = _entry(server, tool) or {}
        tier = tool_tier(server, tool)
        items.append({"tool": tool, "tier": tier, "policy": policy_for_tier(tier),
                      "schema_sha256": str(entry.get("schema_sha256") or ""),
                      "description": str(entry.get("description") or "")[:300],
                      "quarantined": tool_quarantined(server, tool)})
    names = ", ".join(i["tool"] for i in items)
    question = (f"Grant {role} {len(items)} tool{'s' if len(items) != 1 else ''} on "
                f"{server['name']}: {names}?")
    rec = {"action": ACTION, "card": "grant", "server_id": server["id"],
           "server": server["name"], "role_key": role, "tools": items, "reason": why[:1000],
           "run_id": run_id, "requested_by": actor}
    decision_id = create_blocking_decision(connection, project_id, None, question, rec, {},
                                           DECISION_REASON,
                                           risk_tier=max(2, max(i["tier"] for i in items)))
    emit_event(connection, project_id, "mcp.access_requested", actor, "decision", decision_id,
               {"role_key": role, "server_id": server["id"], "tools": [i["tool"] for i in items]})
    return {"status": "asked", "decision_id": decision_id, "server": server["name"],
            "tools": items}


def _set_role_tools(connection: Connection, project_id: str, role: str, server_id: str,
                    tools: Optional[set[str]], actor: str) -> None:
    """Write the role's holder grant: `None` removes it; a set is exactly those tools."""
    from .agents import grant_mcp, revoke_mcp

    if tools is None:
        revoke_mcp(connection, project_id, actor=actor, role_key=role, mcp_server_id=server_id)
    else:
        grant_mcp(connection, project_id, actor=actor, role_key=role, mcp_server_id=server_id,
                  tools=sorted(tools))


def on_approved(connection: Connection, settings: Any, row: dict[str, Any], actor: str) -> None:
    """THE APPROVAL ARM: holder grants for exactly the tools on the card, tier-capped and pinned.
    Raises ToolAccessError (the approval is then not recorded) when the server or a tool changed
    since the card was drawn."""
    from .mcp_registry import get_mcp_server, tool_tier

    rec = row.get("recommendation") or {}
    project_id = str(row["project_id"])
    role = str(rec.get("role_key") or "")
    server = get_mcp_server(connection, project_id, str(rec.get("server_id") or ""))
    if server is None:
        raise ToolAccessError("That MCP server was removed since the agent asked.")
    if not _role_exists(connection, project_id, role):
        raise ToolAccessError(f"The agent {role} is no longer on this project.")
    items = [i for i in rec.get("tools") or [] if isinstance(i, dict) and i.get("tool")]
    for item in items:
        entry = _entry(server, str(item["tool"]))
        if entry is None:
            raise ToolAccessError(f"{item['tool']} is no longer offered by {server['name']}.")
        if str(entry.get("schema_sha256") or "") != str(item.get("schema_sha256") or ""):
            raise ToolAccessError(f"{item['tool']} changed since the agent asked, so this grant "
                                  "was not recorded. The agent can ask again for what it is now.")
    held = _held_tools(connection, project_id, role, server["id"])
    granted = []
    for item in items:
        tool = str(item["tool"])
        tier = tool_tier(server, tool)
        connection.execute(text(
            "INSERT INTO mcp_access_grants (id, project_id, server_id, role_key, tool, tier, "
            "policy, schema_sha256, previously_held, decision_id, reason, status, granted_by) "
            "VALUES (:id, :p, :s, :r, :t, :tier, :pol, :sha, :prev, :d, :why, 'active', :by) "
            "ON CONFLICT (project_id, server_id, role_key, tool) WHERE status = 'active' "
            "DO UPDATE SET tier = EXCLUDED.tier, policy = EXCLUDED.policy, "
            "schema_sha256 = EXCLUDED.schema_sha256, decision_id = EXCLUDED.decision_id, "
            "reason = EXCLUDED.reason, granted_by = EXCLUDED.granted_by"),
            {"id": make_id("mag"), "p": project_id, "s": server["id"], "r": role, "t": tool,
             "tier": tier, "pol": policy_for_tier(tier),
             "sha": str(_entry(server, tool).get("schema_sha256") or ""),
             "prev": _holds(held, tool), "d": row["id"], "why": rec.get("reason"), "by": actor})
        granted.append({"tool": tool, "tier": tier, "policy": policy_for_tier(tier)})
    if held is None:
        _set_role_tools(connection, project_id, role, server["id"],
                        {g["tool"] for g in granted}, actor)
    elif held:                                  # an empty set already holds every tool
        _set_role_tools(connection, project_id, role, server["id"],
                        held | {g["tool"] for g in granted}, actor)
    emit_event(connection, project_id, "mcp.access_granted", actor, "decision", str(row["id"]),
               {"role_key": role, "server_id": server["id"], "tools": granted})


def on_refused(connection: Connection, row: dict[str, Any], actor: str) -> None:
    """A person said No: kept per tool, so the agent is told and does not ask again for a day."""
    rec = row.get("recommendation") or {}
    project_id = str(row["project_id"])
    for item in rec.get("tools") or []:
        if not isinstance(item, dict) or not item.get("tool"):
            continue
        connection.execute(text(
            "INSERT INTO mcp_access_grants (id, project_id, server_id, role_key, tool, tier, "
            "policy, schema_sha256, decision_id, reason, status, granted_by, ended_by, "
            "ended_reason, ended_at) SELECT :id, :p, :s, :r, :t, :tier, :pol, :sha, :d, :why, "
            "'refused', :by, :by, 'refused', now() WHERE EXISTS (SELECT 1 FROM mcp_servers "
            "WHERE id = :s)"),
            {"id": make_id("mag"), "p": project_id, "s": str(rec.get("server_id") or ""),
             "r": str(rec.get("role_key") or ""), "t": str(item["tool"]),
             "tier": int(item.get("tier") or 0), "pol": policy_for_tier(int(item.get("tier") or 0)),
             "sha": str(item.get("schema_sha256") or ""), "d": row["id"],
             "why": rec.get("reason"), "by": actor})
    emit_event(connection, project_id, "mcp.access_refused", actor, "decision", str(row["id"]),
               {"role_key": rec.get("role_key"), "server_id": rec.get("server_id"),
                "tools": [i.get("tool") for i in rec.get("tools") or [] if isinstance(i, dict)]})


def _end(connection: Connection, grant: dict[str, Any], *, status: str, actor: str,
         why: str) -> None:
    """End one grant and take its tool off the role, unless the role held it before."""
    connection.execute(text(
        "UPDATE mcp_access_grants SET status = :st, ended_by = :by, ended_reason = :why, "
        "ended_at = now() WHERE id = :id AND status = 'active'"),
        {"st": status, "by": actor, "why": why, "id": grant["id"]})
    if grant["previously_held"]:
        return
    project_id, role, server_id = grant["project_id"], grant["role_key"], grant["server_id"]
    held = _held_tools(connection, project_id, role, server_id)
    if not held:                # not held any more, or held whole (granted by hand since)
        return
    remaining = held - {grant["tool"]}
    _set_role_tools(connection, project_id, role, server_id, remaining or None, actor)


_GRANT_COLUMNS = ("id, project_id, server_id, role_key, tool, tier, policy, schema_sha256, "
                  "previously_held, decision_id, reason, status, granted_by, ended_by, "
                  "ended_reason, created_at, ended_at")


def revoke_grant(connection: Connection, project_id: str, grant_id: str, *, actor: str
                 ) -> Optional[dict[str, Any]]:
    grant = connection.execute(text(
        f"SELECT {_GRANT_COLUMNS} FROM mcp_access_grants WHERE id = :id AND project_id = :p "
        "FOR UPDATE"), {"id": grant_id, "p": project_id}).mappings().first()
    if grant is None:
        return None
    if grant["status"] != "active":
        raise ToolAccessError("That grant has already ended.")
    _end(connection, dict(grant), status="revoked", actor=actor, why="revoked")
    emit_event(connection, project_id, "mcp.access_revoked", actor, "mcp_access_grant", grant_id,
               {"role_key": grant["role_key"], "server_id": grant["server_id"],
                "tool": grant["tool"]})
    return _public(connection.execute(text(
        f"SELECT {_GRANT_COLUMNS} FROM mcp_access_grants WHERE id = :id"),
        {"id": grant_id}).mappings().one())


def enforce_pins(connection: Connection, project_id: str, role_key: Optional[str] = None
                 ) -> list[str]:
    """Drop every live grant whose tool changed (schema hash != pin) or is gone. Returns the ids
    dropped. Cheap when there is nothing to drop; an install without the table has nothing."""
    from .mcp_registry import get_mcp_server

    try:
        with connection.begin_nested():
            grants = [dict(r) for r in connection.execute(text(
                f"SELECT {_GRANT_COLUMNS} FROM mcp_access_grants WHERE project_id = :p "
                "AND status = 'active' AND (CAST(:r AS TEXT) IS NULL OR role_key = :r)"),
                {"p": project_id, "r": role_key}).mappings()]
    except Exception:  # noqa: BLE001 — the table predates this install's schema
        return []
    dropped: list[str] = []
    servers: dict[str, Optional[dict[str, Any]]] = {}
    for grant in grants:
        sid = grant["server_id"]
        if sid not in servers:
            servers[sid] = get_mcp_server(connection, project_id, sid)
        server = servers[sid]
        entry = _entry(server, grant["tool"]) if server is not None else None
        if entry is not None and str(entry.get("schema_sha256") or "") == grant["schema_sha256"]:
            continue
        why = "tool_gone" if entry is None else "schema_changed"
        _end(connection, grant, status="dropped", actor="system", why=why)
        emit_event(connection, project_id, "mcp.access_dropped", "system", "mcp_access_grant",
                   grant["id"], {"role_key": grant["role_key"], "server_id": sid,
                                 "tool": grant["tool"], "reason": why})
        dropped.append(grant["id"])
    return dropped


def _public(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "ended_at"):
        if out.get(key) is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


def list_grants(connection: Connection, project_id: str, *, server_id: Optional[str] = None,
                role_key: Optional[str] = None, include_ended: bool = False
                ) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        f"SELECT {_GRANT_COLUMNS} FROM mcp_access_grants WHERE project_id = :p "
        "AND (CAST(:s AS TEXT) IS NULL OR server_id = :s) "
        "AND (CAST(:r AS TEXT) IS NULL OR role_key = :r) "
        "AND (CAST(:all AS BOOLEAN) OR status = 'active') ORDER BY created_at DESC"),
        {"p": project_id, "s": server_id, "r": role_key, "all": include_ended}).mappings().all()
    return [_public(r) for r in rows]
