"""ONE agent: a role, what it holds, and optionally a Studio profile.

WHY THIS MODULE EXISTS
----------------------
BOSS grew four places an "agent" lived and none of them joined:

* ``roles`` — the roster row the Agents page lists and workflow steps name as `captain`;
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
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import seed_exclusions
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
    emit_event(connection, project_id, "mcp.assigned_to_agent", actor, "role_mcp_server",
               str(row), {"role_key": role, "mcp_server_id": server["id"],
                          "server": server["name"], "tools": allow})
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
    emit_event(connection, project_id, "mcp.unassigned_from_agent", actor, "role_mcp_server",
               str(removed), {"role_key": role, "mcp_server_id": server["id"],
                              "server": server["name"]})
    return {"role_key": role, "mcp_server_id": server["id"], "server": server["name"],
            "assigned": False}


def held_mcp_servers(connection: Connection, project_id: str, role_key: str, *,
                     enabled_only: bool = True) -> list[dict[str, Any]]:
    """The MCP servers one agent HOLDS, as full server rows plus its tool allowlist.

    What a bounded agent run is offered. A disabled server is left out: disabling one is the
    project-wide off switch, and a grant does not get to override it.
    """
    from .mcp_registry import list_mcp_servers

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
def _names_in_step(step: dict[str, Any]) -> dict[str, str]:
    """{role_key: how} for every agent one workflow step names."""
    found: dict[str, str] = {}
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    captain = str(step.get("captain") or "").strip().lower()
    if captain:
        found[captain] = "runs it"
    agent = str(binding.get("agent") or "").strip().lower()
    if agent:
        found.setdefault(agent, "runs it")
    for listed in binding.get("agents") or []:
        key = str(listed or "").strip().lower()
        if key:
            found.setdefault(key, "is on its shortlist")
    return found


def steps_naming(connection: Connection, project_id: str, role_key: str) -> list[dict[str, Any]]:
    """Every workflow step that names this agent, in the version a run would start from.

    The LATEST published version of each workflow that is not retired, plus the version of any
    run still in flight — a run already under way will reach its steps whatever the newest
    version says.
    """
    role = str(role_key or "").strip().lower()
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (d.id) d.workflow_key, d.name, v.version, v.spec, 'latest' AS why
              FROM workflow_versions v JOIN workflow_definitions d
                ON d.id = v.workflow_definition_id
             WHERE v.project_id = :p AND v.status = 'published' AND d.status <> 'retired'
             ORDER BY d.id, v.version DESC
            """
        ),
        {"p": project_id},
    ).mappings().all()
    live = connection.execute(
        text(
            """
            SELECT DISTINCT d.workflow_key, d.name, v.version, v.spec, 'live run' AS why
              FROM workflow_runs r
              JOIN workflow_versions v ON v.id = r.workflow_version_id
              JOIN workflow_definitions d ON d.id = v.workflow_definition_id
             WHERE r.project_id = :p
               AND r.status IN ('queued', 'validating', 'running', 'waiting', 'paused',
                                'blocked', 'compensating')
            """
        ),
        {"p": project_id},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for row in list(rows) + list(live):
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        for step in spec.get("steps") or []:
            if not isinstance(step, dict):
                continue
            how = _names_in_step(step).get(role)
            if not how:
                continue
            key = (str(row["workflow_key"]), int(row["version"]), str(step.get("id")))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "workflow_key": row["workflow_key"], "workflow": row["name"],
                "version": int(row["version"]), "step_id": str(step.get("id") or ""),
                "step": str(step.get("label") or step.get("name") or step.get("id") or ""),
                "how": how, "because": row["why"],
            })
    return out


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
         "skill_count": int(r["skills"] or 0), "mcp_server_count": int(r["mcp"] or 0)}
        for r in rows
    ]


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
        "autonomy_level": int(role["autonomy_level"] or 0),
        # Made in the console, or installed from a spec pack. Both can be edited; a bundled one
        # stays edited (see seed_exclusions) rather than reverting on the next upgrade.
        "bundled": spec.get("source") != "operator",
        "skills": role_skill_detail(connection, project_id, key),
        "mcp": role_mcp_detail(connection, project_id, key),
        "named_on_steps": steps_naming(connection, project_id, key),
        "studio_profiles": studio_profiles(connection, project_id, key),
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
) -> dict[str, Any]:
    """Make an agent and give it what it holds, in one transaction.

    REFUSES AN EXISTING KEY. The wizard derived a role key from the name and register_role took
    its UPDATE path, so a new agent called "QA" silently replaced the bundled qa role's mission,
    skills and boundaries and reset its autonomy. Creating is never editing.
    """
    display = str(name or "").strip()
    if not display:
        raise AgentError("an agent needs a name.")
    key = str(role_key or "").strip().lower() or key_from_name(display)
    if not _KEY.match(key):
        raise AgentError(
            f"{key!r} cannot be an agent key: lower_snake_case, starting with a letter, 2-64 "
            "characters.")
    if _role(connection, project_id, key) is not None:
        raise AgentError(
            f"an agent {key!r} already exists on this project. Open it to change it, or give "
            "the new one a different name.", status=409)

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
    connection.execute(
        text(
            "INSERT INTO roles (id, project_id, role_key, display_name, autonomy_level, "
            "reports_to, spec) VALUES (:id, :p, :k, :name, 0, :reports_to, CAST(:spec AS JSONB))"
        ),
        {"id": make_id("role"), "p": project_id, "k": key, "name": display,
         "reports_to": parent, "spec": json.dumps(spec)},
    )
    # A KEY AN OPERATOR DELETED AND IS NOW MAKING AGAIN. The new decision wins.
    seed_exclusions.lift(connection, project_id, "role", key)
    _grant_skills(connection, project_id, key, skill_ids, actor)
    for server in servers:
        grant_mcp(connection, project_id, actor=actor, role_key=key,
                  mcp_server_id=server["id"], tools=server["tools"])
    emit_event(connection, project_id, "agent.created", actor, "role", key,
               {"skills": skill_ids, "mcp_servers": [s["id"] for s in servers],
                "reports_to": parent})
    return get_agent(connection, project_id, key)


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
) -> dict[str, Any]:
    """Change what is given, and nothing else.

    A FIELD LEFT OUT IS LEFT ALONE. POST /v1/roles used as an update wrote autonomy_level=0 and
    reports_to=NULL whenever they were omitted, so fixing a typo in a mission demoted the agent
    and detached it from the org chart.

    `skills` / `mcp_servers` are the WHOLE holding (anything not listed is taken away);
    `grant_skills` / `grant_mcp_servers` only add — what the Studio wizard needs when it puts a
    profile on an agent that already holds things.
    """
    key = str(role_key or "").strip().lower()
    role = _require_role(connection, project_id, key)
    spec = dict(role["spec"] if isinstance(role["spec"], dict) else {})
    changed: dict[str, Any] = {}

    if display_name is not _UNSET:
        name = str(display_name or "").strip()
        if not name:
            raise AgentError("an agent needs a name; leave the field out to keep the current one.")
        changed["display_name"] = name
        spec["display_name"] = name
    if mission is not _UNSET:
        spec["mission"] = str(mission or "").strip()[:2000]
        changed["mission"] = spec["mission"]
    if boundaries is not _UNSET:
        spec["boundaries"] = _boundaries(boundaries)
        changed["boundaries"] = spec["boundaries"]
    parent = role["reports_to"]
    if reports_to is not _UNSET:
        parent = _check_reports_to(connection, project_id, key, reports_to)
        changed["reports_to"] = parent

    forbidden = ((spec.get("permissions") or {}).get("forbidden_skills")) or []
    desired_skills = (None if skills is _UNSET
                      else set(_validate_skills(connection, project_id, skills or [], forbidden)))
    added_skills = set(_validate_skills(connection, project_id, grant_skills, forbidden))
    desired_mcp = (None if mcp_servers is _UNSET
                   else _mcp_entries(mcp_servers or []))
    added_mcp = _mcp_entries(grant_mcp_servers)

    if {"display_name", "mission", "boundaries"} & changed.keys():
        connection.execute(
            text("UPDATE roles SET display_name = :name, spec = CAST(:spec AS JSONB) "
                 "WHERE project_id = :p AND role_key = :k"),
            {"p": project_id, "k": key, "name": spec.get("display_name") or role["display_name"],
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

    from .skill_definitions import unassign_skill_from_role

    held = _held_skill_ids(connection, project_id, key)
    if desired_skills is not None:
        for skill in sorted(held - desired_skills - added_skills):
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
    return get_agent(connection, project_id, key)


def delete_agent(connection: Connection, project_id: str, role_key: str, *,
                 actor: str) -> dict[str, Any]:
    """Take an agent off the roster, or say exactly why it cannot be.

    REFUSED WHILE A WORKFLOW STEP NAMES IT. Deleting the agent underneath a step turns a working
    workflow into one that fails at that step, minutes into a run, with "not a role this
    workforce has". The refusal names every step so the operator can move them first.
    """
    key = str(role_key or "").strip().lower()
    role = _require_role(connection, project_id, key)

    named = steps_naming(connection, project_id, key)
    if named:
        shown = "; ".join(
            f"workflow '{n['workflow']}' step '{n['step']}' ({'a run in flight' if n['because'] == 'live run' else 'v' + str(n['version'])}, {n['how']})"
            for n in named[:6])
        more = f" and {len(named) - 6} more" if len(named) > 6 else ""
        raise AgentError(
            f"{role['display_name']} cannot be deleted: {len(named)} workflow step"
            f"{'s name' if len(named) != 1 else ' names'} it — {shown}{more}. Give those steps "
            "to another agent (or leave them for BOSS to pick) and save the workflow, then "
            "delete again.", status=409)

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
                "reports_moved_up": list(moved)})
    return {"role_key": key, "deleted": True, "skills_released": sorted(skills),
            "mcp_servers_released": sorted(mcp),
            "studio_profiles_retired": [p["agent_key"] for p in profiles],
            "reports_moved_to": role["reports_to"], "reports_moved": sorted(moved)}
