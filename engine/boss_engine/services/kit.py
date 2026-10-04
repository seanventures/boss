"""The Kit: what a crew can do, as abilities (skills-mcp spec §3, §5, §6.3, E8).

WHY THIS EXISTS
---------------
Skills & MCP was a parts bin: raw skill ids, collapsed lists, MCP servers with four badges, and
the accounts those depend on scattered over four other areas. A new operator could not answer
"can my crew open PRs, research the web, file tickets and send email — and is it working?".

An ABILITY is that answer's unit. It is one outcome (`Open pull requests`), backed by at most one
connection (kit_connections), some skills and some MCP tools, and it is DATA: the kit file
`packages/specpack/kits/software_factory.yaml` (+ `media.yaml` when the media pack is installed).
This module turns that file plus the project's state into the Kit the console shows:

* ONE STATUS per ability, computed here and nowhere else (`status`), with exactly one next
  action — precedence testing > failing > review > setup|needs_repo > off > unheld > ready;
* who HOLDS it — the existing grants (role_skills, role_mcp_servers), read and written as a
  set (`set_holders`), so granting an ability writes exactly the rows a person would have
  written by hand, and nothing new is invented at run time;
* who it is RECOMMENDED for — role specs name abilities (`permissions.abilities`, granted on
  install; `permissions.recommended_abilities`, offered);
* each tool's ALLOW / ASK / OFF (`tool_policy` on MCP servers, the kit state for skills), never
  below the floor: tier >= 2 always asks a person.

Nothing here talks to a provider; kit_connections does, outside transactions.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

#: How long a passing test keeps an ability "Ready" without a tooltip asking to test again.
FRESH_SECONDS = 24 * 3600

#: project_configurations row: {disabled: [ability], skill_policy: {skill_id: policy}}.
STATE_KEY = "kit_state"

#: A built-in workflow step's capability, and the ability it needs (Studio Check, Used in).
CAPABILITY_ABILITY = {
    "build": "open_prs",
    "retry_build": "open_prs",
    "test": "tests_ci",
    "deploy": "deploy",
    "redeploy": "deploy",
    "monitor": "watch_health",
    "document": "render_docs",
}

#: The order the Kit shows statuses' precedence in (spec §5.2).
PRECEDENCE = ("testing", "failing", "review", "setup", "needs_repo", "off", "unheld", "untested",
              "ready")


class KitError(ValueError):
    """Something the operator can fix, in their words. The API answers 422 (or `status`)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# The kit file                                                                 #
# --------------------------------------------------------------------------- #

_FILES: dict[str, tuple[float, dict[str, Any]]] = {}


def _kits_dir(settings: Any):
    return settings.specpack_root / "kits"


def _read(settings: Any, name: str) -> dict[str, Any]:
    import yaml

    path = _kits_dir(settings) / name
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _FILES.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    data = yaml.safe_load(path.read_text()) or {}
    _FILES[str(path)] = (mtime, data)
    return data


def _normalized(raw: dict[str, Any], essentials: list[str]) -> dict[str, Any]:
    ability = dict(raw)
    ability.setdefault("skills", [])
    ability["mcp"] = [{"server": str(m.get("server")),
                       "tools": [str(t) for t in m.get("tools") or []] or None}
                      for m in ability.get("mcp") or []]
    ability.setdefault("tools", [])
    ability.setdefault("depends_on", [])
    ability.setdefault("connection", None)
    ability.setdefault("provenance", "built_in")
    ability["essential"] = ability["key"] in essentials
    return ability


def definitions(settings: Any, *, media: bool = False) -> dict[str, Any]:
    """{groups, essentials, abilities}: the software factory kit, plus Media when installed."""
    main = _read(settings, "software_factory.yaml")
    essentials = [str(e) for e in main.get("essentials") or []]
    groups = [str(g) for g in main.get("groups") or []]
    abilities = [_normalized(a, essentials) for a in main.get("abilities") or []]
    if media:
        extra = _read(settings, "media.yaml")
        groups += [g for g in extra.get("groups") or [] if g not in groups]
        abilities += [_normalized(a, essentials) for a in extra.get("abilities") or []]
    return {"groups": groups, "essentials": essentials, "abilities": abilities}


def all_abilities(settings: Any) -> list[dict[str, Any]]:
    """Every ability any kit file defines — what a role spec may name."""
    return definitions(settings, media=True)["abilities"]


def ability_def(settings: Any, key: str) -> dict[str, Any]:
    found = next((a for a in all_abilities(settings) if a["key"] == key), None)
    if found is None:
        raise KitError(f"no ability {key!r}.", status=404)
    return found


def media_installed(connection: Connection, project_id: str) -> bool:
    try:
        return bool(connection.execute(
            text("SELECT 1 FROM pack_installs WHERE project_id = :p AND pack_id = 'media_pack'"),
            {"p": project_id}).first())
    except Exception:  # noqa: BLE001
        return False


def expand(settings: Any, keys: list[str]) -> dict[str, Any]:
    """What naming these abilities grants: {skills: [...], mcp: [{server, tools}]}. An unknown
    key is reported, not invented (`unknown`)."""
    by_key = {a["key"]: a for a in all_abilities(settings)}
    skills: list[str] = []
    servers: dict[str, Optional[set[str]]] = {}
    unknown: list[str] = []
    for key in keys:
        ability = by_key.get(str(key))
        if ability is None:
            unknown.append(str(key))
            continue
        for skill in ability["skills"]:
            if skill not in skills:
                skills.append(skill)
        for entry in ability["mcp"]:
            name = entry["server"]
            if entry["tools"] is None:
                servers[name] = None
            elif servers.get(name, set()) is not None:
                servers[name] = set(servers.get(name) or set()) | set(entry["tools"])
    return {"skills": skills,
            "mcp": [{"server": name, "tools": sorted(tools) if tools is not None else []}
                    for name, tools in servers.items()],
            "unknown": unknown}


