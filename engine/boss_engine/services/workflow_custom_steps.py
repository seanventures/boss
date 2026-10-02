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
from .workflow_capabilities import (
    MANUAL_APPROVAL_RISK_FLOOR,
    TIMEOUT_ACTIONS,
    TIMEOUT_APPROVE_RISK_CEILING,
)

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

    ``requires_person`` is the sentence saying why a person must approve the step whatever its
    tier, or None. Today that is one rule — an MCP tool on an unreviewed server
    (mcp_registry.needs_person), reached directly or through an authored mcp_tool skill — and
    approval_for turns it into a manual step. Round-2 review: a tier-0 step bound to an
    unreviewed server's tool ran unattended, because only the tier was asked.
    """
    kind = str(row["binding_kind"])
    unusable = {"risk_tier": UNKNOWN_RISK, "trusted": False, "usable": False,
                "requires_person": None}

    if kind == "skill":
        # THE ONE CATALOGUE. This read `project_skills` alone, so a step bound to a skill the
        # operator AUTHORED was "not registered in this project" and every workflow placing it
        # was refused at publish — while the same skill showed as placeable on the Skills page.
        # Built-in, authored and registered skills, with the risk rule, now come from
        # skill_catalogue, the same place every other consumer reads.
        from .skill_catalogue import entry, risk_facts

        facts = risk_facts(entry(connection, project_id, str(row["skill_id"] or "")),
                           str(row["skill_id"]))
        if not facts["usable"]:
            return {**unusable, "source": facts["source"], "reason": facts["reason"]}
        return {**{key: facts[key] for key in ("risk_tier", "trusted", "usable", "source")},
                "requires_person": facts.get("requires_person")}

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
        from .mcp_registry import needs_person, reviewed, unreviewed_sentence

        server_view = {**dict(server), "provenance": server["provenance"]
                       if isinstance(server["provenance"], dict) else {}}
        # A per-tool override beats the server's blanket tier: one dangerous tool on an
        # otherwise ordinary server is exactly the case the override column exists for. The
        # rule lives in skill_catalogue so an authored mcp_tool skill and a step bound straight
        # to the tool cannot be tiered differently.
        from .skill_catalogue import mcp_tool_tier

        tier = mcp_tool_tier(dict(server), str(row["mcp_tool_name"]))
        return {
            "risk_tier": int(tier),
            "trusted": reviewed(server_view),
            "usable": True,
            "source": f"{server['name']} · {row['mcp_tool_name']}",
            "requires_person": (unreviewed_sentence(server_view)
                                if needs_person(server_view) else None),
        }

    raise CustomStepError(f"unknown binding kind '{kind}'.")


def approval_for(risk: dict[str, Any], requested: Optional[str]) -> str:
    """The approval mode a custom step will run under. Same floor as everything else, and a
    step resolve_risk says needs a person (an unreviewed MCP server) is manual at any tier."""
    if int(risk.get("risk_tier", UNKNOWN_RISK)) >= MANUAL_APPROVAL_RISK_FLOOR:
        return "manual"
    if risk.get("requires_person"):
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
        "approval_forced_manual": (risk["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR
                                   or bool(risk.get("requires_person"))),
        # Why a person is asked whatever the tier, or None — shown by the builder and the
        # publish refusal, so "requires a person" at tier 0 is never a mystery.
        "requires_person": risk.get("requires_person"),
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


def update(
    connection: Connection,
    *,
    project_id: str,
    step_key: str,
    actor: str,
    label: Optional[str] = None,
    summary: Optional[str] = None,
    binding_kind: Optional[str] = None,
    skill_id: Optional[str] = None,
    mcp_server_id: Optional[str] = None,
    mcp_tool_name: Optional[str] = None,
    inputs: Any = None,
    produces: Any = None,
    knowledge: Optional[str] = None,
) -> dict[str, Any]:
    """Change a step that already exists.

    WHY THIS WAS MISSING AND WHY THAT MATTERED. A step could be defined and removed and never
    corrected, so fixing a typo in a summary — or pointing a step at a different skill — meant
    deleting it and making a new one. That is not the same operation: the new step gets a NEW
    KEY, and every workflow referencing the old one silently loses that step. Destroying a
    published workflow is a steep price for a wording change.

    THE KEY NEVER CHANGES. It is what workflow specs bind to, so renaming the label leaves the
    key alone on purpose — a step called "Publish to YouTube" whose key is `publish_to_youtube`
    can become "Publish the short" without breaking a single workflow.

    THE RISK TIER IS STILL NOT ACCEPTED FROM THE CALLER. It is re-resolved from whatever the
    step now binds, exactly as on create, which is the property that stops "edit the step" being
    a way around the approval floor: repointing a step at a tier-3 skill makes it tier 3, and
    every workflow using it starts requiring a person.
    """
    row = connection.execute(
        text("SELECT * FROM workflow_custom_steps WHERE project_id=:p AND step_key=:k"),
        {"p": project_id, "k": step_key},
    ).mappings().first()
    if row is None:
        raise CustomStepError(f"no step called '{step_key}'.")

    kind = (binding_kind or row["binding_kind"]).strip()
    if kind not in BINDING_KINDS:
        raise CustomStepError(f"binding must be one of: {', '.join(BINDING_KINDS)}.")

    # What the step will bind AFTER this change, so the checks below judge the result rather
    # than the arguments — a step switched from mcp_tool to skill must be required to name a
    # skill even though `skill_id` was the only field sent.
    new_skill = skill_id if skill_id is not None else row["skill_id"]
    new_server = mcp_server_id if mcp_server_id is not None else row["mcp_server_id"]
    new_tool = mcp_tool_name if mcp_tool_name is not None else row["mcp_tool_name"]
    if kind == "skill" and not new_skill:
        raise CustomStepError("choose the skill this step runs.")
    if kind == "mcp_tool" and not (new_server and new_tool):
        raise CustomStepError("choose the MCP server and the tool this step calls.")
    if kind == "skill":
        # The other binding's fields are cleared rather than left behind: a row carrying both a
        # skill and an MCP tool is a row where the next reader has to guess which one runs.
        new_server = new_tool = None
    else:
        new_skill = None

    new_label = (label if label is not None else row["label"]).strip()
    if not new_label:
        raise CustomStepError("a custom step needs a name.")

    import json as _json

    updated = connection.execute(
        text(
            """
            UPDATE workflow_custom_steps SET
                label = :label, summary = :summary, binding_kind = :kind,
                skill_id = :skill, mcp_server_id = :server, mcp_tool_name = :tool,
                inputs = CAST(:inputs AS JSONB), produces = CAST(:produces AS JSONB),
                knowledge = :knowledge
            WHERE project_id = :p AND step_key = :k
            RETURNING *
            """
        ),
        {
            "label": new_label,
            "summary": ((summary if summary is not None else row["summary"]) or "").strip()
                       or None,
            "kind": kind, "skill": new_skill, "server": new_server, "tool": new_tool,
            "inputs": _json.dumps(
                _clean_inputs(inputs if inputs is not None else row["inputs"])),
            "produces": _json.dumps([
                str(x) for x in (produces if produces is not None else row["produces"]) or []
                if str(x).strip()]),
            "knowledge": ((knowledge if knowledge is not None else row["knowledge"])
                          or "").strip()[:8000] or None,
            "p": project_id, "k": step_key,
        },
    ).mappings().one()

    before, after = resolve_risk(connection, project_id, row), \
        resolve_risk(connection, project_id, updated)
    emit_event(
        connection, project_id, "workflow.custom_step_updated", actor,
        "workflow_custom_step", updated["id"],
        {
            "step_key": step_key, "binding_kind": kind,
            # RECORDED WHEN IT MOVES. A step that was tier 1 and is now tier 3 changes the
            # approval behaviour of every published workflow using it, without any of those
            # workflows being touched — so the ledger is the only place that connection can be
            # made afterwards.
            "risk_tier_before": before["risk_tier"], "risk_tier_after": after["risk_tier"],
        },
    )
    return serialize(connection, project_id, updated)


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

    approval = step.get("approval") or {}
    requested = approval.get("mode")
    if definition["approval_forced_manual"] and str(requested).lower() == "auto":
        problems.append(
            f"step '{step_id}' ({definition['label']}) runs something at risk tier "
            f"{definition['risk_tier']} and always requires a person."
            if not definition.get("requires_person") or (
                definition["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR)
            else f"step '{step_id}' ({definition['label']}) requires a person: "
                 f"{definition['requires_person']} Set its approval to manual, or review the "
                 "server first."
        )
    on_timeout = approval.get("on_timeout")
    if on_timeout is not None:
        action = str(on_timeout).lower()
        if action not in TIMEOUT_ACTIONS:
            problems.append(
                f"step '{step_id}' on_timeout must be one of: {', '.join(TIMEOUT_ACTIONS)}."
            )
        elif action == "approve" and definition.get("requires_person"):
            # A timeout that approves is nobody approving — exactly what an unreviewed
            # server's tool may not have.
            problems.append(
                f"step '{step_id}' ({definition['label']}) cannot approve itself on a timeout: "
                f"{definition['requires_person']} Use 'escalate' or 'block'."
            )
        elif action == "approve" and definition["risk_tier"] >= TIMEOUT_APPROVE_RISK_CEILING:
            problems.append(
                f"step '{step_id}' ({definition['label']}) is risk tier "
                f"{definition['risk_tier']} and cannot approve itself on a timeout. "
                "Use 'escalate' or 'block'."
            )
    if definition["risk_tier"] >= MANUAL_APPROVAL_RISK_FLOOR and not definition["trusted"]:
        problems.append(
            f"step '{step_id}' ({definition['label']}) binds an untrusted "
            f"{definition['binding_kind'].replace('_', ' ')} at risk tier "
            f"{definition['risk_tier']}. Only a reviewed one can be given that much reach."
        )

    # CAN THE AGENT NAMED ACTUALLY RUN THIS? Asked HERE rather than discovered mid-run.
    #
    # The captain is the permission identity, so assigning `video_editor` to a publish step is
    # refused — correctly — by the runtime. Refused twenty minutes into a workflow, as "caller
    # 'video_editor' not in allowed_callers", which is the right rule delivered at the worst
    # possible moment: the operator has waited for a render before learning the step could
    # never have run. Both facts are known when the workflow is saved.
    captain = str(step.get("captain") or "").strip().lower()
    # Only for an agent this project HAS. Telling the operator to "give engineering_captain that
    # skill on the Agents page" is impossible advice when there is no such agent to open — and
    # `step_agents.assignment_problems` already says that, with the agents there are.
    if captain and not _is_project_agent(connection, project_id, captain):
        return problems
    if captain and definition["binding_kind"] == "skill" and definition["skill_id"]:
        skill_id = str(definition["skill_id"])
        callers = _allowed_callers(connection, project_id, skill_id)
        # TWO WAYS TO BE ALLOWED, and this must know both or it refuses workflows that would
        # run. A skill lists the callers that existed when it was written; GIVING THE SKILL TO
        # THE AGENT is the other grant, and the one the product is built on — see
        # skill_runtime, which checks role_skills when allowed_callers does not match.
        #
        # Checking only the first is how "assign an agent to a step" became a thing you could
        # be refused for doing correctly.
        if callers and captain not in callers and not _agent_holds(
                connection, project_id, captain, skill_id):
            problems.append(
                f"step '{step_id}' gives '{definition['label']}' to {captain}, which is not "
                f"allowed to run {skill_id}. Either give {captain} that skill on the Agents "
                f"page, or use one of: {', '.join(sorted(callers))}."
            )
    return problems


def _is_project_agent(connection: Connection, project_id: str, role_key: str) -> bool:
    """Whether this project has an agent by that key. Fails OPEN: the caller then gives its
    usual advice, which is right for every agent that does exist."""
    try:
        return connection.execute(
            text("SELECT 1 FROM roles WHERE project_id=:p AND role_key=:r LIMIT 1"),
            {"p": project_id, "r": role_key},
        ).first() is not None
    except Exception:  # noqa: BLE001
        return True


def _agent_holds(connection: Connection, project_id: str, role_key: str, skill_id: str) -> bool:
    """Whether this agent has been given this skill.

    Fails CLOSED on its own error, unlike the allowed_callers lookup above: an empty
    allowed_callers means "cannot judge", but a failed role_skills read must not invent a grant
    that does not exist.
    """
    try:
        return connection.execute(
            text("SELECT 1 FROM role_skills WHERE project_id=:p AND role_key=:r "
                 "AND skill_id=:s LIMIT 1"),
            {"p": project_id, "r": role_key, "s": skill_id},
        ).first() is not None
    except Exception:  # noqa: BLE001
        return False


def _allowed_callers(connection: Connection, project_id: str, skill_id: str) -> set[str]:
    """Who the skill itself says may invoke it, or an empty set if that cannot be read.

    EMPTY MEANS "DO NOT JUDGE". A skill whose spec cannot be loaded here must not make every
    workflow using it unpublishable — the runtime performs the real check either way, and a
    validator that fails closed on its own lookup error blocks correct work.
    """
    try:
        from ..config import Settings
        from .entitlements import EntitlementBrokerService

        # A REAL Settings, not None. Passing None constructed a BudgetGovernor(None), which
        # threw in the constructor — and because this fails open, the exception was swallowed
        # and the check silently never ran. A guard that cannot fail loudly has to be exercised
        # against the thing it guards, which is how this was found: by saving the very spec it
        # was written to refuse and watching it succeed.
        spec = EntitlementBrokerService(Settings()).get_skill(
            skill_id, project_id, connection=connection) or {}
        permissions = spec.get("permissions") or {}
        return {str(c).strip().lower() for c in (permissions.get("allowed_callers") or [])}
    except Exception:  # noqa: BLE001
        return set()


def optional_inputs(connection: Connection, project_id: str, step_key: str) -> frozenset[str]:
    """The input names this step does NOT require.

    A step declares `inputs: [{name, required}]`. The runner resolves bindings with everything
    mandatory unless told otherwise, so an input marked optional still failed the run when it
    was left blank — the form said "if you have one" and the runtime disagreed.
    """
    try:
        row = connection.execute(
            text("SELECT inputs FROM workflow_custom_steps WHERE project_id=:p AND step_key=:k"),
            {"p": project_id, "k": step_key},
        ).scalar()
    except Exception:  # noqa: BLE001 — a lookup problem must not make everything mandatory
        return frozenset()
    declared = row if isinstance(row, list) else []
    return frozenset(
        str(i.get("name")) for i in declared
        if isinstance(i, dict) and i.get("name") and not i.get("required"))


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
    approved_by: Optional[str] = None,
    ran_as: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run one custom step and return what it produced.

    ``approved_by`` is the APPROVED gate decision this step was released by, when it had one.
    It travels to the MCP effect, which refuses an unreviewed server's tool without it
    (mcp_registry.call_tool_with_receipt) — so a step that reached here without its gate fails
    with the sentence rather than running unattended.

    Neither branch calls a skill or an MCP server directly. Both go through
    SkillRuntimeService, which is where permission, budget and effect receipts live — the same
    path the chat agent uses. A second, ungoverned way to call a customer's tools is exactly
    what a workflow product must not grow.

    WHO RUNS IT is `ran_as`, decided by `step_agents.resolve` — the agent the step names, or
    the agent that holds the skill, or BOSS's pick among several holders. That agent is both
    the permission identity and the actor on the record.

    NOTHING IS WIDENED. This used to run a step that named nobody as `workflow_runner` and add
    that caller to the skill's allowed_callers on the fly — "defining the step IS the grant".
    That made every skill callable by anyone who could draw a step, and it is the opposite of
    the rule the rest of BOSS runs on: an agent HOLDS a skill (role_skills), and that grant is
    what the permission gate cashes. The runner now calls as `workflow_runner` only where the
    binding's own policy already lists it and it reaches no MCP server, and is otherwise refused
    with "give <skill> to an agent" — except in a version published before that rule, which
    keeps the grant it was published with (`_spec_granting_the_runner`, `legacy_workflow_runner`).
    An MCP tool is never run as the workflow in a current version: its server is granted to an
    agent, never to a step (review of milestone/integration; agents.SERVERLESS_MCP_GRANTS).
    """
    if ran_as is None:
        from .step_agents import StepAgentError, resolve

        try:
            ran_as = resolve(connection, settings, project_id, step, actor=actor)
        except StepAgentError as error:
            raise CustomStepError(str(error)) from error
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

    # The agent that runs it, and the identity every permission check sees. The workflow
    # itself only where the binding's own policy names it — see the docstring.
    caller = str(ran_as.get("agent") or RUNNER_CALLER)
    on_record = actor if caller == RUNNER_CALLER else caller
    # THE ONE ROAD on which the workflow may reach an MCP server nobody holds: a version
    # published under the old rules (agents.SERVERLESS_MCP_GRANTS). Anything else — including a
    # current version whose step nobody holds — is gated on the caller holding the server.
    legacy = ran_as.get("how") == "legacy_workflow_runner"
    # "" rather than None, so agent_loop's own default never applies to a step, even one whose
    # agent happens to be called `agent_loop`.
    mcp_reach = "legacy_workflow_runner" if legacy else ""

    # What the gate decision has to be about: this run, this step. The MCP effect checks it
    # (mcp_registry.approval_stands), so another step's approval cannot release this one.
    approval_about = ({"workflow_run_id": run_id, "step_id": str(step.get("id") or "")}
                      if approved_by else None)

    if row["binding_kind"] == "skill":
        from .agent_loop import _invoke_agent_skill

        result = _invoke_agent_skill(
            connection, settings, project_id, str(row["skill_id"]),
            caller, inputs, actor=on_record,
            # The skill's own spec, unwidened. The agent is allowed because it HOLDS the skill
            # or the skill names it — never because it was put on a step. The one exception is a
            # version published under the old rules, which keeps the grant it was published
            # with; `step_agents.resolve` says so on the run (`legacy_workflow_runner`).
            skill_spec=(_spec_granting_the_runner(connection, settings, project_id,
                                                  str(row["skill_id"]))
                        if legacy else None),
            approved_by=approved_by, approval_about=approval_about,
            # The legacy road is named only where it matters — a skill that calls an MCP
            # server; every other skill's legacy grant is its widened spec, as before.
            **({"mcp_reach": mcp_reach}
               if legacy and _skill_server(connection, project_id, str(row["skill_id"]))
               else {}),
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
            "ran_as": {**ran_as, "skills_offered": [str(row["skill_id"])],
                       "skills_used": [str(row["skill_id"])]},
        }

    from .agent_loop import _invoke_mcp_skill

    server = connection.execute(
        text("SELECT * FROM mcp_servers WHERE id=:i AND project_id=:p"),
        {"i": row["mcp_server_id"], "p": project_id},
    ).mappings().first()
    result = _invoke_mcp_skill(
        connection, settings, project_id,
        dict(server), {"name": row["mcp_tool_name"]}, inputs,
        run_id=run_id, step=step_index, actor=on_record,
        caller=caller, approved_by=approved_by,
        approval_about=approval_about, mcp_reach=mcp_reach,
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
        "ran_as": {**ran_as, "skills_offered": [], "skills_used": [],
                   "mcp_offered": [f"{server['name']} · {row['mcp_tool_name']}"]},
    }


def _skill_server(connection: Connection, project_id: str, skill_id: str) -> str:
    from .step_agents import _skill_server as read

    return read(connection, project_id, skill_id)


def _spec_granting_the_runner(
    connection: Connection, settings: Any, project_id: str, skill_id: str
) -> Optional[dict[str, Any]]:
    """The skill's real spec, with the workflow runner added to its allowed callers.

    ONLY FOR A VERSION PUBLISHED UNDER THE OLD RULES. Until step agents were settled by
    `step_agents`, defining a custom step was itself the grant: a step naming nobody ran as
    `workflow_runner`, and this widened the skill's callers so it could. That was removed for new
    versions — a step is not a way to grant anything — but workflows published under it were
    working, and refusing them on upgrade killed runs mid-way. So it is kept for exactly those,
    and the run says "give <skill> to an agent and publish again" every time it is used.

    What is widened is WHO may call, never WHAT it may do: the risk floor, the approval gate,
    the budget reservation and the effect receipt all still apply.
    """
    try:
        from .entitlements import EntitlementBrokerService

        spec = dict(
            EntitlementBrokerService(settings).get_skill(
                skill_id, project_id, connection=connection
            )
        )
    except Exception:  # noqa: BLE001
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
