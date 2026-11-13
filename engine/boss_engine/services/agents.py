"""ONE agent: a role, what it holds, and optionally a Studio profile.

WHY THIS MODULE EXISTS
----------------------
BOSS grew four places an "agent" lived and none of them joined:

* ``roles`` — the roster row Agent Studio lists and workflow steps name as `captain`;
* ``role_skills`` — the grant the runtime actually checks before an agent may call a skill;
* ``agent_definitions``/``agent_versions`` — Agent Studio's prompts, model policy and rules;
* nothing at all for MCP servers.

So "create an agent and give it skills" wrote the skills to `roles.spec.allowed_skills` and
`agent_versions.requested_skills` — two lists the runtime never reads — and the new agent held
nothing. It was left out of auto-pick, refused by name ("holds no skills"), and its own page said
"This agent holds no skills" straight after the wizard said it was created. Picking '+ A new role'
named "QA" silently overwrote the bundled qa role. The Create-agent wizard posted as the literal
actor 'vp_agents', which a signed-in 'ceo' key may not act as, so on every authed install it
registered an orphan role and then 403'd.

THE MODEL, STATED ONCE
----------------------
    agent   = roles row (role_key, display_name, mission, boundaries, reports_to)
            + holdings: role_skills (skills) and role_mcp_servers (MCP servers, optional tool list)
            + optional Studio profile(s): agent_definitions whose versions point at the role

Every write in this module changes the role and its holdings in the SAME transaction, so a
create that fails half-way leaves nothing behind, and a create that succeeds leaves an agent that
can already do what it was given. The API, the console and the chat tools all write through here.

WHAT AN OPERATOR REMOVES STAYS REMOVED. Every removal and edit here records a seed exclusion
(services/seed_exclusions.py), because the install/upgrade seed used to put back what a person
took away.

EVERY AGENT IS SOMEBODY (Agent World, services/agent_identity.py). A create gives the agent an
immutable agt_ id, a birth (who made it, from what), a sponsor and a v1 seal; every change to
what it is or holds — voice, remit, a grant, a revoke — seals a new version, once per request
however many things changed. The persona (`spec.persona`) is how it speaks, never what it may
do: services/agent_persona.py lints it and refuses one that tries to grant anything.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import agent_identity, seed_exclusions
from .core import emit_event, make_id


class AgentError(ValueError):
    """Something the operator can fix, phrased for them. `status` is the HTTP answer."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_UNSET: Any = object()