def role_abilities(spec: Any) -> tuple[list[str], list[str]]:
    """(granted, recommended-only) ability keys a role spec names."""
    permissions = (spec or {}).get("permissions") if isinstance(spec, dict) else None
    permissions = permissions if isinstance(permissions, dict) else {}
    granted = [str(a) for a in permissions.get("abilities") or []]
    offered = [str(a) for a in permissions.get("recommended_abilities") or []
               if str(a) not in granted]
    return granted, offered


def ability_subject(role_key: str, ability: str) -> str:
    """The seed-exclusion subject for "this agent no longer holds this ability"."""
    from . import seed_exclusions

    return seed_exclusions.role_skill_subject(role_key, f"ability:{ability}")


# --------------------------------------------------------------------------- #
# Kit state: abilities turned off, skill policies                              #
# --------------------------------------------------------------------------- #

def _state(connection: Connection, project_id: str) -> dict[str, Any]:
    from .kit_connections import _config

    body = _config(connection, project_id, STATE_KEY) or {}
    return {"disabled": [str(k) for k in body.get("disabled") or []],
            "skill_policy": dict(body.get("skill_policy") or {}),
            # {ability: [role, ...]}: who held an ability when its server was disconnected,
            # so reconnecting gives it back to exactly them (restore_server_holders).
            "kept_holders": {str(k): [str(r) for r in v or []]
                             for k, v in (body.get("kept_holders") or {}).items()}}


def _save_state(connection: Connection, project_id: str, state: dict[str, Any],
                actor: str) -> None:
    from .kit_connections import _put_config

    _put_config(connection, project_id, STATE_KEY, state, actor)


def _kit_skill_rows(settings: Any) -> dict[str, Optional[str]]:
    """{skill_id: declared policy or None} for every skill a kit tool row lists."""
    out: dict[str, Optional[str]] = {}
    for ability in all_abilities(settings):
        for skill in ability["skills"]:
            out.setdefault(skill, None)
        for tool in ability["tools"]:
            ref = str(tool.get("ref") or "")
            if ref.startswith("skill:"):
                declared = tool.get("policy")
                out[ref[6:]] = declared if declared in ("allow", "ask", "off") else (
                    out.get(ref[6:]))
    return out


def skill_policy(connection: Connection, settings: Any, project_id: str, skill_id: str,
                 tier: int) -> Optional[str]:
    """The effective Allow / Ask / Off of one skill, or None when nobody has a say over it.

    A skill the operator set, or one a kit tool row lists, has a policy (stored, else the kit
    file's default, else the tier's) and the floor applies. Any other skill keeps its old
    behaviour exactly: None.
    """
    from .mcp_registry import effective_policy

    try:
        stored = _state(connection, project_id)["skill_policy"].get(skill_id)
    except Exception:  # noqa: BLE001 — an install predating the table has none
        stored = None
    rows = _kit_skill_rows(settings)
    if stored is None and skill_id not in rows:
        return None
    return effective_policy(int(tier), stored or rows.get(skill_id))


# --------------------------------------------------------------------------- #
# Grants                                                                       #
# --------------------------------------------------------------------------- #

