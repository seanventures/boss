"""Steps a customer defines, bound to their own skills or MCP tools.

The eighteen built-in capabilities cover how software gets delivered. This is for the work that
is specific to one company — file it in our tracker, tell the compliance system, run our own
scanner — and it is the difference between a workflow product and a workflow demo.

TWO BINDING KINDS, NOT THREE. A skill and an MCP tool are each independently callable, so each
can be a step. An integration connection is not: in BOSS a connection grants authority to a
skill (see `capability_resolutions`), it is never invoked on its own. Offering "bind an
integration" would have produced a step that publishes and then has nothing to run. To use an
integration, bind the skill that runs through it.

One further rule shapes the entire module:

    THE RISK TIER IS NEVER A FIELD THE CUSTOMER FILLS IN.

It is resolved from whatever the step is bound to, at validation and again at run time. A
self-declared tier would be the documented way around the approval floor: call your step
"harmless", bind it to a tool that drops the production database, and no gate ever fires. So
the tier is read from the skill's registered config, or from the MCP server's declared tier and
its per-tool override — never from a column a customer controls.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id
from .workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR

BINDING_KINDS = ("skill", "mcp_tool")

#: Who the runner says it is when it invokes a skill. It is a distinct caller on purpose: a
#: skill that only permits `agent_loop` should not silently become workflow-callable, and the
#: denial the customer gets names the thing to fix.
RUNNER_CALLER = "workflow_runner"

#: What a step is assumed to be when nothing says otherwise. The FLOOR, not zero: an
#: unclassified tool is one nobody has looked at, and treating it as safe is how an ungoverned
#: step ships.
UNKNOWN_RISK = MANUAL_APPROVAL_RISK_FLOOR

_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class CustomStepError(RuntimeError):
    """Something about the definition is wrong. The message is shown to the customer."""


def _key_from(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_") or "step"
    key = base
    for suffix in range(2, 500):
        if key not in taken:
            return key
        key = f"{base}_{suffix}"
    return f"{base}_x"


def _clean_inputs(raw: Any) -> list[dict[str, Any]]:
    """Normalise the input declarations, dropping anything unusable.

    A malformed input would otherwise reach the canvas as a field with no name, which the
    builder cannot render and the runner cannot fill.
    """
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            name = item.strip()
            if name:
                out.append({"name": name, "required": True, "description": ""})
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "required": bool(item.get("required", True)),
            "description": str(item.get("description") or ""),
        })
    return out


# ── Risk resolution ────────────────────────────────────────────────────────────

def resolve_risk(connection: Connection, project_id: str, row: Any) -> dict[str, Any]:
    """What this step is ACTUALLY allowed to do, read from the thing it binds.

    Returns the tier, where it came from, whether the binding is still usable, and whether its
    provenance is trusted — everything the approval floor and the builder need, from one place,
    so validation and execution cannot disagree about a step's risk.
    """
    kind = str(row["binding_kind"])
    unusable = {"risk_tier": UNKNOWN_RISK, "trusted": False, "usable": False}

    if kind == "skill":
        skill = connection.execute(
            text("SELECT enabled, config, provenance FROM project_skills "
                 "WHERE project_id=:p AND skill_id=:s"),
            {"p": project_id, "s": row["skill_id"]},
        ).mappings().first()
        if skill is None:
            return {**unusable, "source": "skill",
                    "reason": f"skill '{row['skill_id']}' is not registered in this project."}
        if not skill["enabled"]:
            return {**unusable, "source": "skill",
                    "reason": f"skill '{row['skill_id']}' is disabled."}
        config = skill["config"] if isinstance(skill["config"], dict) else {}
        provenance = skill["provenance"] if isinstance(skill["provenance"], dict) else {}
        declared = config.get("risk_tier")
        return {
            "risk_tier": int(declared) if declared is not None else UNKNOWN_RISK,
            "trusted": bool(provenance.get("trusted", False)),
            "usable": True,
            "source": f"skill {row['skill_id']}",
        }

    if kind == "mcp_tool":
        server = connection.execute(
            text("SELECT name, enabled, risk_tier, provenance, tool_risk_overrides "
                 "FROM mcp_servers WHERE id=:i AND project_id=:p"),
            {"i": row["mcp_server_id"], "p": project_id},
        ).mappings().first()
        if server is None:
            return {**unusable, "source": "mcp",
                    "reason": "the MCP server this step used has been removed."}
        if not server["enabled"]:
            return {**unusable, "source": "mcp",
                    "reason": f"MCP server '{server['name']}' is disabled."}
        overrides = (server["tool_risk_overrides"]
                     if isinstance(server["tool_risk_overrides"], dict) else {})
        provenance = server["provenance"] if isinstance(server["provenance"], dict) else {}
        # A per-tool override beats the server's blanket tier: one dangerous tool on an
        # otherwise ordinary server is exactly the case the override column exists for.
        tier = overrides.get(str(row["mcp_tool_name"]))
        if tier is None:
            # `server["risk_tier"] or UNKNOWN_RISK` would be wrong: tier 0 is falsy, so every
            # read-only server would be promoted to the approval floor.
            tier = server["risk_tier"] if server["risk_tier"] is not None else UNKNOWN_RISK
        return {
            "risk_tier": int(tier),
            "trusted": bool(provenance.get("trusted", False)),
            "usable": True,
            "source": f"{server['name']} · {row['mcp_tool_name']}",
        }

    raise CustomStepError(f"unknown binding kind '{kind}'.")


def approval_for(risk: dict[str, Any], requested: Optional[str]) -> str:
    """The approval mode a custom step will run under. Same floor as everything else."""
    if int(risk.get("risk_tier", UNKNOWN_RISK)) >= MANUAL_APPROVAL_RISK_FLOOR:
        return "manual"
    mode = (requested or "auto").strip().lower()
    if mode not in ("auto", "manual"):
        raise CustomStepError("approval mode must be 'auto' or 'manual'.")
    return mode


# ── CRUD ───────────────────────────────────────────────────────────────────────

def create(
    connection: Connection,
    *,
    project_id: str,
    label: str,
    binding_kind: str,
    actor: str,
    summary: str = "",
    skill_id: Optional[str] = None,
    mcp_server_id: Optional[str] = None,
    mcp_tool_name: Optional[str] = None,
    inputs: Any = None,
    produces: Any = None,
    knowledge: str = "",
    step_key: Optional[str] = None,
) -> dict[str, Any]:
    label = (label or "").strip()
    if not label:
        raise CustomStepError("a custom step needs a name.")
    if binding_kind not in BINDING_KINDS:
        raise CustomStepError(
            f"binding must be one of: {', '.join(BINDING_KINDS)}."
        )
    if binding_kind == "skill" and not skill_id:
        raise CustomStepError("choose the skill this step runs.")
    if binding_kind == "mcp_tool" and not (mcp_server_id and mcp_tool_name):
        raise CustomStepError("choose the MCP server and the tool this step calls.")

    taken = {
        row[0] for row in connection.execute(
            text("SELECT step_key FROM workflow_custom_steps WHERE project_id=:p"),
            {"p": project_id},
        )
    }
    key = (step_key or "").strip() or _key_from(label, taken)
    if not _KEY.match(key):
        raise CustomStepError(
            "a step key is lowercase letters, digits and underscores, starting with a letter."
        )
    if key in taken:
        raise CustomStepError(f"you already have a step called '{key}'.")

    import json as _json

    row = connection.execute(
        text(
            """
            INSERT INTO workflow_custom_steps (
                id, project_id, step_key, label, summary, binding_kind,
                skill_id, mcp_server_id, mcp_tool_name,
                inputs, produces, knowledge, created_by
            ) VALUES (
                :id, :p, :key, :label, :summary, :kind,
                :skill, :server, :tool,
                CAST(:inputs AS JSONB), CAST(:produces AS JSONB), :knowledge, :actor
            ) RETURNING *
            """
        ),
        {
            "id": make_id("wcs"), "p": project_id, "key": key, "label": label,
            "summary": (summary or "").strip() or None, "kind": binding_kind,
            "skill": skill_id, "server": mcp_server_id, "tool": mcp_tool_name,
            "inputs": _json.dumps(_clean_inputs(inputs)),
            "produces": _json.dumps([str(p) for p in (produces or []) if str(p).strip()]),
            # Trimmed, not truncated to a token count: this is prose an operator wrote, and
            # cutting it mid-sentence would be worse than asking them to be brief.
            "knowledge": (knowledge or "").strip()[:8000] or None,
            "actor": actor,
        },
    ).mappings().one()
    emit_event(
        connection, project_id, "workflow.custom_step_created", actor,
        "workflow_custom_step", row["id"],
        {"step_key": key, "binding_kind": binding_kind},
    )
    return serialize(connection, project_id, row)


def serialize(connection: Connection, project_id: str, row: Any) -> dict[str, Any]:
    """One custom step, with its risk resolved from what it binds.

    Risk travels with the row everywhere it is read, so a builder cannot show one number while
    the runner enforces another.
    """
    risk = resolve_risk(connection, project_id, row)
    return {
        "id": row["id"],
        "step_key": row["step_key"],
        "label": row["label"],
        "summary": row["summary"] or "",
        "binding_kind": row["binding_kind"],
        "skill_id": row["skill_id"],
        "mcp_server_id": row["mcp_server_id"],
        "mcp_tool_name": row["mcp_tool_name"],
        "inputs": row["inputs"] if isinstance(row["inputs"], list) else [],
        "produces": row["produces"] if isinstance(row["produces"], list) else [],
        "knowledge": row["knowledge"] if "knowledge" in row.keys() else None,
        "enabled": bool(row["enabled"]),
        # Resolved, never stored.
        "risk_tier": risk["risk_tier"],
        "risk_source": risk.get("source"),
        "trusted": risk["trusted"],
        "usable": risk["usable"],
        "unusable_reason": risk.get("reason"),
        "approval_forced_manual": risk["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR,
    }


def list_steps(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM workflow_custom_steps WHERE project_id=:p ORDER BY label"),
        {"p": project_id},
    ).mappings().all()
    return [serialize(connection, project_id, row) for row in rows]


def get(connection: Connection, project_id: str, step_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM workflow_custom_steps WHERE project_id=:p AND step_key=:k"),
        {"p": project_id, "k": step_key},
    ).mappings().first()
    return serialize(connection, project_id, row) if row else None


def delete(connection: Connection, project_id: str, step_key: str, actor: str) -> bool:
    result = connection.execute(
        text("DELETE FROM workflow_custom_steps WHERE project_id=:p AND step_key=:k"),
        {"p": project_id, "k": step_key},
    )
    if result.rowcount:
        emit_event(
            connection, project_id, "workflow.custom_step_deleted", actor,
            "workflow_custom_step", step_key, {"step_key": step_key},
        )
    return bool(result.rowcount)


# ── Validation, for the workflow spec ──────────────────────────────────────────

def validate_step(
    connection: Connection, project_id: str, step: dict[str, Any]
) -> list[str]:
    """Everything wrong with one custom step as used in a workflow."""
    problems: list[str] = []
    step_id = step.get("id", "?")
    binding = step.get("binding") or {}
    key = str(binding.get("custom_step") or "").strip()
    if not key:
        problems.append(f"step '{step_id}' does not say which custom step it runs.")
        return problems

    definition = get(connection, project_id, key)
    if definition is None:
        problems.append(
            f"step '{step_id}' uses custom step '{key}', which this project does not have."
        )
        return problems
    if not definition["enabled"]:
        problems.append(f"step '{step_id}' uses '{definition['label']}', which is turned off.")
    if not definition["usable"]:
        # Fail closed, and say WHY. "Custom step is unusable" sends somebody hunting.
        problems.append(
            f"step '{step_id}' uses '{definition['label']}', which cannot run: "
            f"{definition['unusable_reason']}"
        )

    inputs = binding.get("inputs") or {}
    for declared in definition["inputs"]:
        if declared.get("required") and not inputs.get(declared["name"]):
            problems.append(
                f"step '{step_id}' ({definition['label']}) needs '{declared['name']}'."
            )

    approval = (step.get("approval") or {}).get("mode")
    if definition["approval_forced_manual"] and str(approval).lower() == "auto":
        problems.append(
            f"step '{step_id}' ({definition['label']}) runs something at risk tier "
            f"{definition['risk_tier']} and always requires a person."
        )
    if definition["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR and not definition["trusted"]:
        problems.append(
            f"step '{step_id}' ({definition['label']}) binds an untrusted "
            f"{definition['binding_kind'].replace('_', ' ')} at risk tier "
            f"{definition['risk_tier']}. Only a reviewed one can be given that much reach."
        )
    return problems


# ── Execution ──────────────────────────────────────────────────────────────────

def execute(
    connection: Connection,
    settings: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    inputs: dict[str, Any],
    run_id: str,
    step_index: int = 0,
) -> dict[str, Any]:
    """Run one custom step and return what it produced.

    Neither branch calls a skill or an MCP server directly. Both go through
    SkillRuntimeService, which is where permission, budget and effect receipts live — the same
    path the chat agent uses. A second, ungoverned way to call a customer's tools is exactly
    what a workflow product must not grow.
    """
    binding = step.get("binding") or {}
    key = str(binding.get("custom_step") or "")
    row = connection.execute(
        text("SELECT * FROM workflow_custom_steps WHERE project_id=:p AND step_key=:k"),
        {"p": project_id, "k": key},
    ).mappings().first()
    if row is None:
        raise CustomStepError(f"this project has no custom step called '{key}'.")
    if not row["enabled"]:
        raise CustomStepError(f"'{row['label']}' is turned off.")

    # Re-resolved at run time, not trusted from publish time. A skill can be disabled or an MCP
    # server removed between publishing a workflow and running it, and the run is the moment
    # that matters.
    risk = resolve_risk(connection, project_id, row)
    if not risk["usable"]:
        raise CustomStepError(f"'{row['label']}' cannot run: {risk['reason']}")

    missing = [
        i["name"] for i in (row["inputs"] if isinstance(row["inputs"], list) else [])
        if i.get("required") and not inputs.get(i["name"])
    ]
    if missing:
        raise CustomStepError(
            f"'{row['label']}' needs {', '.join(missing)}, which the step did not supply."
        )

    # WHAT THE STEP KNOWS, handed to the thing that runs.
    #
    # A binding says WHICH skill or tool runs; it says nothing about the standard the step is
    # meant to hold to — the ticket format the team uses, the clauses that matter, the internal
    # doc explaining the system it touches. That lived in the head of whoever defined the step,
    # so the step called the tool correctly and got the house style wrong.
    #
    # Passed as an ordinary input under a reserved name rather than a new parameter, because
    # every skill and every MCP tool already takes inputs and none of them would take a new
    # positional argument. A step with no knowledge passes nothing, so nothing changes for it.
    knowledge = (row["knowledge"] if "knowledge" in row.keys() else None) or ""
    if knowledge.strip():
        inputs = {**inputs, "knowledge": knowledge.strip()}

    if row["binding_kind"] == "skill":
        from .agent_loop import _invoke_agent_skill

        result = _invoke_agent_skill(
            connection, settings, project_id, str(row["skill_id"]),
            RUNNER_CALLER, inputs, actor=actor,
            skill_spec=_spec_granting_the_runner(
                connection, settings, project_id, str(row["skill_id"])
            ),
        )
        if str(result.get("status")) == "denied":
            # The runtime denies rather than raises, so a denial would otherwise be recorded as
            # a successful step whose output happens to say "denied".
            raise CustomStepError(
                f"'{row['label']}' was denied: {result.get('reason') or 'not permitted'}"
            )
        if str(result.get("status")) == "failed":
            raise CustomStepError(
                f"'{row['label']}' failed: {result.get('reason') or 'the skill did not run'}"
            )
        # The tool's own output stays FLAT, because that is what a later step reads with
        # `{"from_step": …, "field": …}`. The step's own facts go under one reserved key rather
        # than beside them — `toolchain.command.guard` returns its own `risk_tier` (its verdict
        # on the command), and spreading the two together let a tool's output silently
        # overwrite the tier the step actually ran at, so the evidence said tier 0 for a step
        # that needed an approval at tier 2.
        return {
            **(result.get("result") or {}),
            "_step": {
                "custom_step": key, "label": row["label"], "binding": "skill",
                "skill_id": row["skill_id"], "risk_tier": risk["risk_tier"],
            },
        }

    from .agent_loop import _invoke_mcp_skill

    server = connection.execute(
        text("SELECT * FROM mcp_servers WHERE id=:i AND project_id=:p"),
        {"i": row["mcp_server_id"], "p": project_id},
    ).mappings().first()
    result = _invoke_mcp_skill(
        connection, settings, project_id,
        dict(server), {"name": row["mcp_tool_name"]}, inputs,
        run_id=run_id, step=step_index, actor=actor, caller=RUNNER_CALLER,
    )
    if str(result.get("status")) in ("denied", "failed"):
        raise CustomStepError(
            f"'{row['label']}' {result.get('status')}: "
            f"{result.get('reason') or 'the tool did not run'}"
        )
    output = (result.get("result") or {}).get("output")
    flat = output if isinstance(output, dict) else {
        "output": output if output is not None else (result.get("result") or {})
    }
    return {
        **flat,
        "_step": {
            "custom_step": key, "label": row["label"], "binding": "mcp_tool",
            "tool": f"{server['name']} · {row['mcp_tool_name']}",
            "risk_tier": risk["risk_tier"],
        },
    }


def _spec_granting_the_runner(
    connection: Connection, settings: Any, project_id: str, skill_id: str
) -> Optional[dict[str, Any]]:
    """The skill's real spec, with the workflow runner added to its allowed callers.

    DEFINING A CUSTOM STEP IS THE GRANT, and this is where that is cashed in. Skills declare
    which callers may invoke them, and none of them listed a runner that did not exist when
    they were written — so without this, every custom step bound to a skill is denied on its
    first run with "caller 'workflow_runner' not in allowed_callers", and the feature is a
    demo.

    This is not forging permission. The grant is:

      * EXPLICIT — somebody chose this skill for a step, by name, in this project.
      * SCOPED — only to skills a `workflow_custom_steps` row exists for. The caller of this
        function has already loaded that row.
      * RECORDED — `workflow.custom_step_created` names the actor who granted it.
      * REVOCABLE — delete the step and the grant goes with it.

    Everything else still applies unchanged: the risk floor, the approval gate, the budget
    reservation, the effect receipt. What is widened is WHO may call, never WHAT it may do.
    """
    try:
        from .entitlements import EntitlementBrokerService

        spec = dict(
            EntitlementBrokerService(settings).get_skill(
                skill_id, project_id, connection=connection
            )
        )
    except Exception:
        # No spec to widen — let the runtime resolve it and deny if it must. A step that runs
        # because its permissions could not be read is the wrong failure direction.
        return None

    permissions = dict(spec.get("permissions") or {})
    callers = list(permissions.get("allowed_callers") or [])
    if "*" in callers or RUNNER_CALLER in callers:
        return spec
    permissions["allowed_callers"] = [*callers, RUNNER_CALLER]
    spec["permissions"] = permissions
    return spec
