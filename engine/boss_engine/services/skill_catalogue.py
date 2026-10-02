"""THE skill catalogue: every skill a project can use, read from one place.

WHY THIS MODULE EXISTS
----------------------
A project's skills live in three stores, and for a long time every consumer read a different
subset of them:

* the built-in SkillSpecs in ``packages/specpack/skills/*.yaml``, which ship with BOSS;
* ``project_skills``, a per-project OVERRIDE of those (enabled, on-demand, a declared tier,
  and the odd imported SkillSpec);
* ``skill_definitions``, the skills an operator AUTHORED in this project.

``agent_steps.held_skills`` read the YAML only, so an agent given an authored skill was shown
holding it and never offered it as a tool. ``workflow_custom_steps.resolve_risk`` read
``project_skills`` only, so a custom step bound to an authored skill was "not registered in this
project" and every workflow placing it was refused at publish. The custom-step builder's
"Your skills" list read ``project_skills`` too, which on a real install (boss-1) held five BOSS
system skills and none of the operator's — while the skills agents actually held had no row
there at all. ``SkillRuntimeService.invoke`` loaded its spec from the YAML, so an authored skill
raised SkillNotFoundError the first time anything tried to run it. Creating a skill produced an
object every screen showed and nothing could execute.

So there is now one answer to "which skills does this project have, at what risk, and can each
one run": :func:`entries`. Every consumer reads it — held_skills, resolve_risk, the custom-step
builder's bindable list, the workflow ``kind: skill`` validator, the agent assignment picker and
the ``list_skills`` chat tool — and ``EntitlementBrokerService.list_skills`` (which
``SkillRuntimeService`` loads specs from) merges :func:`authored_specs` into the catalogue it
returns. ``engine/tests/test_one_skill_catalogue.py`` enumerates every SQL read of the two
skill tables in the codebase and fails on any reader that is not this module or a store's own
writer, so the next consumer cannot quietly grow its own subset again.

THE RISK RULE, STATED ONCE
--------------------------
A skill's tier is the HIGHEST tier anything it touches declares, and never something a caller
lowers:

* a built-in: the tier its SkillSpec declares, or the project's recorded tier if that is higher;
* an authored ``builtin`` wrapper: its own tier or the wrapped executor's, whichever is higher —
  otherwise wrapping ``github.create_pr_real`` (tier 3) as an authored tier-0 skill would be the
  documented way past every approval in the product;
* an authored ``mcp_tool``: its own tier or the MCP server's tier for that tool;
* a legacy ``project_skills`` row with no spec: its recorded tier, or the approval floor when it
  has none, because an unclassified skill is one nobody looked at.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR

#: What a skill with no declared tier is treated as. The floor, not zero — see the module doc.
UNKNOWN_RISK = MANUAL_APPROVAL_RISK_FLOOR

#: Where a catalogue entry came from. `registered` is a project_skills row that names a skill no
#: spec describes — older installs and imported registrations have them, and they keep working.
SOURCES = ("builtin", "authored", "registered")


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _tier(value: Any) -> Optional[int]:
    try:
        return max(0, min(int(value), 3)) if value is not None else None
    except (TypeError, ValueError):
        return None


def raised_tier(spec_tier: Any, project_row: Optional[dict[str, Any]]) -> int:
    """A built-in skill's tier: its SkillSpec's, or the project's recorded tier if HIGHER.

    The one statement of that half of the risk rule. EntitlementBrokerService.list_skills
    applies it to every spec it returns, so the runtime (SkillRuntimeService, held_skills,
    resolve_for_task) and the catalogue read the same number; before that only :func:`entries`
    applied it, and an agent was offered a skill at its raw spec tier while a custom step bound
    to the same skill needed approval at the raised one (round-1 review).
    """
    tier = _tier(spec_tier) or 0
    config = _loads((project_row or {}).get("config"), {}) or {}
    recorded = _tier(config.get("risk_tier")) if isinstance(config, dict) else None
    return tier if recorded is None else max(tier, recorded)


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from ..config import get_settings

    return get_settings()


# --------------------------------------------------------------------------- #
# Authored skills as SkillSpecs                                                  #
# --------------------------------------------------------------------------- #

def _declared_input_schema(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """The authored inputs in the SkillSpec shorthand agent_loop already turns into a tool."""
    schema: dict[str, Any] = {}
    for entry in inputs or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        schema[name if entry.get("required", True) else f"{name}?"] = "string"
    return schema


def _mcp_servers_by_id(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM mcp_servers WHERE project_id = :p"),
        {"p": project_id},
    ).mappings().all()
    return {str(r["id"]): dict(r) for r in rows}


def mcp_tool_tier(server: dict[str, Any], tool_name: str) -> int:
    """The tier an MCP server declares for one of its tools — mcp_registry.tool_tier, the one
    place that rule lives."""
    from .mcp_registry import tool_tier

    return tool_tier(server, tool_name)


def _cached_tool(server: dict[str, Any], tool_name: str) -> Optional[dict[str, Any]]:
    for tool in _loads(server.get("tools"), []) or []:
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            return tool
    return None


def _authored_facts(
    row: dict[str, Any],
    builtin_by_id: dict[str, dict[str, Any]],
    servers: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Tier, trust, usability and input schema for one authored skill — see the risk rule."""
    behaviour = str(row["behaviour_kind"])
    config = _loads(row["config"], {}) or {}
    inputs = _loads(row["inputs"], []) or []
    own_tier = _tier(row["risk_tier"])
    tier = UNKNOWN_RISK if own_tier is None else own_tier
    trusted = bool(row["trusted"])
    usable, reason = True, None
    input_schema: dict[str, Any] = _declared_input_schema(inputs)
    runs_through = ""

    if behaviour == "builtin":
        executor_id = str(config.get("executor_id") or "").strip()
        runs_through = executor_id
        wrapped = builtin_by_id.get(executor_id)
        if wrapped is not None:
            tier = max(tier, int(wrapped.get("risk_tier") or 0))
            if not input_schema:
                input_schema = dict(wrapped.get("input_schema") or {})
        else:
            from .skill_executors import registered_executor_ids

            if executor_id not in registered_executor_ids():
                usable = False
                reason = (f"it names executor {executor_id!r}, which BOSS does not ship. "
                          "Edit the skill and pick one of the built-in skills.")
    elif behaviour == "mcp_tool":
        server = servers.get(str(config.get("mcp_server_id") or ""))
        tool_name = str(config.get("tool_name") or "")
        if server is None:
            usable = False
            reason = ("the MCP server it calls has been removed. Edit the skill and choose a "
                      "server, or delete the skill.")
        else:
            runs_through = f"{server['name']} · {tool_name}"
            tier = max(tier, mcp_tool_tier(server, tool_name))
            # THE SERVER IS WHAT DOES THE WORK, so its review counts too. A reviewed skill on
            # an unreviewed server is an unreviewed integration with a reviewed label on it.
            provenance = _loads(server.get("provenance"), {}) or {}
            trusted = trusted and bool(provenance.get("trusted", False))
            if not server.get("enabled", True):
                usable = False
                reason = (f"MCP server '{server['name']}' is disabled. Enable it on the Skills "
                          "and MCP page, or point the skill at another server.")
            cached = _cached_tool(server, tool_name)
            if cached and isinstance(cached.get("inputSchema"), dict):
                # The tool's own schema is the truth about what it takes; the authored list
                # is a fallback for a server that has never been reached.
                input_schema = cached["inputSchema"]
    return {
        "risk_tier": tier, "trusted": trusted, "usable": usable, "reason": reason,
        "input_schema": input_schema, "runs_through": runs_through,
    }