def _servers_by_name(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    from .mcp_registry import list_mcp_servers

    return {str(s["name"]).lower(): s for s in list_mcp_servers(connection, project_id)}


def _grants(connection: Connection, project_id: str
            ) -> tuple[dict[str, set[str]], dict[str, dict[str, Optional[set[str]]]]]:
    """({role: skills}, {role: {server_id: tools or None (= every tool)}})."""
    skills: dict[str, set[str]] = {}
    for row in connection.execute(
            text("SELECT role_key, skill_id FROM role_skills WHERE project_id = :p"),
            {"p": project_id}).mappings():
        skills.setdefault(str(row["role_key"]), set()).add(str(row["skill_id"]))
    servers: dict[str, dict[str, Optional[set[str]]]] = {}
    for row in connection.execute(
            text("SELECT role_key, mcp_server_id, tools FROM role_mcp_servers "
                 "WHERE project_id = :p"), {"p": project_id}).mappings():
        tools = row["tools"]
        if isinstance(tools, str):
            tools = json.loads(tools or "[]")
        servers.setdefault(str(row["role_key"]), {})[str(row["mcp_server_id"])] = (
            set(tools) if tools else None)
    return skills, servers


def _holds(ability: dict[str, Any], role: str, skills: dict[str, set[str]],
           servers: dict[str, dict[str, Optional[set[str]]]],
           by_name: dict[str, dict[str, Any]]) -> bool:
    if not ability["skills"] and not ability["mcp"]:
        return False
    if not set(ability["skills"]) <= skills.get(role, set()):
        return False
    for entry in ability["mcp"]:
        server = by_name.get(entry["server"].lower())
        if server is None:
            return False
        held = servers.get(role, {})
        if server["id"] not in held:
            return False
        tools = held[server["id"]]
        if tools is not None and entry["tools"] is not None \
                and not set(entry["tools"]) <= tools:
            return False
    return True


def _roles(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text("SELECT role_key, display_name, spec FROM roles WHERE project_id = :p "
             "AND role_key <> 'human' ORDER BY role_key"), {"p": project_id}).mappings().all()
    return {str(r["role_key"]): {"display_name": r["display_name"],
                                 "spec": r["spec"] if isinstance(r["spec"], dict) else {}}
            for r in rows}


def holders(connection: Connection, settings: Any, project_id: str,
            abilities: list[dict[str, Any]]) -> dict[str, list[str]]:
    skills, servers = _grants(connection, project_id)
    by_name = _servers_by_name(connection, project_id)
    roles = _roles(connection, project_id)
    return {a["key"]: [r for r in roles if _holds(a, r, skills, servers, by_name)]
            for a in abilities}


def recommended(roles: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for role, row in roles.items():
        granted, offered = role_abilities(row["spec"])
        for key in granted + offered:
            out.setdefault(key, []).append(role)
    return out


def _tools_of(server: dict[str, Any]) -> set[str]:
    return {str(t.get("name")) for t in server.get("tools") or [] if t.get("name")}


def _grant(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any],
           role: str, actor: str) -> list[str]:
    """Give one ability to one role: its skills and MCP grants. Returns why parts were not
    granted (empty when all of it was)."""
    from . import seed_exclusions
    from .agents import AgentError, grant_mcp
    from .skill_definitions import SkillDefinitionError, assign_skill_to_role

    problems: list[str] = []
    skills, servers = _grants(connection, project_id)
    for skill in ability["skills"]:
        if skill in skills.get(role, set()):
            continue
        try:
            assign_skill_to_role(connection, project_id=project_id, actor=actor,
                                 role_key=role, skill_id=skill)
        except SkillDefinitionError as error:
            problems.append(str(error))
    by_name = _servers_by_name(connection, project_id)
    for entry in ability["mcp"]:
        server = by_name.get(entry["server"].lower())
        if server is None:
            problems.append(f"MCP server {entry['server']!r} is not registered.")
            continue
        held = servers.get(role, {})
        if server["id"] in held:
            current = held[server["id"]]
            if current is None:
                continue
            wanted = sorted(current | set(entry["tools"])) if entry["tools"] is not None else []
            if entry["tools"] is not None and set(entry["tools"]) <= current:
                continue
        else:
            wanted = sorted(entry["tools"]) if entry["tools"] is not None else []
        try:
            grant_mcp(connection, project_id, actor=actor, role_key=role,
                      mcp_server_id=server["id"], tools=wanted)
        except AgentError as error:
            problems.append(str(error))
    seed_exclusions.lift(connection, project_id, "role_skill", ability_subject(role, ability["key"]))
    return problems


def _revoke(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any],
            role: str, actor: str, others: list[dict[str, Any]]) -> None:
    """Take one ability off one role, keeping what another ability it still holds needs."""
    from . import seed_exclusions
    from .agents import grant_mcp, revoke_mcp
    from .skill_definitions import unassign_skill_from_role

    skills, servers = _grants(connection, project_id)
    by_name = _servers_by_name(connection, project_id)
    still = [o for o in others if o["key"] != ability["key"]
             and _holds(o, role, skills, servers, by_name)]
    needed_skills = {s for o in still for s in o["skills"]}
    for skill in ability["skills"]:
        if skill in skills.get(role, set()) and skill not in needed_skills:
            unassign_skill_from_role(connection, project_id=project_id, actor=actor,
                                     role_key=role, skill_id=skill)
    for entry in ability["mcp"]:
        server = by_name.get(entry["server"].lower())
        if server is None or server["id"] not in servers.get(role, {}):
            continue
        current = servers[role][server["id"]]
        need: Optional[set[str]] = set()
        for other in still:
            for o_entry in other["mcp"]:
                if o_entry["server"].lower() != entry["server"].lower():
                    continue
                if o_entry["tools"] is None:
                    need = None
                elif need is not None:
                    need |= set(o_entry["tools"])
        if need is None:
            continue  # another held ability needs every tool of this server
        if entry["tools"] is None:
            remaining = set(need)
        else:
            base = current if current is not None else _tools_of(server)
            remaining = (base - set(entry["tools"])) | need
        if remaining:
            grant_mcp(connection, project_id, actor=actor, role_key=role,
                      mcp_server_id=server["id"], tools=sorted(remaining))
        else:
            revoke_mcp(connection, project_id, actor=actor, role_key=role,
                       mcp_server_id=server["id"])
    seed_exclusions.exclude(connection, project_id, "role_skill",
                            ability_subject(role, ability["key"]), actor=actor,
                            reason="ability taken off the agent")


def set_holders(connection: Connection, settings: Any, project_id: str, key: str,
                roles: list[str], *, actor: str) -> dict[str, Any]:
    """Exactly these roles hold the ability (spec §6.3 PUT holders): set semantics, one
    transaction (the caller's). A removal is remembered so the next seed does not undo it."""
    from .agent_identity import sealing_once

    defs = definitions(settings, media=media_installed(connection, project_id))
    ability = next((a for a in defs["abilities"] if a["key"] == key), None)
    if ability is None:
        raise KitError(f"no ability {key!r}.", status=404)
    known = _roles(connection, project_id)
    wanted = {str(r).strip().lower() for r in roles if str(r).strip()}
    unknown = sorted(wanted - set(known))
    if unknown:
        raise KitError(f"no agent {', '.join(unknown)} on this project.")
    current = set(holders(connection, settings, project_id, [ability])[key])
    added = sorted(wanted - current)
    if added:
        status = _status_of(connection, settings, project_id, ability)
        if status in ("setup", "needs_repo"):
            raise KitError(f"Set up {ability['label']} first", status=409)
    with sealing_once():
        for role in added:
            problems = _grant(connection, settings, project_id, ability, role, actor)
            if problems:
                raise KitError(f"Could not give {ability['label']} to {role}: {problems[0]}")
        for role in sorted(current - wanted):
            _revoke(connection, settings, project_id, ability, role, actor, defs["abilities"])
    emit_event(connection, project_id, "kit.holders_set", actor, "kit_ability", key,
               {"added": added, "removed": sorted(current - wanted)})
    return ability_view(connection, settings, project_id, key)


def abilities_on_server(settings: Any, server_name: str) -> list[dict[str, Any]]:
    """Every ability (media included) whose kit entry names this MCP server."""
    name = str(server_name or "").lower()
    return [a for a in definitions(settings, media=True)["abilities"]
            if any(e["server"].lower() == name for e in a["mcp"])]