def key_from_name(name: str) -> str:
    """The role key an agent called `name` gets: lower_snake_case, letters first."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if slug and not slug[0].isalpha():
        slug = f"agent_{slug}"
    return slug[:64]


def _role(connection: Connection, project_id: str, role_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT id, role_key, display_name, spec, autonomy_level, reports_to, "
             "source_module_id FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).mappings().first()
    return dict(row) if row else None


def _require_role(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    role = _role(connection, project_id, role_key)
    if role is None:
        available = connection.execute(
            text("SELECT role_key FROM roles WHERE project_id = :p ORDER BY role_key LIMIT 40"),
            {"p": project_id},
        ).scalars().all()
        raise AgentError(
            f"no agent {role_key!r} on this project. Available: {', '.join(available) or 'none'}.",
            status=404)
    return role


def _check_reports_to(connection: Connection, project_id: str, role_key: str,
                      reports_to: Optional[str]) -> Optional[str]:
    target = str(reports_to or "").strip().lower() or None
    if target is None:
        return None
    if target == role_key:
        raise AgentError("an agent cannot report to itself.")
    if _role(connection, project_id, target) is None:
        raise AgentError(
            f"{target!r} is not an agent on this project, so nobody can report to it. "
            "Pick an existing agent, or leave reports-to empty.")
    # A CYCLE OF TWO OR MORE. The org chart picks a root by walking up; a loop makes it pick an
    # arbitrary one and look correct while being wrong.
    seen = {role_key}
    cursor: Optional[str] = target
    for _ in range(64):
        if cursor is None:
            break
        if cursor in seen:
            raise AgentError(
                f"{role_key} reporting to {target} would make a loop in the reporting lines.")
        seen.add(cursor)
        parent = connection.execute(
            text("SELECT reports_to FROM roles WHERE project_id = :p AND role_key = :r"),
            {"p": project_id, "r": cursor},
        ).scalar()
        cursor = str(parent).strip().lower() if parent else None
    return target


def _clean_lines(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [line for line in value.splitlines()]
    if not isinstance(value, list):
        raise AgentError(f"{field} must be a list of sentences.")
    return [str(v).strip()[:400] for v in value if str(v).strip()][:40]


def _boundaries(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {"must": [], "must_not": []}
    if not isinstance(value, dict):
        raise AgentError("boundaries must be an object with 'must' and 'must_not' lists.")
    return {"must": _clean_lines(value.get("must"), "boundaries.must"),
            "must_not": _clean_lines(value.get("must_not"), "boundaries.must_not")}


# --------------------------------------------------------------------------- #
# MCP grants
# --------------------------------------------------------------------------- #
def _mcp_entries(raw: Iterable[Any]) -> list[dict[str, Any]]:
    """`["mcp_1", {"server_id": "mcp_2", "tools": ["post"]}]` → [{server, tools}]."""
    out: list[dict[str, Any]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            entry = {"server_id": entry}
        if not isinstance(entry, dict):
            raise AgentError("each MCP server is an id, or {server_id, tools}.")
        server = str(entry.get("server_id") or entry.get("mcp_server_id") or entry.get("id")
                     or entry.get("server") or "").strip()
        if not server:
            raise AgentError("an MCP server entry is missing its server_id.")
        tools = entry.get("tools") or []
        if isinstance(tools, str):
            tools = [t for t in re.split(r"[\s,]+", tools) if t]
        if not isinstance(tools, list):
            raise AgentError("tools must be a list of tool names (empty = every tool).")
        out.append({"server": server,
                    "tools": sorted({str(t).strip() for t in tools if str(t).strip()})})
    return out


def _resolve_server(connection: Connection, project_id: str, server: str) -> dict[str, Any]:
    """By id, or by name when the name is unique — a chat model says 'mattermost', not mcp_9f."""
    row = connection.execute(
        text("SELECT id, name, url, enabled, risk_tier FROM mcp_servers "
             "WHERE project_id = :p AND id = :s"),
        {"p": project_id, "s": server},
    ).mappings().first()
    if row:
        return dict(row)
    named = connection.execute(
        text("SELECT id, name, url, enabled, risk_tier FROM mcp_servers "
             "WHERE project_id = :p AND lower(name) = lower(:s) ORDER BY created_at"),
        {"p": project_id, "s": server},
    ).mappings().all()
    if len(named) == 1:
        return dict(named[0])
    registered = connection.execute(
        text("SELECT id, name FROM mcp_servers WHERE project_id = :p ORDER BY name LIMIT 40"),
        {"p": project_id},
    ).mappings().all()
    listing = ", ".join(f"{r['name']} ({r['id']})" for r in registered) or "none registered"
    if len(named) > 1:
        raise AgentError(
            f"{len(named)} MCP servers are called {server!r}; name one by id. Registered: "
            f"{listing}.")
    raise AgentError(
        f"no MCP server {server!r} on this project. Registered: {listing}. Register it on the "
        "Skills and MCP page first.")


def grant_mcp(connection: Connection, project_id: str, *, actor: str, role_key: str,
              mcp_server_id: str, tools: Optional[list[str]] = None) -> dict[str, Any]:
    """Give one MCP server (optionally only some of its tools) to one agent. Idempotent."""
    role = str(role_key or "").strip().lower()
    _require_role(connection, project_id, role)
    server = _resolve_server(connection, project_id, str(mcp_server_id or "").strip())
    allow = sorted({str(t).strip() for t in (tools or []) if str(t).strip()})
    row = connection.execute(
        text(
            "INSERT INTO role_mcp_servers (id, project_id, role_key, mcp_server_id, tools, "
            "assigned_by) VALUES (:id, :p, :r, :s, CAST(:tools AS JSONB), :by) "
            "ON CONFLICT (project_id, role_key, mcp_server_id) DO UPDATE "
            "SET tools = EXCLUDED.tools RETURNING id"
        ),
        {"id": make_id("rmcp"), "p": project_id, "r": role, "s": server["id"],
         "tools": json.dumps(allow), "by": actor},
    ).scalar()
    # GIVEN BY HAND, so an earlier removal no longer keeps the seed from granting it.
    seed_exclusions.lift(connection, project_id, "role_skill",
                         role_mcp_subject(role, server["name"]))
    emit_event(connection, project_id, "mcp.assigned_to_agent", actor, "role_mcp_server",
               str(row), {"role_key": role, "mcp_server_id": server["id"],
                          "server": server["name"], "tools": allow})
    agent_identity.quietly(connection, agent_identity.seal_identity, project_id, role,
                           "grant", actor)
    return {"role_key": role, "mcp_server_id": server["id"], "server": server["name"],
            "tools": allow, "assigned": True,
            "enabled": bool(server["enabled"])}


def revoke_mcp(connection: Connection, project_id: str, *, actor: str, role_key: str,
               mcp_server_id: str) -> dict[str, Any]:
    role = str(role_key or "").strip().lower()
    server = _resolve_server(connection, project_id, str(mcp_server_id or "").strip())
    removed = connection.execute(
        text("DELETE FROM role_mcp_servers WHERE project_id = :p AND role_key = :r "
             "AND mcp_server_id = :s RETURNING id"),
        {"p": project_id, "r": role, "s": server["id"]},
    ).scalar()
    if not removed:
        raise AgentError(f"{role} does not hold the MCP server {server['name']!r}.",
                         status=404)
    # A REMOVAL THE NEXT SEED MUST NOT UNDO: role specs now name the servers their agent holds
    # (permissions.mcp_servers), and sync_role_mcp grants them on every install and upgrade.
    seed_exclusions.exclude(connection, project_id, "role_skill",
                            role_mcp_subject(role, server["name"]), actor=actor,
                            reason="MCP server taken off the agent")
    emit_event(connection, project_id, "mcp.unassigned_from_agent", actor, "role_mcp_server",
               str(removed), {"role_key": role, "mcp_server_id": server["id"],
                              "server": server["name"]})
    agent_identity.quietly(connection, agent_identity.seal_identity, project_id, role,
                           "revoke", actor)
    return {"role_key": role, "mcp_server_id": server["id"], "server": server["name"],
            "assigned": False}


def role_mcp_subject(role_key: str, server_name: str) -> str:
    """The seed-exclusion subject for "this agent no longer holds this server".

    Recorded under the `role_skill` kind (the schema's CHECK allows four kinds) with an `mcp:`
    prefix no skill id can have — skill ids are dotted (`mcp.<server>.<tool>`), never colons.
    By server NAME, because built-in servers are ensured by name and a re-registered one gets a
    new id.
    """
    return seed_exclusions.role_skill_subject(role_key, f"mcp:{str(server_name).strip()}")


def sync_role_mcp(connection: Connection, *, project_id: str,
                  actor: str = "system") -> dict[str, Any]:
    """Give every installed agent the MCP servers its role spec names. The seed's half of the
    MCP grant, exactly as sync_role_skills is for skills.

    A role spec names servers by NAME under `permissions.mcp_servers` ({server, tools}). Additive
    only: a grant that already exists is left as the operator set it (its tool list included),
    a server the operator took off the agent stays off (seed_exclusions), and a server this
    project has not registered is reported, never invented.
    """
    from .mcp_registry import list_mcp_servers

    removed = seed_exclusions.excluded(connection, project_id, "role_skill")
    servers = {str(s["name"]).strip().lower(): s
               for s in list_mcp_servers(connection, project_id)}
    held = {(r["role_key"], r["mcp_server_id"]) for r in connection.execute(
        text("SELECT role_key, mcp_server_id FROM role_mcp_servers WHERE project_id = :p"),
        {"p": project_id}).mappings().all()}
    granted: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    kept_off: list[dict[str, str]] = []
    roles = connection.execute(
        text("SELECT role_key, spec FROM roles WHERE project_id = :p ORDER BY role_key"),
        {"p": project_id},
    ).mappings().all()
    for row in roles:
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        wanted = ((spec.get("permissions") or {}).get("mcp_servers")) or []
        for entry in wanted if isinstance(wanted, list) else []:
            if isinstance(entry, str):
                entry = {"server": entry}
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("server") or "").strip()
            if not name:
                continue
            if role_mcp_subject(row["role_key"], name) in removed:
                kept_off.append({"role_key": row["role_key"], "server": name})
                continue
            server = servers.get(name.lower())
            if server is None:
                missing.append({"role_key": row["role_key"], "server": name,
                                "reason": "not registered on this project"})
                continue
            if (row["role_key"], server["id"]) in held:
                continue
            tools = [str(t) for t in entry.get("tools") or [] if str(t).strip()]
            grant_mcp(connection, project_id, actor=actor, role_key=row["role_key"],
                      mcp_server_id=server["id"], tools=tools)
            granted.append({"role_key": row["role_key"], "server": name, "tools": tools})
    # AND THE ABILITIES ITS ROLE NAMES (`permissions.abilities`, skills-mcp spec §6.2): each one
    # expands through the kit file into the same role_skills and role_mcp_servers rows, run here
    # because the servers they name exist by now. Additive; a removal stays removed.
    from ..config import get_settings
    from .kit import sync_role_abilities

    abilities: dict[str, Any] = {}
    try:
        with connection.begin_nested():
            abilities = sync_role_abilities(connection, get_settings(), project_id, actor=actor)
    except Exception as error:  # noqa: BLE001 — a fresh project must still come up; say why
        abilities = {"granted": [], "unavailable": [{"reason": str(error)}]}
    return {"granted": granted, "granted_count": len(granted), "unavailable": missing,
            "kept_off_by_operator": kept_off, "abilities": abilities}


def held_mcp_servers(connection: Connection, project_id: str, role_key: str, *,
                     enabled_only: bool = True) -> list[dict[str, Any]]:
    """The MCP servers one agent HOLDS, as full server rows plus its tool allowlist.

    What a bounded agent run is offered. A disabled server is left out: disabling one is the
    project-wide off switch, and a grant does not get to override it.
    """
    from .mcp_registry import list_mcp_servers
    from .tool_access_requests import enforce_pins

    # A tool granted on request is held only while its schema matches the one the person
    # approved (BOSSAPP-848): a changed one is dropped here, before it is offered.
    enforce_pins(connection, project_id, str(role_key or "").strip().lower() or None)
    grants = {
        str(r["mcp_server_id"]): list(r["tools"] or [])
        for r in connection.execute(
            text("SELECT mcp_server_id, tools FROM role_mcp_servers "
                 "WHERE project_id = :p AND role_key = :r"),
            {"p": project_id, "r": str(role_key or "").strip().lower()},
        ).mappings().all()
    }
    if not grants:
        return []
    out = []
    for server in list_mcp_servers(connection, project_id, enabled_only=enabled_only):
        if server["id"] in grants:
            out.append({**server, "granted_tools": grants[server["id"]]})
    return out


def holds_mcp_tool(connection: Connection, project_id: str, role_key: str, server_id: str,
                   tool: str) -> bool:
    """Does this agent hold this server for this tool? (An empty tool list means all of them.)"""
    from .tool_access_requests import enforce_pins

    enforce_pins(connection, project_id, str(role_key or "").strip().lower() or None)
    row = connection.execute(
        text("SELECT tools FROM role_mcp_servers WHERE project_id = :p AND role_key = :r "
             "AND mcp_server_id = :s"),
        {"p": project_id, "r": str(role_key or "").strip().lower(), "s": server_id},
    ).first()
    if row is None:
        return False
    allowed = [str(t) for t in (row[0] or [])]
    return not allowed or tool in allowed


#: The ONLY roads on which an MCP tool runs without its caller holding the server, by name, and
#: the one caller each applies to. A road names itself on the invocation
#: (`SkillExecutorContext.mcp_reach`); the runtime gate and the executor backstop both ask
#: `serverless_mcp_allowed`, so a caller's name alone never opens it.
#:
#: legacy_workflow_runner  a custom step in a version published before step agents had to hold
#:                          what they run (step_agents' documented exception). Review of
#:                          milestone/integration: `workflow_runner` used to be on this list
#:                          outright, so ANY custom MCP-tool step nobody held ran as the workflow
#:                          in current versions too — chat could author one, publish it and a
#:                          schedule run it, with no person ever granting the server.
#: agent_loop_offer         the agent loop calling a tool it OFFERED: a bounded run is offered
#:                          only the servers its agent holds (agent_loop._granted_mcp_servers),
#:                          an unbounded one is the operator's own run over reviewed servers.
SERVERLESS_MCP_GRANTS: dict[str, str] = {
    "legacy_workflow_runner": "workflow_runner",
    "agent_loop_offer": "agent_loop",
}


def serverless_mcp_allowed(caller: Optional[str], reach: Optional[str]) -> bool:
    """May this invocation reach an MCP server its caller does not hold? Only on a named road."""
    return bool(reach) and SERVERLESS_MCP_GRANTS.get(str(reach)) == str(caller or "")


def mcp_grant_gap(connection: Connection, project_id: str, role_key: str, *,
                  server_id: str, tool: str, skill_key: str) -> Optional[dict[str, Any]]:
    """What `role_key` is missing to call `tool` on `server_id` through `skill_key`, or None.

    None also when the server is gone: the executor and the catalogue say that in their own
    words ("the MCP server it calls has been removed"), and saying "you do not hold it" about a
    server that does not exist would send the operator to grant something they cannot.
    """
    server = connection.execute(
        text("SELECT id, name FROM mcp_servers WHERE project_id = :p AND id = :s"),
        {"p": project_id, "s": server_id},
    ).mappings().first()
    if server is None:
        return None
    role = str(role_key or "").strip().lower()
    held = connection.execute(
        text("SELECT tools FROM role_mcp_servers WHERE project_id = :p AND role_key = :r "
             "AND mcp_server_id = :s"),
        {"p": project_id, "r": role, "s": server_id},
    ).first()
    gap = {"skill": skill_key, "role_key": role, "mcp_server_id": server_id,
           "server": str(server["name"]), "tool": tool}
    who = role or "this caller"
    if held is None and role == "workflow_runner":
        # A STEP NOBODY HOLDS: say what to do about the step, not about an agent called
        # "workflow_runner", which the operator cannot open.
        return {**gap, "message": (
            f"this step runs as the workflow, and the workflow holds no MCP server: {skill_key} "
            f"calls '{tool}' on '{server['name']}'. Give the server to an agent in Agent Studio "
            "(Loadout › MCP › + Add) and put that agent on the step, or leave the "
            "step's agent empty so BOSS picks the one that holds it.")}
    if held is None:
        return {**gap, "message": (
            f"{who} may not run {skill_key}: it calls the MCP server '{server['name']}', and "
            f"{who} does not hold that server. Holding a skill does not carry a server's reach "
            "— give the agent the server in Agent Studio (Loadout › MCP › + Add), "
            "or ask BOSS to offer assign_mcp_to_agent.")}
    allowed = [str(t) for t in (held[0] or [])]
    if allowed and tool not in allowed:
        return {**gap, "message": (
            f"{who} may not run {skill_key}: it calls '{tool}' on the MCP server "
            f"'{server['name']}', and {who} holds that server only for {', '.join(allowed)}. "
            f"Add '{tool}' to its grant in Agent Studio.")}
    return None


def mcp_reach_gap(connection: Connection, project_id: str, role_key: str,
                  skill_key: str) -> Optional[dict[str, Any]]:
    """What an agent is missing to run a skill that calls an MCP tool, or None.

    A GRANT WHOSE SKILL REACHES AN MCP SERVER IS AN MCP GRANT. Review of milestone/integration:
    `assign_mcp_to_agent` is offered to the operator, never run by the chat model, because a
    server carries its credentials and every action it exposes. But `draft_skill` (an
    `mcp_tool` skill) and `assign_skill_to_agent` were both `safe`, and nothing at execution
    asked whether the agent held the server — so the model could give any agent any registered
    server's tool on its own, one skill at a time. The rule now lives here and every road asks
    it: the skill runtime's permission gate and the mcp_tool executor (through mcp_grant_gap,
    with the server they already hold), the step auto-pick, the chat grant and the API grant's
    answer. An agent reaches exactly the servers (and tools) it holds in role_mcp_servers.

    None when the skill does not call an MCP tool, or the agent holds that server for that tool.
    Otherwise a dict naming the server and tool, and a `message` saying what to do.
    """
    from .skill_catalogue import entry

    item = entry(connection, project_id, skill_key)
    policy = ((item or {}).get("spec") or {}).get("policy") or {}
    if not policy.get("mcp_server_id"):
        return None
    return mcp_grant_gap(connection, project_id, role_key,
                         server_id=str(policy["mcp_server_id"]),
                         tool=str(policy.get("mcp_tool") or ""), skill_key=skill_key)


def list_role_mcp(connection: Connection, project_id: str,
                  role_key: Optional[str] = None) -> list[dict[str, Any]]:
    sql = ("SELECT g.role_key, g.mcp_server_id, g.tools, g.assigned_by, g.created_at, "
           "s.name, s.enabled FROM role_mcp_servers g JOIN mcp_servers s "
           "ON s.id = g.mcp_server_id WHERE g.project_id = :p")
    params: dict[str, Any] = {"p": project_id}
    if role_key:
        sql += " AND g.role_key = :r"
        params["r"] = str(role_key).strip().lower()
    rows = connection.execute(text(sql + " ORDER BY g.role_key, s.name"), params).mappings()
    return [
        {"role_key": r["role_key"], "mcp_server_id": r["mcp_server_id"], "server": r["name"],
         "tools": list(r["tools"] or []), "enabled": bool(r["enabled"]),
         "assigned_by": r["assigned_by"],
         "assigned_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in rows
    ]


def role_mcp_detail(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    """What MCP servers one agent holds, and which others it could be given. Disjoint lists."""
    from .mcp_registry import list_mcp_servers

    holds = list_role_mcp(connection, project_id, role_key)
    held = {h["mcp_server_id"] for h in holds}
    servers = list_mcp_servers(connection, project_id)
    by_id = {s["id"]: s for s in servers}
    for h in holds:
        server = by_id.get(h["mcp_server_id"]) or {}
        h["url"] = server.get("url")
        h["risk_tier"] = server.get("risk_tier")
        h["description"] = server.get("description")
    return {
        "role_key": str(role_key).strip().lower(),
        "holds": holds,
        "available": [
            {"mcp_server_id": s["id"], "server": s["name"], "url": s["url"],
             "enabled": s["enabled"], "risk_tier": s["risk_tier"],
             "description": s.get("description")}
            for s in servers if s["id"] not in held
        ],
    }


# --------------------------------------------------------------------------- #
# Skill grants, through the one function that writes them
# --------------------------------------------------------------------------- #
def _validate_skills(connection: Connection, project_id: str, skills: Iterable[str],
                     forbidden: Iterable[str] = ()) -> list[str]:
    from .skill_definitions import _skill_exists

    wanted = sorted({str(s).strip() for s in skills or [] if str(s).strip()})
    blocked = sorted(set(wanted) & {str(f) for f in forbidden or []})
    if blocked:
        raise AgentError(
            f"this agent's spec forbids {', '.join(blocked)}. Edit its boundaries first, or give "
            "the work to another agent.")
    missing = [s for s in wanted if _skill_exists(connection, project_id, s) is None]
    if missing:
        raise AgentError(
            f"no skill {', '.join(repr(m) for m in missing)} in this project — author it on the "
            "Skills and MCP page, or pick one that exists.")
    return wanted


def _grant_skills(connection: Connection, project_id: str, role_key: str, skills: list[str],
                  actor: str) -> None:
    from .skill_definitions import SkillDefinitionError, assign_skill_to_role

    for skill in skills:
        try:
            assign_skill_to_role(connection, project_id=project_id, actor=actor,
                                 role_key=role_key, skill_id=skill)
        except SkillDefinitionError as error:
            raise AgentError(str(error)) from error


def _held_skill_ids(connection: Connection, project_id: str, role_key: str) -> set[str]:
    return set(connection.execute(
        text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).scalars().all())


# --------------------------------------------------------------------------- #
# Where an agent is named
# --------------------------------------------------------------------------- #
#: EVERY FIELD OF A WORKFLOW STEP THAT NAMES AN AGENT, and what naming it there means.
#:
#: This is the one place that knows. The delete guard first read only `captain`, `binding.agent`
#: and `binding.agents`, so an agent named as a gate's approver (`approval.approver_role` — the
#: catalogue gates name release_manager, product_owner, design_lead and tech_lead there) or as a
#: binding's `role_key` (which start_run refuses when the role is missing) was deleted with
#: 200, tombstoned so no seed would bring it back, and the gate stayed broken for good.
#: tests/test_every_step_field_naming_an_agent_guards_its_delete.py scans the engine for every
#: step field read as a role key and fails until each is listed here or exempted with a reason.
#:
#: (path into the step, how the step uses the agent, whether the field holds a list of keys)
STEP_AGENT_FIELDS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (("captain",), "runs it", False),
    (("binding", "agent"), "runs it", False),
    (("binding", "role_key"), "runs it", False),
    (("binding", "agents"), "is on its shortlist", True),
    (("approval", "approver_role"), "approves it", False),
)


def _field(step: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = step
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _names_in_step(step: dict[str, Any]) -> dict[str, str]:
    """{role_key: how} for every agent one workflow step names, read from STEP_AGENT_FIELDS."""
    found: dict[str, str] = {}
    for path, how, many in STEP_AGENT_FIELDS:
        value = _field(step, path)
        listed = (value if isinstance(value, list) else []) if many else [value]
        for item in listed:
            key = str(item or "").strip().lower()
            if key:
                found.setdefault(key, how)
    return found


def steps_naming(connection: Connection, project_id: str, role_key: str) -> list[dict[str, Any]]:
    """Every workflow step that names this agent, in any version a run can still start from.

    Read from workflows.versions_a_run_can_reach — every published version of a workflow that
    is not retired, labelled with how a run reaches it — plus the version of any run still in
    flight, which will reach its steps whatever the workflow says now.

    Round 1 of the milestone/agents review read only each workflow's NEWEST version, so an
    agent named only in the version that actually runs (an older one made active again, or the
    active one under a newer unactivated publish) was deleted with 200 and the workflow broken
    for good. Each entry says which version names it (`because`: the strongest REACH_SAID
    label; `reasons`: every one that applies), whether retiring that version would free the
    agent (`retirable`), and — in the operator's words, the same ones the API's refusal and the
    console show — where it is (`where`) and what to do about it (`remedy`).

    A RUN THAT CAN STILL BE TRIED AGAIN REACHES ITS VERSION. Only runs in flight counted, so a
    run stopped at a failed step (status failed or deadlettered, still offering Try again) did
    not hold its version: the agent was deleted (its version retired if older), and Try again
    sent the run on to a later step naming it, which failed with "not a role this workforce
    has" (review of milestone/agents, round 3). Every run whose status is not CLOSED — the
    statuses in which nothing runs again — now counts.
    """
    from . import workflows
    from .step_agents import is_default_captain, named_agent
    from .workflow_run_actions import CLOSED_RUN_STATES

    role = str(role_key or "").strip().lower()
    versions = workflows.versions_a_run_can_reach(connection, project_id)
    reachable = [
        {"workflow_key": v["workflow_key"], "name": v["name"], "version": v["version"],
         "version_id": v["version_id"], "spec": v["spec"], "why": v["reach"],
         "pointed_by": v.get("pointed_by") or []}
        for v in versions
    ]
    runs = connection.execute(
        text(
            """
            SELECT DISTINCT d.workflow_key, d.name, v.version, v.id AS version_id, v.spec,
                   r.status
              FROM workflow_runs r
              JOIN workflow_versions v ON v.id = r.workflow_version_id
              JOIN workflow_definitions d ON d.id = v.workflow_definition_id
             WHERE r.project_id = :p
               AND NOT (r.status = ANY(:closed))
            """
        ),
        {"p": project_id, "closed": sorted(CLOSED_RUN_STATES)},
    ).mappings().all()
    live = [{**dict(r), "why": run_reach(r["status"]), "pointed_by": []} for r in runs]
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in live + reachable:
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        for step in spec.get("steps") or []:
            if not isinstance(step, dict):
                continue
            names = _names_in_step(step)
            how = names.get(role)
            if not how:
                continue
            # ONLY THE CATALOGUE'S DEFAULT, and nothing else on the step names it: deleting or
            # pausing the agent hands the step to Auto (step_agents.resolve), so it never blocks.
            soft = (is_default_captain(step) and named_agent(step) == role
                    and not _names_in_step({k: v for k, v in step.items()
                                            if k != "captain"}).get(role))
            key = (str(row["workflow_key"]), int(row["version"]), str(step.get("id")))
            entry = seen.get(key)
            if entry is None:
                entry = {
                    "workflow_key": row["workflow_key"], "workflow": row["name"],
                    "version": int(row["version"]), "version_id": str(row["version_id"]),
                    "step_id": str(step.get("id") or ""),
                    "step": str(step.get("label") or step.get("name") or step.get("id") or ""),
                    "how": how, "because": row["why"], "reasons": [], "pointed_by": [],
                    "soft": soft,
                }
                seen[key] = entry
                out.append(entry)
            # EVERY REASON, not the first: a version with a run in flight that a mapping also
            # starts must say both, or the operator lets the run finish and is refused again.
            if row["why"] not in entry["reasons"]:
                entry["reasons"].append(row["why"])
            entry["pointed_by"].extend(row["pointed_by"])
    for entry in out:
        # A version with a run that can still go on is never retirable, however else reached.
        entry["retirable"] = all(r in workflows.RETIRABLE_REACH for r in entry["reasons"])
        # What a delete retiring this version retires with it — older parent versions that
        # bind it as a subworkflow — so the page can say so before it is pressed.
        entry["also_retires"] = [
            {"workflow": p["name"], "version": p["version"]}
            for p in workflows.retired_with(versions, entry["version_id"])
        ] if entry["retirable"] else []
        entry["where"] = _where(entry)
        entry["remedy"] = _remedy(entry)
    return out


def _reachable_specs(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every workflow version a run can reach (versions_a_run_can_reach) plus the version of
    every run that can still go on: the one set steps_naming reads, as {workflow_key, name,
    spec}. Read once for the whole project by the batched readers below."""
    from . import workflows
    from .workflow_run_actions import CLOSED_RUN_STATES

    out = [{"workflow_key": v["workflow_key"], "name": v["name"],
            "spec": v["spec"] if isinstance(v["spec"], dict) else {}}
           for v in workflows.versions_a_run_can_reach(connection, project_id)]
    for r in connection.execute(
        text(
            """
            SELECT DISTINCT d.workflow_key, d.name, v.id, v.spec
              FROM workflow_runs r
              JOIN workflow_versions v ON v.id = r.workflow_version_id
              JOIN workflow_definitions d ON d.id = v.workflow_definition_id
             WHERE r.project_id = :p AND NOT (r.status = ANY(:closed))
            """
        ),
        {"p": project_id, "closed": sorted(CLOSED_RUN_STATES)},
    ).mappings().all():
        out.append({"workflow_key": r["workflow_key"], "name": r["name"],
                    "spec": r["spec"] if isinstance(r["spec"], dict) else {}})
    return out