def authored_specs(
    connection: Connection,
    project_id: str,
    builtin_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every skill this project authored, as a SkillSpec the runtime can load and govern.

    Called by EntitlementBrokerService.list_skills, which is what SkillRuntimeService,
    held_skills and the agent loop all load specs from — so an authored skill reaches every
    runtime by the same road a built-in does.

    `allowed_callers` is EMPTY on purpose: nobody reaches an authored skill by default. The
    routes in are the two explicit grants BOSS already has — giving it to an agent
    (role_skills) and placing it in a custom step (which grants the workflow runner).
    """
    rows = connection.execute(
        text("SELECT * FROM skill_definitions WHERE project_id = :p ORDER BY skill_key"),
        {"p": project_id},
    ).mappings().all()
    if not rows:
        return []
    servers = _mcp_servers_by_id(connection, project_id)
    specs: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        facts = _authored_facts(row, builtin_by_id, servers)
        produces = _loads(row["produces"], []) or []
        summary = str(row["summary"] or "").strip() or str(row["label"])
        specs.append({
            "id": str(row["skill_key"]),
            "kind": f"authored.{row['behaviour_kind']}",
            "system": False,
            "load": "eager",
            "risk_tier": facts["risk_tier"],
            "description": summary,
            "mock_mode": False,
            "input_schema": facts["input_schema"] or {"type": "object", "properties": {}},
            "output_schema": {str(p.get("name")): "string" for p in produces
                              if isinstance(p, dict) and p.get("name")} or {"result": "object"},
            "cost": {"estimator": "fixed", "category": "skill"},
            "retry": {"max_attempts": 1, "backoff": "none"},
            "permissions": {"allowed_callers": [], "required_permissions": ["workflow_execute"]},
            "data_boundary": "internal",
            "policy": {
                "authored": True,
                "behaviour_kind": str(row["behaviour_kind"]),
                "requires_approval": facts["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR,
                "usable": facts["usable"],
                # The operator's own switch, kept apart from `enabled` below so a screen can
                # say whether a skill is off because somebody turned it off or because what it
                # runs through went away.
                "switched_on": bool(row["enabled"]),
                **({"unusable_reason": facts["reason"]} if facts["reason"] else {}),
            },
            "source": "authored",
            # Enabled means CAN BE OFFERED. A skill whose server was removed is off for the
            # runtime even though the operator never switched it off, because a tool the model
            # can call and nothing can execute wastes a whole turn finding that out.
            "enabled": bool(row["enabled"]) and facts["usable"],
            "on_demand": False,
            "label": str(row["label"]),
            "knowledge": str(row["knowledge"] or ""),
            "runs_through": facts["runs_through"],
            "publisher": {"id": str(row["created_by"]), "name": str(row["created_by"])},
            "provenance": {"source": "authored", "trusted": facts["trusted"]},
        })
    return specs


# --------------------------------------------------------------------------- #
# The catalogue                                                                 #
# --------------------------------------------------------------------------- #

def entries(
    connection: Connection,
    project_id: str,
    *,
    settings: Any = None,
) -> dict[str, dict[str, Any]]:
    """Every skill this project has, keyed by id, with the facts every consumer needs.

    Each entry: skill_id, source (builtin | authored | registered), label, summary, kind,
    behaviour_kind, risk_tier, risk_declared, trusted, enabled, system, usable, reason, and
    `spec` — the SkillSpec the runtime will load, or None for a registered row no spec describes.
    """
    from .entitlements import EntitlementBrokerService

    catalogue = EntitlementBrokerService(_settings(settings)).list_skills(
        project_id, connection=connection)
    overrides = {
        str(r["skill_id"]): dict(r)
        for r in connection.execute(
            text("SELECT skill_id, enabled, config, provenance FROM project_skills "
                 "WHERE project_id = :p"),
            {"p": project_id},
        ).mappings().all()
    }

    out: dict[str, dict[str, Any]] = {}
    for spec in catalogue:
        skill_id = str(spec["id"])
        policy = spec.get("policy") or {}
        authored = spec.get("source") == "authored"
        tier = int(spec.get("risk_tier") or 0)
        if not authored:
            # The project's recorded tier counts only when it is HIGHER. A row copied from an
            # older spec must not lower a tier the spec has since raised. list_skills has
            # already folded this into the spec (raised_tier); applied again so an entry can
            # never be lower than the spec the runtime will load.
            tier = raised_tier(tier, overrides.get(skill_id))
        usable = bool(policy.get("usable", True)) if authored else True
        out[skill_id] = {
            "skill_id": skill_id,
            "source": "authored" if authored else "builtin",
            "label": str(spec.get("label") or skill_id),
            "summary": str(spec.get("description") or ""),
            "kind": str(spec.get("kind") or ""),
            "behaviour_kind": str(policy.get("behaviour_kind") or "builtin")
                              if authored else "builtin",
            "runs_through": str(spec.get("runs_through") or ""),
            "risk_tier": tier,
            "risk_declared": True,
            "trusted": bool((spec.get("provenance") or {}).get("trusted", False)),
            # An authored skill's spec folds "usable" into enabled for the runtime; the
            # catalogue keeps the two apart so a screen can say WHICH one is the problem.
            "enabled": bool(policy.get("switched_on", True)) if authored
                       else bool(spec.get("system") or spec.get("enabled", True)),
            "system": bool(spec.get("system", False)),
            "usable": usable,
            "reason": policy.get("unusable_reason"),
            "spec": spec,
        }

    # A REGISTERED ROW NO SPEC DESCRIBES. Kept, with its recorded tier, because custom steps
    # on older installs bind to exactly these — and dropping them would turn a working step
    # into "not registered" with nothing changed but the reader.
    for skill_id, row in overrides.items():
        if skill_id in out:
            continue
        config = _loads(row.get("config"), {}) or {}
        provenance = _loads(row.get("provenance"), {}) or {}
        declared = _tier(config.get("risk_tier"))
        out[skill_id] = {
            "skill_id": skill_id,
            "source": "registered",
            "label": skill_id,
            "summary": "",
            "kind": "registered",
            "behaviour_kind": "builtin",
            "runs_through": "",
            "risk_tier": UNKNOWN_RISK if declared is None else declared,
            "risk_declared": declared is not None,
            "trusted": bool(provenance.get("trusted", False)),
            "enabled": bool(row.get("enabled")),
            "system": False,
            "usable": True,
            "reason": None,
            "spec": None,
        }
    return out


def entry(
    connection: Connection,
    project_id: str,
    skill_id: str,
    *,
    settings: Any = None,
) -> Optional[dict[str, Any]]:
    """One skill's catalogue entry, or None when the project has no such skill."""
    return entries(connection, project_id, settings=settings).get(str(skill_id or "").strip())


def risk_facts(item: Optional[dict[str, Any]], skill_id: str) -> dict[str, Any]:
    """What a step bound to this skill may do: the shape resolve_risk and the publish-time
    validator both read, so a builder and a runner cannot disagree about a step's risk."""
    if item is None:
        return {"risk_tier": UNKNOWN_RISK, "trusted": False, "usable": False,
                "enabled": False, "source": "skill",
                "reason": (f"skill '{skill_id}' is not in this project's catalogue — it was "
                           "deleted, or the pack it ships in is not installed here. Author it "
                           "on the Skills and MCP page, or bind the step to another skill.")}
    if not item["enabled"]:
        return {"risk_tier": item["risk_tier"], "trusted": item["trusted"], "usable": False,
                "enabled": False, "source": f"skill {skill_id}",
                "reason": (f"skill '{skill_id}' is disabled. Turn it on on the Skills and MCP "
                           "page, or bind the step to another skill.")}
    if not item["usable"]:
        return {"risk_tier": item["risk_tier"], "trusted": item["trusted"], "usable": False,
                "enabled": True, "source": f"skill {skill_id}",
                "reason": f"skill '{skill_id}' cannot run: {item['reason']}"}
    return {"risk_tier": item["risk_tier"], "trusted": item["trusted"], "usable": True,
            "enabled": True, "source": f"skill {skill_id}"}


def bindable(connection: Connection, project_id: str, *, settings: Any = None) -> list[dict]:
    """Every skill a custom step could be bound to, with the tier the floor will use.

    All three sources, enabled or not: a disabled skill is listed (marked) because it is the one
    the operator is about to turn on, and leaving it out reads as BOSS having lost it.
    """
    items = []
    for item in entries(connection, project_id, settings=settings).values():
        items.append({
            "skill_id": item["skill_id"],
            "label": item["label"],
            "summary": item["summary"],
            "source": item["source"],
            "behaviour_kind": item["behaviour_kind"],
            "runs_through": item["runs_through"],
            "risk_tier": item["risk_tier"],
            "trusted": item["trusted"],
            "enabled": item["enabled"],
            "usable": item["usable"] and item["enabled"],
            "reason": item["reason"] if item["enabled"] else "turned off",
            # Said here rather than left for the operator to infer from a number.
            "unclassified": not item["risk_declared"],
            "requires_approval": item["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR,
        })
    return sorted(items, key=lambda s: (s["source"] != "authored", s["skill_id"]))