def remember_server_holders(connection: Connection, settings: Any, project_id: str,
                            server_name: str, *, actor: str) -> None:
    """Before a server is disconnected: who holds each ability it backs. Deleting the server
    releases the grants; reconnecting reads this back so nobody has to re-grant by hand."""
    abilities = abilities_on_server(settings, server_name)
    if not abilities:
        return
    held = holders(connection, settings, project_id, abilities)
    state = _state(connection, project_id)
    for ability in abilities:
        state["kept_holders"][ability["key"]] = list(held[ability["key"]])
    _save_state(connection, project_id, state, actor)


def restore_server_holders(connection: Connection, settings: Any, project_id: str,
                           server_name: str, *, actor: str) -> dict[str, list[str]]:
    """After a server is (re)connected: give each ability it backs to the agents that held it
    before a disconnect — or, the first time, to the agents whose role recommends it (spec
    §3.2: engineers hold Library docs). Returns {ability: roles granted}."""
    from .agent_identity import sealing_once

    abilities = abilities_on_server(settings, server_name)
    if not abilities:
        return {}
    state = _state(connection, project_id)
    roles = _roles(connection, project_id)
    recs = recommended(roles)
    held = holders(connection, settings, project_id, abilities)
    out: dict[str, list[str]] = {}
    with sealing_once():
        for ability in abilities:
            key = ability["key"]
            wanted = state["kept_holders"].pop(key, None)
            if wanted is None:
                wanted = recs.get(key, [])
            granted = []
            for role in wanted:
                if role not in roles or role in held[key]:
                    continue
                if not _grant(connection, settings, project_id, ability, role, actor):
                    granted.append(role)
            out[key] = granted
    _save_state(connection, project_id, state, actor)
    return out


def apply_recommended(connection: Connection, settings: Any, project_id: str, *, actor: str,
                      roles: Optional[list[str]] = None) -> dict[str, Any]:
    """Give every agent (or only `roles`) its role's recommended abilities, in one press.

    Skips, with the reason, what cannot be granted yet (an ability that needs setting up)."""
    from .agent_identity import sealing_once

    defs = definitions(settings, media=media_installed(connection, project_id))
    by_key = {a["key"]: a for a in defs["abilities"]}
    known = _roles(connection, project_id)
    held = holders(connection, settings, project_id, defs["abilities"])
    wanted = {str(r).strip().lower() for r in roles} if roles else set(known)
    granted: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    statuses: dict[str, str] = {}
    with sealing_once():
        for role in sorted(wanted & set(known)):
            granted_keys, offered = role_abilities(known[role]["spec"])
            for key in granted_keys + offered:
                ability = by_key.get(key)
                if ability is None or role in held.get(key, []):
                    continue
                if key not in statuses:
                    statuses[key] = _status_of(connection, settings, project_id, ability)
                if statuses[key] in ("setup", "needs_repo"):
                    skipped.append({"role_key": role, "ability": key,
                                    "reason": f"Set up {ability['label']} first"})
                    continue
                problems = _grant(connection, settings, project_id, ability, role, actor)
                if problems:
                    skipped.append({"role_key": role, "ability": key, "reason": problems[0]})
                else:
                    granted.append({"role_key": role, "ability": key})
    emit_event(connection, project_id, "kit.recommended_applied", actor, "project", project_id,
               {"granted": len(granted), "skipped": len(skipped)})
    return {"granted": granted, "skipped": skipped}


def sync_role_abilities(connection: Connection, settings: Any, project_id: str, *,
                        actor: str = "system") -> dict[str, Any]:
    """The seed's half: every role holds the abilities its spec GRANTS (`abilities`).

    Additive, like sync_role_skills and sync_role_mcp: an ability an operator took off an
    agent stays off (seed_exclusions), and a part that cannot be granted (a server the operator
    removed) is reported, never invented."""
    from . import seed_exclusions
    from .agent_identity import sealing_once

    removed = seed_exclusions.excluded(connection, project_id, "role_skill")
    defs = definitions(settings, media=True)
    by_key = {a["key"]: a for a in defs["abilities"]}
    granted: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []
    # READ ONCE, re-read only after a write: this runs on every boot for every project, and a
    # read per role per ability was hundreds of queries to find nothing to do.
    skills, servers = _grants(connection, project_id)
    by_name = _servers_by_name(connection, project_id)
    with sealing_once():
        for role, row in _roles(connection, project_id).items():
            keys, _ = role_abilities(row["spec"])
            for key in keys:
                ability = by_key.get(key)
                if ability is None:
                    unavailable.append({"role_key": role, "ability": key,
                                        "reason": "no such ability"})
                    continue
                if ability_subject(role, key) in removed:
                    continue
                if _holds(ability, role, skills, servers, by_name):
                    continue
                problems = _grant_quietly(connection, settings, project_id, ability, role,
                                          actor, removed)
                if problems:
                    unavailable.append({"role_key": role, "ability": key,
                                        "reason": problems[0]})
                else:
                    granted.append({"role_key": role, "ability": key})
                skills, servers = _grants(connection, project_id)
    return {"granted": granted, "unavailable": unavailable}