def _step_title(step: dict[str, Any]) -> str:
    return str(step.get("title") or step.get("label") or step.get("name") or step.get("id") or "")


def steps_naming_all(connection: Connection, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """steps_naming for EVERY agent in one pass (org-view spec §6.2): {role_key: [{workflow_key,
    workflow_name, step_id, step_title, how}]}, one entry per workflow step whatever how many
    reachable versions name it. The same versions and the same STEP_AGENT_FIELDS as
    steps_naming, so an agent's `Named on` and its delete guard read one set."""
    out: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in _reachable_specs(connection, project_id):
        for step in row["spec"].get("steps") or []:
            if not isinstance(step, dict):
                continue
            for role, how in _names_in_step(step).items():
                key = (role, str(row["workflow_key"]), str(step.get("id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                out.setdefault(role, []).append({
                    "workflow_key": str(row["workflow_key"]), "workflow_name": str(row["name"]),
                    "step_id": str(step.get("id") or ""), "step_title": _step_title(step),
                    "how": how})
    return out


def missing_roles(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Roles a workflow step names that this project does not have (org-view spec §6.2): the
    empty seats. For every step of every version a run can reach, validate_captain against the
    project's roster; a named key it refuses is missing. `Auto` steps name nobody and the
    catalogue's soft default falls back to Auto (step_agents.resolve), so neither is ever
    missing. Each entry suggests a team: the pack's role YAML when it knows the key, else
    `yours`. Sorted by how many steps name it, then key."""
    from . import workflows
    from .step_agents import HUMAN, is_default_captain, named_agent
    from .workflow_capabilities import validate_captain

    roster = workflows.project_roster(connection, project_id)
    found: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in _reachable_specs(connection, project_id):
        for step in row["spec"].get("steps") or []:
            if not isinstance(step, dict):
                continue
            named = named_agent(step)
            if not named or named == HUMAN or named in roster or is_default_captain(step):
                continue
            if not validate_captain(step, roster):
                continue
            key = (named, str(row["workflow_key"]), str(step.get("id") or ""))
            if key in seen:
                continue
            seen.add(key)
            entry = found.setdefault(named, {"role_key": named, "team": _pack_team(named),
                                             "steps": []})
            entry["steps"].append({"workflow_key": str(row["workflow_key"]),
                                   "workflow_name": str(row["name"]),
                                   "step_id": str(step.get("id") or ""),
                                   "step_title": _step_title(step)})
    return sorted(found.values(), key=lambda e: (-len(e["steps"]), e["role_key"]))


def _pack_team(role_key: str) -> str:
    """The team the shipped pack's role YAML puts this key in, else `yours`."""
    try:
        import yaml

        from ..config import get_settings

        path = get_settings().specpack_root / "roles" / f"{role_key}.yaml"
        if not path.exists():
            return "yours"
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return agent_identity.team_of({"team": body.get("team")})["key"]
    except Exception:  # noqa: BLE001 — a suggestion, never a failure
        return "yours"


#: How a RUN reaches its version, beside workflows.VERSION_REACH (how a START reaches one).
RUN_REACH = {
    "live run": "a run of it is in flight",
    "stopped run": "a run of it stopped at a failed step, and Try again would carry it on",
}


def run_reach(status: Any) -> Optional[str]:
    """The RUN_REACH label for a run in this status, or None when nothing in it runs again."""
    from .workflow_run_actions import CLOSED_RUN_STATES, STOPPED_RUN_STATES

    status = str(status or "")
    if status in CLOSED_RUN_STATES:
        return None
    return "stopped run" if status in STOPPED_RUN_STATES else "live run"


def _work_class(pointer: dict[str, Any]) -> str:
    """The kind of work a mapping routes, said out loud."""
    from .workflow_catalogue import work_class_label

    kind, value = pointer.get("selector_kind"), str(pointer.get("selector_value") or "")
    if kind == "work_class":
        return work_class_label(value)
    return f"work with {kind} '{value}'"


def _pointers(named: dict[str, Any], via: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in named.get("pointed_by") or []:
        if p.get("via") == via and p not in out:
            out.append(p)
    return out


def _mapped_where(n: dict[str, Any]) -> str:
    routed = " and ".join(_work_class(p) for p in _pointers(n, "mapped")) or "work"
    return f"v{n['version']}, where a mapping still sends {routed}"


def _mapped_remedy(n: dict[str, Any]) -> str:
    # workflows.repoint_mapping moves exactly this mapping, whatever its scope and priority —
    # the catalogue's Apply only replaces its own slot, so it is not named here.
    moves = [f"point {_work_class(p)} at the current version of '{n['workflow']}' (Point at "
             f"the current version on this agent's page, or POST /v1/workflow-mappings/"
             f"{p['mapping_id']}/repoint)"
             for p in _pointers(n, "mapped")]
    return "; ".join(moves) or (f"point the mapping that starts v{n['version']} of "
                                f"'{n['workflow']}' at its current version")


def _subworkflow_where(n: dict[str, Any]) -> str:
    parents = ", ".join(f"step '{p['step']}' of '{p['workflow']}' v{p['version']}"
                        for p in _pointers(n, "subworkflow")) or "another workflow's step"
    return f"v{n['version']}, which {parents} starts as a subworkflow"


def _subworkflow_remedy(n: dict[str, Any]) -> str:
    moves = [f"re-bind step '{p['step']}' of '{p['workflow']}' to the current version of "
             f"'{n['workflow']}' and save '{p['workflow']}'"
             for p in _pointers(n, "subworkflow")]
    return "; ".join(moves) or (f"re-bind the subworkflow step that starts v{n['version']} of "
                                f"'{n['workflow']}' to its current version")


def _edit_the_step(n: dict[str, Any]) -> str:
    return (f"give step '{n['step']}' of '{n['workflow']}' to another agent (or leave it for "
            "BOSS to pick) and save the workflow")


#: WHERE A STEP NAMING THE AGENT IS, AND WHAT TO DO ABOUT IT — one entry per way a run reaches a
#: version (every workflows.VERSION_REACH label and every RUN_REACH label), read by the API's
#: refusal and sent to the console as each step's `where` and `remedy`, so the two can never
#: say different things.
#:
#: THE INCIDENT (review of milestone/agents, round 3). Every reach but 'older' got one sentence:
#: "give those steps to another agent and save the workflow, then delete again". Saving never
#: moves a mapping (it pins one exact version) or a parent's subworkflow binding, so an operator
#: who did exactly that was refused again, for good, and the refusal named neither the kind of
#: work nor the parent workflow. tests/test_every_version_a_run_can_start_guards_its_agents.py
#: walks every label and fails until it has its own entry here.
REACH_SAID: dict[str, tuple[Any, Any]] = {
    "active": (lambda n: f"v{n['version']}, the version it runs", _edit_the_step),
    "not yet active": (
        lambda n: f"v{n['version']}, published but not yet the version it runs",
        _edit_the_step),
    "mapped": (_mapped_where, _mapped_remedy),
    "subworkflow": (_subworkflow_where, _subworkflow_remedy),
    "older": (
        lambda n: f"v{n['version']}, an older version still startable by id",
        lambda n: ("delete again choosing to retire the older versions that name it (their "
                   "history is kept), or delete the workflow")),
    "live run": (
        lambda n: f"a run in flight on v{n['version']}",
        lambda n: f"let the run of '{n['workflow']}' v{n['version']} finish, or stop it"),
    "stopped run": (
        lambda n: f"a run of v{n['version']} stopped at a failed step",
        lambda n: (f"stop the run of '{n['workflow']}' v{n['version']} that is waiting at a "
                   "failed step (its history is kept) — Try again would carry it on to this "
                   "step")),
}


def _where(named: dict[str, Any]) -> str:
    """Which version names it, said the way an operator thinks of it."""
    return REACH_SAID[named["because"]][0](named)


def _remedy(named: dict[str, Any]) -> str:
    """What to do so this step stops naming the agent: one clause per reason it is reached."""
    said: list[str] = []
    for reason in named.get("reasons") or [named["because"]]:
        clause = REACH_SAID[reason][1](named)
        if clause not in said:
            said.append(clause)
    return "; ".join(said)


def studio_profiles(connection: Connection, project_id: str, role_key: str) -> list[dict]:
    """The Agent Studio profiles (prompts, model policy, rules) that sit on this agent."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT d.id, d.agent_key, d.name, d.status, d.active_version_id
              FROM agent_definitions d JOIN agent_versions v ON v.agent_definition_id = d.id
             WHERE d.project_id = :p AND v.role_key = :r
             ORDER BY d.agent_key
            """
        ),
        {"p": project_id, "r": str(role_key).strip().lower()},
    ).mappings().all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def list_agents(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every agent with what it holds, in one read — the pickers need the counts."""
    rows = connection.execute(
        text(
            """
            SELECT r.role_key, r.display_name, r.spec, r.reports_to, r.autonomy_level,
                   r.agent_id, r.lifecycle, r.identity_version,
                   (SELECT count(*) FROM role_skills s
                     WHERE s.project_id = r.project_id AND s.role_key = r.role_key) AS skills,
                   (SELECT count(*) FROM role_mcp_servers m
                     WHERE m.project_id = r.project_id AND m.role_key = r.role_key) AS mcp
              FROM roles r WHERE r.project_id = :p ORDER BY r.role_key
            """
        ),
        {"p": project_id},
    ).mappings().all()
    return [
        {"role_key": r["role_key"], "name": r["display_name"],
         "mission": str(((r["spec"] or {}) if isinstance(r["spec"], dict) else {})
                        .get("mission") or ""),
         "reports_to": r["reports_to"], "autonomy_level": int(r["autonomy_level"] or 0),
         "skill_count": int(r["skills"] or 0), "mcp_server_count": int(r["mcp"] or 0),
         # WHO IT IS, for the pickers: a retired agent is left out of them, a paused one is
         # shown as paused (Auto skips it).
         "agent_id": r["agent_id"], "lifecycle": r["lifecycle"] or "active",
         "version": int(r["identity_version"] or 1),
         **_voice_summary(r["spec"])}
        for r in rows
    ]


def _voice_summary(spec: Any) -> dict[str, Any]:
    persona = (spec or {}).get("persona") if isinstance(spec, dict) else None
    persona = persona if isinstance(persona, dict) else {}
    return {"callsign": persona.get("callsign"), "tagline": persona.get("tagline"),
            "archetype": persona.get("archetype")}


def get_agent(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    from .skill_definitions import role_skill_detail

    role = _require_role(connection, project_id, str(role_key or "").strip().lower())
    spec = role["spec"] if isinstance(role["spec"], dict) else {}
    key = role["role_key"]
    return {
        "project_id": project_id,
        "role_key": key,
        "name": role["display_name"],
        "mission": str(spec.get("mission") or ""),
        "boundaries": _boundaries(spec.get("boundaries") or {}),
        "reports_to": role["reports_to"],
        # Which team it sits in (agent_identity.team_of, the one resolver): {key, label, tip?}.
        "team": agent_identity.team_of(spec),
        "autonomy_level": int(role["autonomy_level"] or 0),
        # Made in the console, or installed from a spec pack. Both can be edited; a bundled one
        # stays edited (see seed_exclusions) rather than reverting on the next upgrade.
        "bundled": spec.get("source") != "operator",
        "skills": role_skill_detail(connection, project_id, key),
        "mcp": role_mcp_detail(connection, project_id, key),
        "named_on_steps": steps_naming(connection, project_id, key),
        "studio_profiles": studio_profiles(connection, project_id, key),
        # Agent World: id, version, lifecycle, sponsor, persona, avatar (agent_identity).
        **agent_identity.summary(connection, project_id, key),
    }


# --------------------------------------------------------------------------- #
# Create, update, delete
# --------------------------------------------------------------------------- #
def create_agent(
    connection: Connection,
    project_id: str,
    *,
    actor: str,
    name: str,
    purpose: str = "",
    role_key: Optional[str] = None,
    skills: Iterable[str] = (),
    mcp_servers: Iterable[Any] = (),
    reports_to: Optional[str] = None,
    boundaries: Optional[dict[str, Any]] = None,
    persona: Optional[dict[str, Any]] = None,
    born_from: Optional[dict[str, Any]] = None,
    team: Optional[str] = None,
) -> dict[str, Any]:
    """Make an agent and give it what it holds, in one transaction.

    BORN, NOT JUST INSERTED. The agent gets its agt_ id, `born_by`/`sponsor` = the actor, its
    `born_from` (blank, a template role or an archetype) and one `agent.born` + `agent.sealed`
    v1 — after its grants, so v1 is the agent as created. From a template or an archetype, what
    the operator left empty (purpose, boundaries, skills, MCP servers, voice) comes from that
    role's YAML; what they wrote wins.

    REFUSES AN EXISTING KEY. The wizard derived a role key from the name and register_role took
    its UPDATE path, so a new agent called "QA" silently replaced the bundled qa role's mission,
    skills and boundaries and reset its autonomy. Creating is never editing.
    """
    display = str(name or "").strip()
    if not display:
        raise AgentError("an agent needs a name.")
    given = str(role_key or "").strip().lower()
    key = given or key_from_name(display)
    if not given and not _KEY.match(key):
        # A NAME WITH NO ENGLISH LETTERS STILL MAKES AN AGENT. 'காணொளி தொகுப்பாளர்' slugged to ''
        # and quick create, which has no key field unless asked, answered "'' cannot be an agent
        # key" — a refusal naming nothing the operator typed. The name stays exactly as written;
        # only the key, which nobody chose, is made up.
        key = f"agent_{make_id('k').split('_')[-1][:8].lower()}"
    if not _KEY.match(key):
        raise AgentError(
            f"{key!r} cannot be an agent key. Use English letters, digits and underscores, "
            "starting with a letter, 2-64 characters (for example release_auditor) — or leave "
            "the key empty and BOSS will make one.")
    if _role(connection, project_id, key) is not None:
        raise AgentError(
            f"an agent {key!r} already exists on this project. Open it to change it, or give "
            "the new one a different name.", status=409)

    # REGISTERING A SHIPPED AGENT REGISTERS THE SHIPPED AGENT. The roster's Register on a key
    # BOSS ships a definition for (software_engineer, video_editor) made a blank operator row:
    # no persona, no remit, no grants, and marked operator-edited so the seed never filled it.
    # born_from {kind: shipped} (the Register dialog, untouched) — or no born_from and nothing
    # authored — installs the shipped role instead. A key with no shipped YAML is a blank agent.
    skills, mcp_servers = list(skills or []), list(mcp_servers or [])
    asked = str(born_from.get("kind") or "").strip().lower() if isinstance(born_from, dict) else ""
    authored = bool(str(purpose or "").strip() or skills or mcp_servers or boundaries
                    or reports_to or {k: v for k, v in (persona or {}).items()
                                      if k != "avatar" and v})
    if asked == "shipped" or (born_from is None and not authored):
        shipped = _shipped_role(key)
        if shipped is not None:
            return _install_shipped(connection, project_id, key, shipped, actor)
        if asked == "shipped":
            born_from = {"kind": "blank"}

    origin, blueprint = _blueprint(born_from)
    if blueprint:
        purpose = purpose or str(blueprint.get("mission") or "")
        if boundaries is None and isinstance(blueprint.get("boundaries"), dict):
            boundaries = blueprint["boundaries"]
        permissions = blueprint.get("permissions") or {}
        if not list(skills or []):
            from .skill_definitions import _skill_exists

            forbidden = set(permissions.get("forbidden_skills") or [])
            skills = [s for s in permissions.get("allowed_skills") or []
                      if "*" not in str(s) and s not in forbidden
                      and _skill_exists(connection, project_id, str(s)) is not None]
        if not list(mcp_servers or []):
            mcp_servers = _blueprint_servers(connection, project_id,
                                             permissions.get("mcp_servers") or [])
        base = dict(blueprint.get("persona") or {}) if isinstance(blueprint.get("persona"),
                                                                    dict) else {}
        # A NEW AGENT GETS ITS OWN FACE. The blueprint's avatar seed is the shipped agent's
        # (Vera's, Nova's); a second agent wearing it would be two agents with one face. The
        # face is seeded by the new agt_ id instead; `archetype` stays, so the kit can theme it.
        base.pop("avatar", None)
        persona = {**base, **(persona or {})} if (base or persona) else None
    voice, warnings = _clean_persona(connection, project_id, persona)

    skill_ids = _validate_skills(connection, project_id, skills)
    servers = [{**_resolve_server(connection, project_id, e["server"]), "tools": e["tools"]}
               for e in _mcp_entries(mcp_servers)]
    parent = _check_reports_to(connection, project_id, key, reports_to)

    spec = {
        "display_name": display,
        "mission": str(purpose or "").strip()[:2000],
        "boundaries": _boundaries(boundaries),
        # The grant is role_skills, not this list. Left empty so a later seed's
        # sync_role_skills has nothing to re-grant behind the operator's back.
        "permissions": {"allowed_skills": [], "forbidden_skills": []},
        "source": "operator",
    }
    if voice:
        spec["persona"] = voice
    stored_team = _team_value(team)
    if stored_team:
        spec["team"] = stored_team
    connection.execute(
        text(
            "INSERT INTO roles (id, project_id, role_key, display_name, autonomy_level, "
            "reports_to, spec) VALUES (:id, :p, :k, :name, 0, :reports_to, CAST(:spec AS JSONB))"
        ),
        {"id": make_id("role"), "p": project_id, "k": key, "name": display,
         "reports_to": parent, "spec": json.dumps(spec)},
    )
    # A KEY AN OPERATOR DELETED AND IS NOW MAKING AGAIN. The new decision wins — including over
    # the pack: deleting the bundled qa and making a qa by hand used to lift only 'role', so the
    # next seed found a bundled key whose spec differed from the YAML and rewrote it with the
    # pack's mission and grants. The spec written here is the operator's, so the seed leaves it.
    seed_exclusions.lift(connection, project_id, "role", key)
    seed_exclusions.exclude(connection, project_id, "role_spec", key, actor=actor,
                            reason="created in the console")
    if parent:
        _plan_line(connection, project_id, key, parent, actor)
    with agent_identity.sealing_once():
        agent_identity.birth(connection, project_id, key, born_by=actor, born_from=origin,
                             sponsor=actor if agent_identity.is_person(actor) else None,
                             actor=actor)
        # EVERY AGENT IS BORN KNOWING BOSS (agent_baseline.BASELINE_SKILLS): the baseline is
        # granted with whatever the operator chose, not left for the next boot's sync.
        from .agent_baseline import BASELINE_SKILLS

        _grant_skills(connection, project_id, key,
                      list(skill_ids) + [s for s in BASELINE_SKILLS if s not in skill_ids
                                         and _baseline_available(connection, project_id, s)],
                      actor)
        for server in servers:
            grant_mcp(connection, project_id, actor=actor, role_key=key,
                      mcp_server_id=server["id"], tools=server["tools"])
    emit_event(connection, project_id, "agent.created", actor, "role", key,
               {"skills": skill_ids, "mcp_servers": [s["id"] for s in servers],
                "reports_to": parent, "born_from": origin})
    return {**get_agent(connection, project_id, key), "warnings": warnings}


def _baseline_available(connection: Connection, project_id: str, skill_id: str) -> bool:
    """Whether this project has the baseline skill at all (a pack without the kit bundle does
    not): a missing one is left for sync_role_skills to report, not raised at creation."""
    from .skill_definitions import _skill_exists

    try:
        return _skill_exists(connection, project_id, skill_id) is not None
    except Exception:  # noqa: BLE001 — creation must not fail on a catalogue read
        return False


def _team_value(team: Any) -> Optional[str]:
    """What `spec.team` stores: a shipped team's key, else the operator's own words (<=40);
    None for no team (Yours). Read back only through agent_identity.team_of."""
    raw = str(team or "").strip()[:40]
    if not raw:
        return None
    resolved = agent_identity.team_of({"team": raw})
    if resolved["key"] == "yours":
        return None
    return resolved["key"] if resolved["key"] in agent_identity.TEAM_KEYS else raw


def _plan_line(connection: Connection, project_id: str, key: str, parent: Optional[str],
               actor: str) -> None:
    """A reporting line set on the agent is the plan's line too (org_assignments, the one home
    Organization › Lines edits and the Passport's declared lineage reads). A line cleared here
    clears it there only when the plan had one; an agent never in the plan stays out of it."""
    from . import org_assignments

    if parent is None:
        if key not in org_assignments.get_all(connection, project_id):
            return
    org_assignments.set_one(connection, project_id, key, reports_to=parent, actor=actor)


def _shipped_role(key: str) -> Optional[dict[str, Any]]:
    """The role YAML BOSS ships for `key`, loaded and validated; None when it ships none."""
    from ..config import get_settings
    from .specpack import RoleNotFoundError, SpecPackService

    try:
        return SpecPackService(get_settings()).load_role_spec(key)
    except (RoleNotFoundError, ValueError):
        return None


def _install_shipped(connection: Connection, project_id: str, key: str,
                     shipped: dict[str, Any], actor: str) -> dict[str, Any]:
    """Install the shipped role as the seed would: its persona, remit and grants, born from its
    pack. The operator asked for it, so an earlier delete of the key no longer holds."""
    from ..config import get_settings
    from .shipped_cast import _grant, home_pack
    from .specpack import SpecPackService

    settings = get_settings()
    specpack = SpecPackService(settings)
    for kind in ("role", "role_spec"):
        seed_exclusions.lift(connection, project_id, kind, key)
    pack_id, version = home_pack(specpack, key, settings)
    origin = {"kind": "pack", "pack_id": pack_id, "module_version": version}
    with agent_identity.sealing_once():
        specpack._upsert_role_spec(connection, project_id, key, shipped, actor,
                                   born_from=origin)
        _grant(connection, project_id, actor)
    emit_event(connection, project_id, "agent.created", actor, "role", key,
               {"skills": [], "mcp_servers": [], "reports_to": None, "born_from": origin})
    return {**get_agent(connection, project_id, key), "warnings": []}


#: The kinds of birth POST /v1/agents accepts. `pack` is the seed's; `clone` is phase 2.
BIRTH_KINDS = ("blank", "template", "archetype", "shipped")


def _blueprint(born_from: Optional[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(born_from as stored, the role YAML it starts from — {} for a blank agent)."""
    if not born_from:
        return {"kind": "blank"}, {}
    if not isinstance(born_from, dict):
        raise AgentError("born_from must be {kind: blank|template|archetype, …}.")
    kind = str(born_from.get("kind") or "blank").strip().lower()
    if kind not in BIRTH_KINDS:
        raise AgentError(f"born_from.kind must be one of {', '.join(BIRTH_KINDS)}.")
    if kind == "blank":
        return {"kind": "blank"}, {}
    from .agent_persona import ARCHETYPES, _specpack_root

    field = "archetype" if kind == "archetype" else "template_key"
    name = str(born_from.get(field) or born_from.get("key") or "").strip().lower()
    if kind == "archetype" and name not in ARCHETYPES:
        raise AgentError(f"no archetype {name!r}. Pick one of: {', '.join(ARCHETYPES)}.")
    if not _KEY.match(name):
        raise AgentError(f"born_from.{field} must name a shipped role, like qa.")
    path = _specpack_root() / "roles" / f"{name}.yaml"
    if not path.is_file():
        raise AgentError(f"no template {name!r}. Templates are the shipped roles, like qa or "
                         "backend_engineer.")
    import yaml

    blueprint = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"kind": kind, field: name}, blueprint


def _blueprint_servers(connection: Connection, project_id: str,
                       wanted: Iterable[Any]) -> list[dict[str, Any]]:
    """The template's MCP servers this project actually has, as create_agent entries."""
    from .mcp_registry import list_mcp_servers

    servers = {str(s["name"]).strip().lower(): s["id"]
               for s in list_mcp_servers(connection, project_id)}
    out = []
    for entry in wanted or []:
        entry = {"server": entry} if isinstance(entry, str) else entry
        if not isinstance(entry, dict):
            continue
        server_id = servers.get(str(entry.get("server") or "").strip().lower())
        if server_id:
            out.append({"server_id": server_id, "tools": list(entry.get("tools") or [])})
    return out


def _clean_persona(connection: Connection, project_id: str,
                   persona: Optional[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """The persona as stored, and its warnings — or a 422 naming the field.

    The permission lint also knows this project's own skill ids, so "you hold deploy_app"
    and "github.merge_pr_real is yours" are refused whatever the skill is called here.
    """
    from .agent_persona import PersonaError, clean_persona

    if persona is None:
        return {}, []
    try:
        from .skill_catalogue import entries

        with connection.begin_nested():
            names = list(entries(connection, project_id))
    except Exception:  # noqa: BLE001 — the lint still knows every engine tool and skill id
        names = []
    try:
        return clean_persona(persona, extra_names=names)
    except PersonaError as error:
        raise PersonaInvalid(error.field, error.message) from error


class PersonaInvalid(AgentError):
    """A persona refused by the lint: 422 with {field, message}."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def update_agent(
    connection: Connection,
    project_id: str,
    role_key: str,
    *,
    actor: str,
    display_name: Any = _UNSET,
    mission: Any = _UNSET,
    boundaries: Any = _UNSET,
    reports_to: Any = _UNSET,
    skills: Any = _UNSET,
    mcp_servers: Any = _UNSET,
    grant_skills: Iterable[str] = (),
    grant_mcp_servers: Iterable[Any] = (),
    persona: Any = _UNSET,
    team: Any = _UNSET,
) -> dict[str, Any]:
    """Change what is given, and nothing else.

    ONE SEAL PER SAVE. Whatever changed — the voice, the remit, five grants — the agent's
    version goes up once (agent_identity.sealing_once), and not at all when nothing really
    changed. The answer carries `version`, `fingerprint` and the persona lint's `warnings`.

    A FIELD LEFT OUT IS LEFT ALONE. POST /v1/roles used as an update wrote autonomy_level=0 and
    reports_to=NULL whenever they were omitted, so fixing a typo in a mission demoted the agent
    and detached it from the org chart.

    `skills` / `mcp_servers` are the WHOLE holding (anything not listed is taken away);
    `grant_skills` / `grant_mcp_servers` only add — what the Studio wizard needs when it puts a
    profile on an agent that already holds things.
    """
    key = str(role_key or "").strip().lower()
    with agent_identity.sealing_once():
        role = _require_role(connection, project_id, key)
        spec = dict(role["spec"] if isinstance(role["spec"], dict) else {})
        changed: dict[str, Any] = {}
        warnings: list[str] = []

        if display_name is not _UNSET:
            name = str(display_name or "").strip()
            if not name:
                raise AgentError(
                    "an agent needs a name; leave the field out to keep the current one.")
            changed["display_name"] = name
            spec["display_name"] = name
        if mission is not _UNSET:
            spec["mission"] = str(mission or "").strip()[:2000]
            changed["mission"] = spec["mission"]
        if persona is not _UNSET:
            voice, warnings = _clean_persona(connection, project_id, persona or {})
            if voice != (spec.get("persona") or {}):
                if voice:
                    spec["persona"] = voice
                else:
                    spec.pop("persona", None)
                changed["persona"] = voice
        if boundaries is not _UNSET:
            spec["boundaries"] = _boundaries(boundaries)
            changed["boundaries"] = spec["boundaries"]
        if team is not _UNSET:
            # Organization's `Move to…`. Stored as written (a shipped key, or the operator's
            # own team name); agent_identity.team_of is the only reader. Empty = Yours.
            stored_team = _team_value(team)
            if stored_team != spec.get("team"):
                if stored_team:
                    spec["team"] = stored_team
                else:
                    spec.pop("team", None)
                changed["team"] = stored_team or "yours"
        parent = role["reports_to"]
        if reports_to is not _UNSET:
            parent = _check_reports_to(connection, project_id, key, reports_to)
            changed["reports_to"] = parent

        forbidden = ((spec.get("permissions") or {}).get("forbidden_skills")) or []
        desired_skills = (None if skills is _UNSET else set(
            _validate_skills(connection, project_id, skills or [], forbidden)))
        added_skills = set(_validate_skills(connection, project_id, grant_skills, forbidden))
        desired_mcp = (None if mcp_servers is _UNSET
                       else _mcp_entries(mcp_servers or []))
        added_mcp = _mcp_entries(grant_mcp_servers)

        if {"display_name", "mission", "boundaries", "persona", "team"} & changed.keys():
            connection.execute(
                text("UPDATE roles SET display_name = :name, spec = CAST(:spec AS JSONB) "
                     "WHERE project_id = :p AND role_key = :k"),
                {"p": project_id, "k": key,
                 "name": spec.get("display_name") or role["display_name"],
                 "spec": json.dumps(spec)},
            )
            # AN EDITED BUNDLED AGENT STAYS EDITED. The role installer rewrites a bundled role's
            # spec whenever it differs from the pack's YAML, which is every time somebody edits it.
            seed_exclusions.exclude(connection, project_id, "role_spec", key, actor=actor,
                                    reason="edited in the console")
        if "reports_to" in changed:
            connection.execute(
                text("UPDATE roles SET reports_to = :r WHERE project_id = :p AND role_key = :k"),
                {"p": project_id, "k": key, "r": parent},
            )
            _plan_line(connection, project_id, key, parent, actor)

        from .skill_definitions import unassign_skill_from_role

        held = _held_skill_ids(connection, project_id, key)
        if desired_skills is not None:
            # THE BASELINE IS NOT PART OF A REPLACE (agent_baseline.BASELINE_SKILLS). Replacing an
            # agent's skills sets its OWN skills; a list that simply did not name the baseline
            # would otherwise unassign it, and unassigning records an exclusion, so the agent
            # would lose BOSS's fundamentals for good. Taking one off is the explicit remove
            # (unassign_skill_from_role), recorded with who did it.
            from .agent_baseline import BASELINE_SKILLS

            for skill in sorted(held - desired_skills - added_skills - set(BASELINE_SKILLS)):
                unassign_skill_from_role(connection, project_id=project_id, actor=actor,
                                         role_key=key, skill_id=skill)
            to_add = (desired_skills | added_skills) - held
            changed["skills"] = sorted(desired_skills | added_skills)
        else:
            to_add = added_skills - held
            if to_add:
                changed["skills_added"] = sorted(to_add)
        _grant_skills(connection, project_id, key, sorted(to_add), actor)

        if desired_mcp is not None:
            wanted = {_resolve_server(connection, project_id, e["server"])["id"]: e["tools"]
                      for e in desired_mcp + added_mcp}
            for grant in list_role_mcp(connection, project_id, key):
                if grant["mcp_server_id"] not in wanted:
                    revoke_mcp(connection, project_id, actor=actor, role_key=key,
                               mcp_server_id=grant["mcp_server_id"])
            for server_id, tools in wanted.items():
                grant_mcp(connection, project_id, actor=actor, role_key=key,
                          mcp_server_id=server_id, tools=tools)
            changed["mcp_servers"] = sorted(wanted)
        elif added_mcp:
            for entry in added_mcp:
                granted = grant_mcp(connection, project_id, actor=actor, role_key=key,
                                    mcp_server_id=entry["server"], tools=entry["tools"])
                changed.setdefault("mcp_servers_added", []).append(granted["mcp_server_id"])

        if changed:
            emit_event(connection, project_id, "agent.updated", actor, "role", key,
                       {"changed": sorted(changed)})
            if "persona" in changed:
                agent_identity.seal_identity(connection, project_id, key, "persona", actor)
            if {"display_name", "mission", "boundaries", "reports_to"} & changed.keys():
                agent_identity.seal_identity(connection, project_id, key, "remit", actor)
    return {**get_agent(connection, project_id, key), "warnings": warnings}


def delete_agent(connection: Connection, project_id: str, role_key: str, *,
                 actor: str, retire_older_versions: bool = False) -> dict[str, Any]:
    """Take an agent off the roster, or say exactly why it cannot be.

    REFUSED WHILE A WORKFLOW STEP NAMES IT. Deleting the agent underneath a step turns a working
    workflow into one that fails at that step, minutes into a run, with "not a role this
    workforce has". The refusal names every step so the operator can move them first.

    `retire_older_versions` is the one way past a step in an OLDER version — published, still
    startable by id, but neither active, mapped, a subworkflow child nor running. Without it an
    agent once named in v1 of a workflow now at v5 could never be deleted. Those versions are
    retired (kept, no longer startable) in the same transaction as the delete, and only those.
    """
    key = str(role_key or "").strip().lower()
    from . import workflows

    # HELD BEFORE ANYTHING IS READ, until the delete commits: no publish, mapping, run start or
    # migration may change which versions a run can reach between the check below and the
    # delete (workflows.hold_workflow_reach — review of milestone/agents, round 3).
    workflows.hold_workflow_reach(connection, project_id, exclusive=True)
    role = _require_role(connection, project_id, key)

    named = steps_naming(connection, project_id, key)
    # A step that names it only as the catalogue's default never blocks: the delete re-staffs
    # those workflows without it, and any run still on an older version runs that step on Auto.
    blocking = [n for n in named
                if not n.get("soft") and not (retire_older_versions and n["retirable"])]
    if blocking:
        shown = "; ".join(
            f"workflow '{n['workflow']}' step '{n['step']}' ({n['where']}, {n['how']})"
            for n in blocking[:6])
        more = f" and {len(blocking) - 6} more" if len(blocking) > 6 else ""
        if all(n["retirable"] for n in blocking):
            todo = ("Only older versions name it, and nothing starts them by default — delete "
                    "again choosing to retire those versions (their history is kept), or "
                    "delete the workflow.")
        else:
            # Each step's own remedy (REACH_SAID), never one sentence for every reach: "save the
            # workflow" does not move a mapping or a parent's binding.
            remedies: list[str] = []
            for n in blocking:
                if n["remedy"] not in remedies:
                    remedies.append(n["remedy"])
            todo = "To delete it: " + "; ".join(remedies) + " — then delete again."
        raise AgentError(
            f"{role['display_name']} cannot be deleted: {len(blocking)} workflow step"
            f"{'s name' if len(blocking) != 1 else ' names'} it — {shown}{more}. {todo}",
            status=409)

    history = connection.execute(
        text("SELECT (SELECT count(*) FROM role_reviews WHERE role_id = :id) "
             "+ (SELECT count(*) FROM autonomy_changes WHERE role_id = :id)"),
        {"id": role["id"]},
    ).scalar() or 0
    if history:
        raise AgentError(
            f"{role['display_name']} has {history} trust review or autonomy change"
            f"{'s' if history != 1 else ''} on record, and that history is audit evidence BOSS "
            "keeps. Take its skills and MCP servers away instead — an agent holding nothing is "
            "never picked for a step and is refused if one names it.", status=409)

    planned = {n["version_id"] for n in named if n["retirable"] and not n.get("soft")}
    retired = workflows.retire_versions(
        connection, project_id, planned,
        actor=actor, reason=f"named agent '{key}', which was deleted") if named else []
    if not planned <= set(retired):
        # Cannot happen while the reach lock is held; if a writer ever skips it, refuse rather
        # than delete the agent under a version that is no longer older (and so not retired).
        raise AgentError(
            f"{role['display_name']} was not deleted: a workflow changed while it was being "
            "deleted, so a version that names it is no longer an older one. Open the agent "
            "again to see what names it now, then delete again.", status=409)

    from .workflow_catalogue import restaff_without

    restaffed = restaff_without(connection, project_id, key, actor=actor) if any(
        n.get("soft") for n in named) else []

    connection.execute(text("UPDATE kpis SET owner_role_id = NULL WHERE owner_role_id = :id"),
                       {"id": role["id"]})
    # Its reports move up one level rather than pointing at an agent that no longer exists.
    moved = connection.execute(
        text("UPDATE roles SET reports_to = :up WHERE project_id = :p AND reports_to = :k "
             "RETURNING role_key"),
        {"p": project_id, "k": key, "up": role["reports_to"]},
    ).scalars().all()
    skills = connection.execute(
        text("DELETE FROM role_skills WHERE project_id = :p AND role_key = :k RETURNING skill_id"),
        {"p": project_id, "k": key},
    ).scalars().all()
    mcp = connection.execute(
        text("DELETE FROM role_mcp_servers WHERE project_id = :p AND role_key = :k "
             "RETURNING mcp_server_id"),
        {"p": project_id, "k": key},
    ).scalars().all()
    connection.execute(
        text("DELETE FROM project_agents WHERE project_id = :p AND role_key = :k"),
        {"p": project_id, "k": key},
    )
    # ITS INSTRUCTIONS GO WITH IT, their bodies stay (an old run still shows what it acted on).
    # Keyed by role_key, so a new agent re-created under this key must not inherit them.
    from .agent_instructions import drop_for_agent

    drop_for_agent(connection, project_id, key)
    # ITS STUDIO PROFILES ARE RETIRED, NOT DELETED: their runs and tests are evidence.
    profiles = studio_profiles(connection, project_id, key)
    for profile in profiles:
        connection.execute(
            text("UPDATE agent_definitions SET status = 'retired', active_version_id = NULL, "
                 "updated_at = now() WHERE id = :id"), {"id": profile["id"]})
        connection.execute(
            text("UPDATE agent_versions SET status = 'retired', retired_at = now() "
                 "WHERE agent_definition_id = :id AND status <> 'retired'"),
            {"id": profile["id"]})
    connection.execute(text("DELETE FROM roles WHERE id = :id"), {"id": role["id"]})
    # A BUNDLED AGENT AN OPERATOR DELETED IS NOT REINSTALLED BY THE NEXT UPGRADE.
    seed_exclusions.exclude(connection, project_id, "role", key, actor=actor,
                            reason="deleted in the console")
    seed_exclusions.lift(connection, project_id, "role_spec", key)
    emit_event(connection, project_id, "agent.deleted", actor, "role", key,
               {"skills": list(skills), "mcp_servers": list(mcp),
                "studio_profiles_retired": [p["agent_key"] for p in profiles],
                "reports_moved_up": list(moved), "workflow_versions_retired": retired,
                "workflows_restaffed": restaffed})
    return {"role_key": key, "deleted": True, "skills_released": sorted(skills),
            "workflow_versions_retired": retired,
            "workflows_restaffed": restaffed,
            "mcp_servers_released": sorted(mcp),
            "studio_profiles_retired": [p["agent_key"] for p in profiles],
            "reports_moved_to": role["reports_to"], "reports_moved": sorted(moved)}