def _grant_quietly(connection: Connection, settings: Any, project_id: str,
                   ability: dict[str, Any], role: str, actor: str, removed: set[str]
                   ) -> list[str]:
    """`_grant` for the seed: never re-grants a skill or server an operator took off the agent,
    and never lifts an exclusion (the seed is not a person deciding)."""
    from . import seed_exclusions
    from .agents import AgentError, grant_mcp, role_mcp_subject
    from .skill_definitions import SkillDefinitionError, assign_skill_to_role

    problems: list[str] = []
    skills, servers = _grants(connection, project_id)
    for skill in ability["skills"]:
        if skill in skills.get(role, set()):
            continue
        if seed_exclusions.role_skill_subject(role, skill) in removed:
            problems.append(f"{skill} was taken off {role}")
            continue
        try:
            with connection.begin_nested():
                assign_skill_to_role(connection, project_id=project_id, actor=actor,
                                     role_key=role, skill_id=skill)
        except SkillDefinitionError as error:
            problems.append(str(error))
    by_name = _servers_by_name(connection, project_id)
    for entry in ability["mcp"]:
        server = by_name.get(entry["server"].lower())
        if server is None:
            problems.append(f"MCP server {entry['server']!r} is not registered")
            continue
        if role_mcp_subject(role, server["name"]) in removed:
            problems.append(f"{server['name']} was taken off {role}")
            continue
        held = servers.get(role, {})
        current = held.get(server["id"], set()) if server["id"] in held else set()
        if server["id"] in held and current is None:
            continue
        if entry["tools"] is None:
            wanted: list[str] = []
        else:
            if server["id"] in held and set(entry["tools"]) <= (current or set()):
                continue
            wanted = sorted((current or set()) | set(entry["tools"]))
        try:
            grant_mcp(connection, project_id, actor=actor, role_key=role,
                      mcp_server_id=server["id"], tools=wanted)
        except AgentError as error:
            problems.append(str(error))
    # grant_mcp lifts an exclusion it finds; the seed only reaches here when none applied.
    return problems


# --------------------------------------------------------------------------- #
# Status                                                                       #
# --------------------------------------------------------------------------- #

def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def last_tests(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    try:
        rows = connection.execute(
            text("""SELECT DISTINCT ON (ability) ability, ok, latency_ms, fact, error, field, at
                      FROM kit_tests WHERE project_id = :p ORDER BY ability, at DESC"""),
            {"p": project_id}).mappings().all()
    except Exception:  # noqa: BLE001 — an install predating the table has no tests yet
        return {}
    return {str(r["ability"]): {"ok": bool(r["ok"]), "at": _iso(r["at"]),
                                "latency_ms": int(r["latency_ms"] or 0), "fact": r["fact"],
                                "error": r["error"], "field": r["field"]} for r in rows}


def record_test(connection: Connection, project_id: str, ability: str, outcome: dict[str, Any],
                *, actor: str) -> None:
    connection.execute(
        text("""INSERT INTO kit_tests (id, project_id, ability, ok, latency_ms, fact, error,
                                       field, actor)
                VALUES (:id, :p, :a, :ok, :ms, :fact, :error, :field, :actor)"""),
        {"id": make_id("ktest"), "p": project_id, "a": ability, "ok": bool(outcome["ok"]),
         "ms": int(outcome.get("latency_ms") or 0), "fact": outcome.get("fact"),
         "error": (outcome.get("error") or None) and str(outcome["error"])[:1000],
         "field": outcome.get("field"), "actor": actor})


def _age(at: Optional[str]) -> tuple[str, float]:
    if not at:
        return "", 0.0
    try:
        when = datetime.fromisoformat(str(at))
    except ValueError:
        return "", 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (datetime.now(timezone.utc) - when).total_seconds())
    if seconds < 60:
        return "just now", seconds
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago", seconds
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago", seconds
    return f"{int(seconds // 86400)}d ago", seconds


def compute_status(*, disabled: bool, needs_repo: bool, needs_setup: bool,
                   failing: bool, review: bool, held: bool, tested: bool = True) -> str:
    """The single status (spec §5.2), by precedence. `testing` is the console's alone.

    READY MEANS IT PASSED A TEST. An ability that is set up and held but has never passed its
    test is `untested`: it does not count toward "n of 12 ready", and its button is Test."""
    if failing:
        return "failing"
    if review:
        return "review"
    if needs_setup:
        return "setup"
    if needs_repo:
        return "needs_repo"
    if disabled:
        return "off"
    if not held:
        return "unheld"
    if not tested:
        return "untested"
    return "ready"


def _facts(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any],
           *, dep: dict[str, Any], state: dict[str, Any], tests: dict[str, dict[str, Any]],
           by_name: dict[str, dict[str, Any]], held: list[str]) -> dict[str, Any]:
    from . import kit_connections

    conn = kit_connections.view(connection, settings, project_id, ability, dep)
    servers = [by_name.get(e["server"].lower()) for e in ability["mcp"]]
    present = [s for s in servers if s is not None]
    missing_server = len(present) < len(servers)
    needs_repo = "github" in ability["depends_on"] and not dep["github"]["connected"]
    needs_setup = (("deploy_target" in ability["depends_on"] and not dep["deploy_target"])
                   or (conn is not None and not conn["connected"])
                   or (missing_server and not (conn and conn["connected"])))
    if ability["key"] == "file_tickets" and conn is not None and not conn["connected"] \
            and not dep["github"]["connected"]:
        needs_setup, needs_repo = True, False
    last = tests.get(ability["key"])
    unreachable = next((s for s in present if s.get("enabled") and s.get("reachable") is False),
                       None)
    # SETUP'S GITHUB CHECK FAILED: the repo is connected (or was tried) and does not work, so
    # every ability that needs it is Failing with that reason, not "Needs repo".
    github_failing = dep["github"].get("failing") if "github" in ability["depends_on"] else None
    if github_failing and not dep["github"]["connected"] and not dep["github"].get(
            "has_connection"):
        github_failing = None
    if github_failing:
        needs_repo = False
    failing = bool(last and not last["ok"]) or unreachable is not None or bool(github_failing)
    untrusted = [s for s in present if s.get("enabled", True) and not s.get("trusted")]
    disabled = ability["key"] in state["disabled"] or any(
        s is not None and not s.get("enabled", True) for s in present)
    status = compute_status(disabled=disabled, needs_repo=needs_repo, needs_setup=needs_setup,
                            failing=failing, review=bool(untrusted), held=bool(held),
                            tested=bool(last and last["ok"]))
    line: Optional[str] = None
    tip = ""
    if status == "failing":
        error = (last or {}).get("error") if last and not last["ok"] else (
            f"{unreachable['name']}: {unreachable.get('last_error') or 'no answer'}"
            if unreachable else (f"Setup's GitHub check failed: {github_failing}"
                                 if github_failing else None))
        line = kit_connections.short_error(error)
        tip = f"Last test failed: {error or 'no answer'}"
    elif status == "review":
        count = sum(len(s.get("tools") or []) for s in untrusted)
        line = (f"{count} tool{'s' if count != 1 else ''} need"
                f"{'' if count != 1 else 's'} review" if count else "Server needs review")
        tip = "Unreviewed: every call waits for a person"
    elif status == "setup":
        tip = "Needs an account or key first"
    elif status == "needs_repo":
        tip = "Connect your GitHub repo in Setup first"
    elif status == "off":
        tip = "No agent or step can use it"
    elif status == "unheld":
        tip = "Works, but no agent may use it yet"
    elif status == "untested":
        tip = "Not tested yet · Test"
    else:
        age, seconds = _age((last or {}).get("at"))
        if seconds > FRESH_SECONDS:
            tip = f"Tested {age} · Test again"
        else:
            tip = f"Tested {age}" + (f" · {last['fact']}" if last.get("fact") else "")
    return {"status": status, "status_line": line, "status_tip": tip, "connection": conn,
            "servers": present, "last_test": last}


def _status_of(connection: Connection, settings: Any, project_id: str,
               ability: dict[str, Any]) -> str:
    from . import kit_connections

    held = holders(connection, settings, project_id, [ability])[ability["key"]]
    return _facts(connection, settings, project_id, ability,
                  dep=kit_connections.deps(connection, settings, project_id),
                  state=_state(connection, project_id),
                  tests=last_tests(connection, project_id),
                  by_name=_servers_by_name(connection, project_id), held=held)["status"]


# --------------------------------------------------------------------------- #
# Tools and their policies                                                     #
# --------------------------------------------------------------------------- #

def _humanise(name: str) -> str:
    words = re.sub(r"[_\-.]+", " ", name).strip()
    return words[:1].upper() + words[1:] if words else name


def _catalogue(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    from .skill_catalogue import entries

    try:
        return entries(connection, project_id, settings=settings)
    except Exception:  # noqa: BLE001
        return {}


def tools_view(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any],
               *, state: dict[str, Any], by_name: dict[str, dict[str, Any]],
               catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    from .mcp_registry import effective_policy, policy_floor, tool_policy, tool_tier

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    rows = list(ability["tools"])
    for skill in ability["skills"]:
        if not any(str(t.get("ref")) == f"skill:{skill}" for t in rows):
            rows.append({"ref": f"skill:{skill}"})
    for row in rows:
        ref = str(row.get("ref") or "")
        if ref in seen or not ref.startswith("skill:"):
            continue
        seen.add(ref)
        skill = ref[6:]
        entry = catalogue.get(skill) or {}
        tier = int(entry.get("risk_tier") or 0)
        stored = state["skill_policy"].get(skill)
        declared = row.get("policy") if row.get("policy") in ("allow", "ask", "off") else None
        out.append({"ref": ref, "label": str(row.get("label") or entry.get("label") or
                                             _humanise(skill)),
                    "tier": tier, "policy": effective_policy(tier, stored or declared),
                    "floor": policy_floor(tier), "kind": "skill"})
    for entry in ability["mcp"]:
        server = by_name.get(entry["server"].lower())
        if server is None:
            continue
        names = entry["tools"] if entry["tools"] is not None else sorted(_tools_of(server))
        known = {str(t.get("name")): t for t in server.get("tools") or []}
        for name in names:
            ref = f"mcp:{server['name']}.{name}"
            if ref in seen:
                continue
            seen.add(ref)
            tool = known.get(name) or {}
            annotations = tool.get("annotations") or {}
            declared = tool.get("risk_tier", annotations.get("risk_tier")
                                if isinstance(annotations, dict) else None)
            tier = tool_tier(server, name, declared)
            label = next((str(t.get("label")) for t in ability["tools"]
                          if str(t.get("ref")) == ref and t.get("label")), _humanise(name))
            out.append({"ref": ref, "label": label, "tier": tier,
                        "policy": tool_policy(server, name, declared),
                        "floor": policy_floor(tier), "kind": "mcp"})
    return out


def set_policy(connection: Connection, settings: Any, project_id: str, key: str,
               tool_ref: str, policy: str, *, actor: str) -> dict[str, Any]:
    """Allow / Ask / Off for one tool row. 409 (KitError status) below the floor."""
    from .mcp_registry import PolicyBelowFloor, checked_policy, find_mcp_server, set_tool_policy

    ability = ability_def(settings, key)
    by_name = _servers_by_name(connection, project_id)
    state = _state(connection, project_id)
    rows = tools_view(connection, settings, project_id, ability, state=state, by_name=by_name,
                      catalogue=_catalogue(connection, settings, project_id))
    row = next((r for r in rows if r["ref"] == tool_ref), None)
    if row is None:
        raise KitError(f"{ability['label']} has no tool {tool_ref!r}.", status=404)
    try:
        checked_policy(row["tier"], policy)
        if row["kind"] == "skill":
            state["skill_policy"][tool_ref[6:]] = policy
            _save_state(connection, project_id, state, actor)
            emit_event(connection, project_id, "kit.tool_policy_set", actor, "kit_ability", key,
                       {"tool": tool_ref, "policy": policy})
        else:
            server_name, _, tool = tool_ref[4:].partition(".")
            server = find_mcp_server(connection, project_id, server_name)
            if server is None:
                raise KitError("That MCP server is gone.", status=404)
            set_tool_policy(connection, project_id, server["id"], tool, policy, actor=actor)
    except PolicyBelowFloor as error:
        raise KitError(str(error), status=409) from error
    return ability_view(connection, settings, project_id, key)


def set_enabled(connection: Connection, settings: Any, project_id: str, key: str,
                enabled: bool, *, actor: str) -> dict[str, Any]:
    """Turn an ability off (or on): its tools stop being offered or run. A dedicated server
    (one only this ability uses, like a catalogue server) is switched off with it."""
    from .mcp_registry import set_mcp_enabled, set_tool_policies

    ability = ability_def(settings, key)
    state = _state(connection, project_id)
    disabled = [k for k in state["disabled"] if k != key]
    if not enabled:
        disabled.append(key)
    state["disabled"] = disabled
    # THE RUNTIME HONOURS IT through the same switches a person would use: every skill row
    # turned Off (and back to its default on), every MCP tool of the ability likewise.
    by_name = _servers_by_name(connection, project_id)
    for skill in ability["skills"]:
        if enabled:
            state["skill_policy"].pop(skill, None)
        else:
            state["skill_policy"][skill] = "off"
    _save_state(connection, project_id, state, actor)
    for entry in ability["mcp"]:
        server = by_name.get(entry["server"].lower())
        if server is None:
            continue
        if entry["tools"] is None:
            set_mcp_enabled(connection, project_id, server["id"], enabled, actor=actor)
            continue
        policies = dict(server.get("tool_policy") or {})
        for tool in entry["tools"]:
            if enabled:
                policies.pop(tool, None)
            else:
                policies[tool] = "off"
        set_tool_policies(connection, project_id, server["id"], policies, actor=actor)
    emit_event(connection, project_id, "kit.ability_toggled", actor, "kit_ability", key,
               {"enabled": bool(enabled)})
    return ability_view(connection, settings, project_id, key)


# --------------------------------------------------------------------------- #
# Used in                                                                      #
# --------------------------------------------------------------------------- #

def used_in(connection: Connection, settings: Any, project_id: str,
            abilities: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Workflow steps that use each ability: an agent step scoped to its skills or servers, a
    custom step bound to one, a skill step, or a built-in capability that needs it."""
    out: dict[str, list[dict[str, Any]]] = {a["key"]: [] for a in abilities}
    by_skill: dict[str, list[str]] = {}
    by_server: dict[str, list[str]] = {}
    for a in abilities:
        for skill in a["skills"]:
            by_skill.setdefault(skill, []).append(a["key"])
        for entry in a["mcp"]:
            by_server.setdefault(entry["server"].lower(), []).append(a["key"])
    try:
        rows = connection.execute(
            text("""SELECT d.workflow_key, d.name, v.spec
                      FROM workflow_definitions d
                      JOIN workflow_versions v ON v.id = d.active_version_id
                     WHERE d.project_id = :p ORDER BY d.name"""), {"p": project_id}
        ).mappings().all()
        custom = {str(r["step_key"]): dict(r) for r in connection.execute(
            text("""SELECT c.step_key, c.skill_id, s.name AS server_name
                      FROM workflow_custom_steps c
                      LEFT JOIN mcp_servers s ON s.id = c.mcp_server_id
                     WHERE c.project_id = :p"""), {"p": project_id}).mappings()}
    except Exception:  # noqa: BLE001 — an install without workflows uses nothing
        return out
    for row in rows:
        spec = row["spec"] if isinstance(row["spec"], dict) else json.loads(row["spec"] or "{}")
        for step in spec.get("steps") or []:
            if not isinstance(step, dict):
                continue
            binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
            # (ability, via): via says WHICH part of the ability the step uses —
            # "skill:<id>", "mcp:<server>" or "capability:<x>" — so the Library row of one skill
            # or server counts its own steps, not every step of the ability.
            uses: set[tuple[str, str]] = set()

            def _skill(skill_id: Any) -> None:
                for key in by_skill.get(str(skill_id), []):
                    uses.add((key, f"skill:{skill_id}"))

            def _server(name: Any) -> None:
                for key in by_server.get(str(name).lower(), []):
                    uses.add((key, f"mcp:{str(name).lower()}"))

            for skill in binding.get("skills") or []:
                _skill(skill)
            for server in (binding.get("mcp") or []) + (binding.get("mcp_servers") or []):
                _server(server)
            if binding.get("skill_id"):
                _skill(binding["skill_id"])
            if binding.get("custom_step") and str(binding["custom_step"]) in custom:
                bound = custom[str(binding["custom_step"])]
                if bound.get("skill_id"):
                    _skill(bound["skill_id"])
                if bound.get("server_name"):
                    _server(bound["server_name"])
            capability = str(binding.get("capability") or "")
            if capability in CAPABILITY_ABILITY:
                uses.add((CAPABILITY_ABILITY[capability], f"capability:{capability}"))
            for key, via in sorted(uses):
                if key in out:
                    out[key].append({"workflow_key": str(row["workflow_key"]),
                                     "workflow_name": str(row["name"]),
                                     "step_id": str(step.get("id") or ""),
                                     "step_title": str(step.get("title") or step.get("id")
                                                       or ""),
                                     "via": via})
    return out


# --------------------------------------------------------------------------- #
# The view                                                                     #
# --------------------------------------------------------------------------- #

def _try_field(ability: dict[str, Any], tools: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    spec = ability.get("try")
    if not isinstance(spec, dict) or not spec.get("field"):
        return None
    tool = next((t for t in tools if t["ref"] == spec.get("tool")), None)
    if tool is None or tool["tier"] != 0 or tool["policy"] == "off":
        return None
    return {"field": dict(spec["field"])}


def _view(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any], *,
          dep: dict[str, Any], state: dict[str, Any], tests: dict[str, Any],
          by_name: dict[str, Any], held: list[str], recommended_for: list[str],
          uses: list[dict[str, Any]], catalogue: dict[str, Any]) -> dict[str, Any]:
    facts = _facts(connection, settings, project_id, ability, dep=dep, state=state,
                   tests=tests, by_name=by_name, held=held)
    tools = tools_view(connection, settings, project_id, ability, state=state, by_name=by_name,
                       catalogue=catalogue)
    conn = facts["connection"]
    return {
        "key": ability["key"], "group": ability["group"], "label": ability["label"],
        "tip": ability.get("tip") or "", "icon": ability.get("icon") or "",
        "provenance": ability.get("provenance") or "built_in",
        "essential": bool(ability.get("essential")),
        "status": facts["status"], "status_line": facts["status_line"],
        "status_tip": facts["status_tip"],
        "connection": None if conn is None else {
            k: conn[k] for k in ("providers", "provider", "set_fields", "acts_as", "flow",
                                 "connected")},
        "tools": tools, "holders": held, "recommended": recommended_for, "used_in": uses,
        "last_test": facts["last_test"], "try": _try_field(ability, tools),
        "depends_on": list(ability["depends_on"]),
        "connect_href": ability.get("connect_href"),
        "scopes_tip": ability.get("scopes_tip"),
        "mcp": [{"server_id": s["id"], "name": s["name"], "url": s.get("url"),
                 "trusted": bool(s.get("trusted")),
                 "reachable": s.get("reachable")} for s in facts["servers"]],
        # The servers the kit file names, registered or not — what a Studio picker offers.
        "mcp_names": [e["server"] for e in ability["mcp"]],
    }


def kit_view(connection: Connection, settings: Any, project_id: str, *,
             only: Optional[str] = None) -> dict[str, Any]:
    """The Kit (kit-types.ts `Kit`): every ability with its one status. `only` limits the
    abilities computed (one drawer)."""
    from . import kit_connections

    defs = definitions(settings, media=media_installed(connection, project_id))
    abilities = [a for a in defs["abilities"] if only is None or a["key"] == only]
    if only is not None and not abilities:
        raise KitError(f"no ability {only!r}.", status=404)
    roles = _roles(connection, project_id)
    held = holders(connection, settings, project_id, abilities)
    recs = recommended(roles)
    dep = kit_connections.deps(connection, settings, project_id)
    state = _state(connection, project_id)
    tests = last_tests(connection, project_id)
    by_name = _servers_by_name(connection, project_id)
    uses = used_in(connection, settings, project_id, abilities)
    catalogue = _catalogue(connection, settings, project_id)
    views = [_view(connection, settings, project_id, a, dep=dep, state=state, tests=tests,
                   by_name=by_name, held=held[a["key"]],
                   recommended_for=recs.get(a["key"], []), uses=uses[a["key"]],
                   catalogue=catalogue) for a in abilities]
    return {"project_id": project_id,
            "ready": sum(1 for v in views if v["status"] == "ready"),
            "total": len(views), "essentials": defs["essentials"], "groups": defs["groups"],
            "abilities": views,
            # THE CREW the grants are about, so Who can and the drawer's Agents list the same
            # agents the grants were computed over.
            "crew": [{"role_key": key, "display_name": row["display_name"]}
                     for key, row in sorted(roles.items())]}


def ability_view(connection: Connection, settings: Any, project_id: str, key: str
                 ) -> dict[str, Any]:
    return kit_view(connection, settings, project_id, only=key)["abilities"][0]


# --------------------------------------------------------------------------- #
# Studio: what a step needs set up                                             #
# --------------------------------------------------------------------------- #

def ability_problems(connection: Connection, settings: Any, project_id: str,
                     spec: dict[str, Any]) -> list[dict[str, Any]]:
    """`{step_id, field: "ability", message}` for every step whose ability is not set up
    (status setup or needs_repo) — the Studio's Check and Start readiness (spec §3.3)."""
    steps = [s for s in (spec or {}).get("steps") or [] if isinstance(s, dict)]
    if not steps:
        return []
    defs = definitions(settings, media=media_installed(connection, project_id))
    by_key = {a["key"]: a for a in defs["abilities"]}
    wanted: dict[str, list[str]] = {}
    for step in steps:
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        key = CAPABILITY_ABILITY.get(str(binding.get("capability") or ""))
        if key and key in by_key:
            wanted.setdefault(key, []).append(str(step.get("id") or ""))
    if not wanted:
        return []
    from . import kit_connections

    dep = kit_connections.deps(connection, settings, project_id)
    state = _state(connection, project_id)
    tests = last_tests(connection, project_id)
    by_name = _servers_by_name(connection, project_id)
    held = holders(connection, settings, project_id, [by_key[k] for k in wanted])
    out: list[dict[str, Any]] = []
    for key, step_ids in wanted.items():
        status = _facts(connection, settings, project_id, by_key[key], dep=dep, state=state,
                        tests=tests, by_name=by_name, held=held[key])["status"]
        if status in ("setup", "needs_repo"):
            for step_id in step_ids:
                out.append({"step_id": step_id, "field": "ability", "ability": key,
                            "message": f"Needs {by_key[key]['label']} set up"})
    return out


# --------------------------------------------------------------------------- #
# Test crew                                                                    #
# --------------------------------------------------------------------------- #

def crew_rows(connection: Connection, settings: Any, project_id: str) -> list[dict[str, Any]]:
    """For each Essential: can at least one agent use it right now? Holders, the approval rule
    and reachability, read the way the runtime reads them. No model call."""
    view = kit_view(connection, settings, project_id)
    rows = []
    for ability in view["abilities"]:
        if not ability["essential"]:
            continue
        ok = ability["status"] == "ready" and bool(ability["holders"])
        if ability["status"] == "unheld" or not ability["holders"]:
            detail = "No one holds it"
        elif ability["status"] == "ready":
            asks = any(t["policy"] == "ask" for t in ability["tools"])
            detail = "Asks a person" if asks else "Runs without asking"
        else:
            detail = ability["status_tip"] or ability["status"]
        rows.append({"ability": ability["key"], "ok": ok, "holders": ability["holders"],
                     "detail": detail})
    return rows
