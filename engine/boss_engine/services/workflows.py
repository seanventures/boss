"""Reusable, mapped, durable workflow runtime.

The runtime deliberately owns orchestration only.  Agent groups, skills,
integrations, decisions, and subworkflows remain separate capabilities and are
referenced through pinned bindings.  All control-flow evaluation is
declarative; no workflow field is evaluated as Python or shell code.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, json_hash, make_id, now_utc
from .workflow_custom_steps import validate_step as defined_step_problems
from .workflow_capabilities import (
    approval_mode_for as effective_approval_mode,
    validate_custom_step as custom_step_problems,
    effective_approval as effective_approval_policy,
    estimate_minutes as capability_minutes,
    project_requirements as capability_project_requirements,
    validate_step as capability_problems,
    validate_captain,
)

logger = logging.getLogger(__name__)


WORKFLOW_KINDS = {
    "task",
    # A PRODUCT CAPABILITY: research, meeting, build, deploy, monitor.
    #
    # Added, never substituted. A step could already call a skill, an integration or an agent
    # group, and none of those said "build it" — so the five things a delivery workflow is made
    # of were exactly the five things a step could not be. Every existing kind behaves as it
    # always did; see services/workflow_capabilities.py for what a capability declares.
    "capability",
    "skill",
    "integration",
    "agent_group",
    "decision",
    "timer",
    "subworkflow",
    "condition",
    "parallel",
    "join",
    "bounded_loop",
}
CONTROL_KINDS = {
    "condition",
    "parallel",
    "join",
    "bounded_loop",
    "decision",
    "timer",
    "subworkflow",
}
#: Whose workflow a definition is. See the column comment in db/schema.sql.
NAMESPACES = {"system", "product"}
SYSTEM_NAMESPACE = "system"
PRODUCT_NAMESPACE = "product"

TERMINAL_RUN_STATES = {
    "completed",
    "failed",
    "deadlettered",
    "cancelled",
    "compensated",
}
TERMINAL_STEP_STATES = {"succeeded", "skipped", "failed", "deadlettered", "cancelled"}
ACTIVE_STEP_STATES = {"ready", "leased", "running", "waiting"}
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class WorkflowError(RuntimeError):
    pass


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowConflictError(WorkflowError):
    pass


class WorkflowValidationError(WorkflowError):
    pass


class WorkflowMappingNotFoundError(WorkflowError):
    pass


class WorkflowMappingAmbiguousError(WorkflowError):
    pass


class WorkflowLeaseError(WorkflowError):
    pass


class WorkflowNamespaceError(WorkflowError):
    """A caller reached across the system/product line.

    Raised rather than returning "not found" because the two failures need different answers: a
    missing workflow is a typo, and this is a customer surface touching BOSS's own machinery,
    which somebody needs to see in the logs.
    """


class WorkflowBindingBlockedError(WorkflowError):
    pass


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _clean_text(value: Any, name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise WorkflowValidationError(f"{name} is required.")
    return cleaned


def _step_map(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(step["id"]): step for step in spec.get("steps", [])}


def _targets(step: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    next_value = step.get("next", [])
    if isinstance(next_value, str):
        targets.append(next_value)
    elif isinstance(next_value, list):
        targets.extend(str(item) for item in next_value)
    kind = step.get("kind")
    if kind == "condition":
        targets.extend([str(step.get("on_true") or ""), str(step.get("on_false") or "")])
    elif kind == "parallel":
        targets.extend(str(item) for item in step.get("branches", []))
        if step.get("join"):
            targets.append(str(step["join"]))
    elif kind == "bounded_loop" and step.get("body"):
        targets.append(str(step["body"]))
    return [target for target in targets if target]


#: The shapes an input_schema is written in. BOTH already exist in real databases and a third
#: would make the reconciliation worse, not better:
#:
#:   * ``{"video_url": "string"}``                  — name-to-type, what draft_workflow emits
#:   * ``{"required": [...], "properties": {...}}`` — JSON Schema, 21 of 45 versions in one DB
#:
#: So this VALIDATES both and invents neither. Richer description — a label, a hint, an
#: example — belongs in JSON Schema's own `title`, `description` and `examples`, which is what
#: those keys are for and what any other tool already understands.


def declared_input_names(raw: Any) -> list[str]:
    """The input names a spec declares, in whichever shape it wrote them.

    Reading `.keys()` on the JSON Schema shape yields ["required"], which told operators to
    supply an input called "required" — confidently wrong, and truthy enough to displace the
    real diagnosis. Kept here beside the validator so the runtime and the tools agree.
    """
    if not isinstance(raw, dict) or not raw:
        return []
    if "required" in raw or "properties" in raw:
        required = raw.get("required")
        if isinstance(required, list):
            return [str(x) for x in required]
        properties = raw.get("properties")
        return [str(k) for k in properties] if isinstance(properties, dict) else []
    return [str(k) for k in raw]


def validate_input_schema(raw: Any) -> dict[str, Any]:
    """What this workflow needs supplied when a run is started.

    WHY IT MATTERS. `from_input` has always resolved ANY field from a run's input — the
    resolver looks the name up in a dict — so a step could already read `video_url`. What no
    entry point could do was PASS one: start_workflow accepted directive, workflow_key, title
    and from_discovery and nothing else, so a workflow needing a file to fetch or a channel to
    publish to had nowhere to receive them.

    Returned unchanged when it is one of the two known shapes. This is a check, not a
    rewrite: versions already published carry these values, and normalising them into a third
    shape would strand every one of them.
    """
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise WorkflowValidationError("input_schema must be an object.")
    for name in declared_input_names(raw):
        if not _STEP_ID.fullmatch(str(name)):
            raise WorkflowValidationError(
                f"declared input '{name}' must use letters, numbers, dot, underscore, or dash.")
    return raw


def missing_required_inputs(spec: dict[str, Any], input_data: dict[str, Any]) -> list[str]:
    """Declared inputs a run was started without. Empty when it can proceed.

    Only JSON Schema distinguishes required from optional — the name-to-type shape says
    nothing about it, so nothing there is treated as mandatory. Guessing would refuse runs
    that have always been legal.

    Checked at START rather than when the step that needs it runs: a workflow that fails on
    its third step because nobody supplied a video URL has already paid for the first two.
    """
    schema = (spec or {}).get("input_schema") or {}
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    supplied = input_data or {}
    return sorted(
        str(name) for name in required
        if not str(supplied.get(str(name)) or "").strip()
    )


def validate_workflow_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise WorkflowValidationError("workflow spec must be an object.")
    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowValidationError("workflow spec must contain at least one step.")

    normalized_steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise WorkflowValidationError("every workflow step must be an object.")
        step = dict(raw)
        step_id = _clean_text(step.get("id"), "step.id")
        if not _STEP_ID.fullmatch(step_id):
            raise WorkflowValidationError(
                f"step id '{step_id}' must use letters, numbers, dot, underscore, or dash."
            )
        if step_id in ids:
            raise WorkflowValidationError(f"duplicate workflow step id '{step_id}'.")
        ids.add(step_id)
        kind = _clean_text(step.get("kind"), f"step '{step_id}' kind").lower()
        if kind not in WORKFLOW_KINDS:
            raise WorkflowValidationError(
                f"step '{step_id}' kind must be one of: {', '.join(sorted(WORKFLOW_KINDS))}."
            )
        step["id"] = step_id
        step["kind"] = kind
        step["max_attempts"] = int(step.get("max_attempts", 3))
        if step["max_attempts"] < 1 or step["max_attempts"] > 25:
            raise WorkflowValidationError(
                f"step '{step_id}' max_attempts must be between 1 and 25."
            )
        if "next" in step and isinstance(step["next"], str):
            step["next"] = [step["next"]]
        elif "next" not in step:
            step["next"] = []
        elif not isinstance(step["next"], list):
            raise WorkflowValidationError(f"step '{step_id}' next must be a list.")

        binding = step.get("binding") or {}
        if not isinstance(binding, dict):
            raise WorkflowValidationError(f"step '{step_id}' binding must be an object.")
        step["binding"] = binding
        if kind == "capability" and binding.get("agent_work"):
            # A STEP WHOSE SPECIFICATION IS A SENTENCE. It binds no capability and no custom
            # step, so neither of the checks below applies — what it needs is an instruction and,
            # optionally, an agent. See services/agent_steps.py for why this shape exists.
            wiring = binding.get("inputs") or {}
            if not isinstance(wiring, dict) or "instructions" not in wiring:
                raise WorkflowValidationError(
                    f"step '{step_id}' must wire 'instructions' — that is the whole of what it "
                    "tells the agent to do. Read it from the run's input, or pin a literal.")
            # WHETHER THE NAMED AGENT EXISTS is checked here; whether it HOLDS anything is
            # checked in `_validate_bindings`, which has the project in hand. Both fail closed,
            # because a step given to an agent that can do nothing is a run that wastes a gate.
            captain_problems = validate_captain(step)
            if captain_problems:
                raise WorkflowValidationError(" ".join(captain_problems))
            approval = dict(step.get("approval") or {})
            mode = str(approval.get("mode", "auto")).lower()
            if mode not in ("auto", "manual"):
                raise WorkflowValidationError(
                    f"step '{step_id}' approval mode must be 'auto' or 'manual'.")
            # AUTO DOES NOT MEAN UNATTENDED. The agent's own loop suspends on any skill at or
            # above the risk floor, so a step that reaches a tier-3 publish stops for a person
            # whatever this says. Recorded as asked rather than rewritten, because the gate that
            # fires is the skill's, not the step's.
            approval["mode"] = mode
            approval.setdefault("on_timeout", "block")
            step["approval"] = approval
        elif kind == "capability" and binding.get("custom_step"):
            # A step the customer defined. It cannot be checked here — whether the skill it
            # binds still exists, and what risk tier it really carries, are facts about the
            # project, not about the spec. `_validate_bindings` does it with a connection in
            # hand; this pass only confirms the shape.
            if not str(binding.get("custom_step") or "").strip():
                raise WorkflowValidationError(
                    f"step '{step_id}' does not say which custom step it runs."
                )
            # A CUSTOM STEP MAY NAME AN AGENT TOO, and until now only the built-in capabilities
            # had their captain checked — so a typo in a role on a customer's step published
            # cleanly and was denied at run time, twenty minutes into a workflow, as
            # "caller 'vidoe_editor' not in allowed_callers".
            captain_problems = validate_captain(step)
            if captain_problems:
                raise WorkflowValidationError(" ".join(captain_problems))
            approval = dict(step.get("approval") or {})
            mode = str(approval.get("mode", "auto")).lower()
            if mode not in ("auto", "manual"):
                raise WorkflowValidationError(
                    f"step '{step_id}' approval mode must be 'auto' or 'manual'."
                )
            approval["mode"] = mode
            approval.setdefault("on_timeout", "block")
            step["approval"] = approval
        elif kind == "capability":
            # Every problem at once. A builder that surfaces one error at a time makes a
            # customer fix a workflow five times to publish it once.
            problems = capability_problems(step)
            if problems:
                raise WorkflowValidationError(" ".join(problems))
            # Store the EFFECTIVE approval mode, not the requested one: a plan that reads
            # "auto" beside a step that will stop and wait is a plan nobody can trust.
            step["approval"] = effective_approval_policy(
                str(binding["capability"]), step.get("approval")
            )
        if kind == "skill" and not binding.get("skill_id"):
            raise WorkflowValidationError(f"skill step '{step_id}' requires binding.skill_id.")
        if kind == "skill" and step.get("approval"):
            # A customer's own step is under the same floor as a standard one. Without this,
            # "custom step" would be the documented way to deploy to production unattended.
            # The skill's real risk tier is checked against the project registry in
            # `_validate_bindings`, which is the only place that can see it.
            approval = dict(step["approval"])
            mode = str(approval.get("mode", "auto")).lower()
            if mode not in ("auto", "manual"):
                raise WorkflowValidationError(
                    f"step '{step_id}' approval mode must be 'auto' or 'manual'."
                )
            approval["mode"] = mode
            step["approval"] = approval
        if kind == "integration" and not binding.get("integration_connection_id"):
            raise WorkflowValidationError(
                f"integration step '{step_id}' requires binding.integration_connection_id."
            )
        if kind == "agent_group" and not binding.get("agent_group_version_id"):
            raise WorkflowValidationError(
                f"agent_group step '{step_id}' requires binding.agent_group_version_id."
            )
        if kind == "subworkflow" and not binding.get("workflow_version_id"):
            raise WorkflowValidationError(
                f"subworkflow step '{step_id}' requires binding.workflow_version_id."
            )
        if kind == "condition":
            if not isinstance(step.get("expression"), dict):
                raise WorkflowValidationError(
                    f"condition step '{step_id}' requires a declarative expression."
                )
            _clean_text(step.get("on_true"), f"condition step '{step_id}' on_true")
            _clean_text(step.get("on_false"), f"condition step '{step_id}' on_false")
        if kind == "parallel":
            branches = step.get("branches")
            if not isinstance(branches, list) or len(branches) < 2:
                raise WorkflowValidationError(
                    f"parallel step '{step_id}' requires at least two branches."
                )
            _clean_text(step.get("join"), f"parallel step '{step_id}' join")
        if kind == "join":
            wait_for = step.get("wait_for")
            if not isinstance(wait_for, list) or not wait_for:
                raise WorkflowValidationError(f"join step '{step_id}' requires wait_for.")
            if step.get("success_policy", "all") not in {"all", "any"}:
                raise WorkflowValidationError(
                    f"join step '{step_id}' success_policy must be all or any."
                )
        if kind == "bounded_loop":
            _clean_text(step.get("body"), f"bounded_loop step '{step_id}' body")
            if not isinstance(step.get("expression"), dict):
                raise WorkflowValidationError(
                    f"bounded_loop step '{step_id}' requires a declarative expression."
                )
            maximum = int(step.get("max_iterations", 0))
            if maximum < 1 or maximum > 100:
                raise WorkflowValidationError(
                    f"bounded_loop step '{step_id}' max_iterations must be between 1 and 100."
                )
            step["max_iterations"] = maximum
        if kind == "timer":
            seconds = int(step.get("wait_seconds", 0))
            if seconds < 0 or seconds > 31_536_000:
                raise WorkflowValidationError(
                    f"timer step '{step_id}' wait_seconds must be between 0 and 31536000."
                )
            step["wait_seconds"] = seconds
        if kind == "decision":
            _clean_text(step.get("question"), f"decision step '{step_id}' question")
        if "compensation" in step and not isinstance(step["compensation"], dict):
            raise WorkflowValidationError(
                f"step '{step_id}' compensation must be an object."
            )
        normalized_steps.append(step)

    entry = _clean_text(spec.get("entry_step"), "entry_step")
    if entry not in ids:
        raise WorkflowValidationError(f"entry step '{entry}' does not exist.")

    normalized = {
        **spec,
        "entry_step": entry,
        "input_schema": validate_input_schema(spec.get("input_schema")),
        "output_schema": spec.get("output_schema") or {},
        "steps": normalized_steps,
    }
    steps = _step_map(normalized)
    for step in normalized_steps:
        for target in _targets(step):
            if target not in steps:
                raise WorkflowValidationError(
                    f"step '{step['id']}' references missing step '{target}'."
                )
        if step["kind"] == "join":
            for dependency in step["wait_for"]:
                if dependency not in steps:
                    raise WorkflowValidationError(
                        f"join step '{step['id']}' waits for missing step '{dependency}'."
                    )

    # Declared edges must be acyclic.  Repetition is represented only by a
    # bounded_loop controller, never by an arbitrary graph cycle.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise WorkflowValidationError(
                f"unbounded cycle detected at step '{step_id}'; use bounded_loop."
            )
        if step_id in visited:
            return
        visiting.add(step_id)
        for target in _targets(steps[step_id]):
            visit(target)
        visiting.remove(step_id)
        visited.add(step_id)

    visit(entry)
    unreachable = sorted(ids - visited)
    if unreachable:
        raise WorkflowValidationError(
            "unreachable workflow steps: " + ", ".join(unreachable)
        )
    return normalized


def _serialize_definition(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "description": row.get("description"),
        "status": row["status"],
        "namespace": row.get("namespace") or SYSTEM_NAMESPACE,
        "is_catalogue": bool(row.get("is_catalogue")),
        "forked_from_version_id": row.get("forked_from_version_id"),
        "active_version_id": row.get("active_version_id"),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _serialize_version(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_definition_id": row["workflow_definition_id"],
        "version": int(row["version"]),
        "status": row["status"],
        "spec": _json(row["spec"], {}),
        "content_hash": row["content_hash"],
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "published_at": _iso(row["published_at"]),
    }


def _serialize_mapping(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_version_id": row["workflow_version_id"],
        "selector_kind": row["selector_kind"],
        "selector_value": row["selector_value"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def require_namespace(
    connection: Connection,
    project_id: str,
    definition_id: str,
    expected: str,
) -> dict[str, Any]:
    """Fetch a definition and refuse it if it belongs to the other tier.

    THE WHOLE POINT OF PHASE 0. Customer workflows and BOSS's own share this runtime because
    leasing, retries, evidence and approval are hard and already correct here. They must never
    share an editing surface: `governed_coding_pipeline` governs every build in the system, and
    a customer-facing builder that could reach it is one mis-scoped query away from rewriting
    the thing that reviews its own code.

    Called by every product-facing entry point. A guard that some callers remember is not a
    guard, so the test suite asserts the set of entry points that call it.
    """
    row = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id=:id AND project_id=:project_id"),
        {"id": definition_id, "project_id": project_id},
    ).mappings().first()
    if row is None:
        raise WorkflowNotFoundError(f"workflow definition '{definition_id}' was not found.")
    actual = row.get("namespace") or SYSTEM_NAMESPACE
    if actual != expected:
        raise WorkflowNamespaceError(
            f"workflow '{row['workflow_key']}' is a {actual} workflow and cannot be used "
            f"from the {expected} surface."
        )
    return dict(row)


def delete_definition(
    connection: Connection, project_id: str, definition_id: str, actor: str
) -> dict[str, Any]:
    """Remove a customer's own workflow.

    "Build your own" without this is a list that only grows: every fork, every import and every
    mistake stays in the picker forever, and a project ends up offering several entries with the
    same name and no way to tell them apart. That is the state this was written in.

    THREE THINGS IT REFUSES, and each is a way to lose work rather than tidy it:

      * A catalogue workflow — the twelve BOSS ships are not the customer's to delete.
      * Anything outside the product namespace — same rule as every other product entry point.
      * A workflow with a RUN THAT IS STILL GOING. Deleting the definition under a live run
        strands it: the runtime reads the spec to decide what comes next, so it would be a run
        that cannot finish and cannot say why.

    Finished runs are kept and so are the versions they pin, because a run's history has to stay
    readable after the workflow it came from is gone. Only the definition and its mappings go —
    which is what stops it appearing in the picker.
    """
    definition = require_namespace(connection, project_id, definition_id, PRODUCT_NAMESPACE)
    require_editable(definition)

    live = connection.execute(
        text(
            """
            SELECT count(*) FROM workflow_runs
            WHERE workflow_definition_id = :id
              AND status IN ('running', 'waiting', 'queued', 'validating')
            """
        ),
        {"id": definition_id},
    ).scalar() or 0
    if int(live):
        raise WorkflowConflictError(
            f"'{definition['name']}' has {int(live)} run"
            f"{'' if int(live) == 1 else 's'} still going. Let them finish, or cancel them, "
            "before deleting it."
        )

    # The mapping goes either way: whatever happens to the definition, no new work should be
    # routed to it.
    connection.execute(
        text("DELETE FROM workflow_mappings WHERE workflow_version_id IN "
             "(SELECT id FROM workflow_versions WHERE workflow_definition_id=:id)"),
        {"id": definition_id},
    )

    # HAS IT EVER RUN?
    #
    # This decides between two genuinely different acts, and the database enforces the
    # difference: `workflow_versions` cascades from the definition, so deleting one that has
    # history would take the specs with it — and a finished run whose spec has vanished cannot
    # be read back. `workflow_runs` has no cascade at all, so the delete simply fails.
    #
    # Never run  -> a mistake, and removing it leaves nothing behind.
    # Has runs   -> RETIRED. It leaves the picker, its history stays readable.
    ever = connection.execute(
        text("SELECT count(*) FROM workflow_runs WHERE workflow_definition_id=:id"),
        {"id": definition_id},
    ).scalar() or 0

    if int(ever):
        connection.execute(
            text("UPDATE workflow_definitions SET status='retired', updated_at=now() "
                 "WHERE id=:id AND project_id=:project_id"),
            {"id": definition_id, "project_id": project_id},
        )
        outcome = "retired"
    else:
        connection.execute(
            text("DELETE FROM workflow_definitions WHERE id=:id AND project_id=:project_id"),
            {"id": definition_id, "project_id": project_id},
        )
        outcome = "deleted"

    emit_event(
        connection, project_id, f"workflow.definition_{outcome}", actor,
        "workflow_definition", definition_id,
        {"workflow_key": definition["workflow_key"], "name": definition["name"],
         "runs": int(ever)},
    )
    return {"outcome": outcome, "id": definition_id, "runs": int(ever),
            "workflow_key": definition["workflow_key"], "name": definition["name"]}


def require_editable(definition: dict[str, Any]) -> None:
    """Refuse to edit a catalogue workflow in place.

    The twelve BOSS ships are read-only on purpose: forking is what keeps "reset to the
    standard" meaningful and keeps the customer's diff against it visible. Editing in place
    would quietly make every fleet's "Standard Product Delivery" a different thing.
    """
    if definition.get("is_catalogue"):
        raise WorkflowNamespaceError(
            f"'{definition['workflow_key']}' is a standard workflow BOSS ships and cannot be "
            "edited in place. Fork it to make your own version."
        )


def create_definition(
    connection: Connection,
    *,
    project_id: str,
    workflow_key: str,
    name: str,
    description: Optional[str],
    actor: str,
    namespace: str = SYSTEM_NAMESPACE,
    is_catalogue: bool = False,
    forked_from_version_id: Optional[str] = None,
) -> dict[str, Any]:
    if namespace not in NAMESPACES:
        raise WorkflowValidationError(
            f"namespace must be one of: {', '.join(sorted(NAMESPACES))}."
        )
    definition_id = make_id("wfd")
    try:
        row = connection.execute(
            text(
                """
                INSERT INTO workflow_definitions (
                    id, project_id, workflow_key, name, description, status, created_by,
                    namespace, is_catalogue, forked_from_version_id
                ) VALUES (:id, :project_id, :workflow_key, :name, :description, 'active', :actor,
                          :namespace, :is_catalogue, :forked_from)
                RETURNING *
                """
            ),
            {
                "id": definition_id,
                "project_id": project_id,
                "workflow_key": _clean_text(workflow_key, "workflow_key"),
                "name": _clean_text(name, "name"),
                "description": description,
                "actor": _clean_text(actor, "actor"),
                "namespace": namespace,
                "is_catalogue": bool(is_catalogue),
                "forked_from": forked_from_version_id,
            },
        ).mappings().one()
    except Exception as error:
        if "workflow_definitions_project_id_workflow_key_key" in str(error):
            raise WorkflowConflictError(
                f"workflow key '{workflow_key}' already exists in this project."
            ) from error
        raise
    emit_event(
        connection,
        project_id,
        "workflow.definition_created",
        actor,
        "workflow_definition",
        definition_id,
        {"workflow_key": workflow_key, "name": name},
    )
    return _serialize_definition(row)


def list_definitions(
    connection: Connection,
    project_id: str,
    *,
    include_retired: bool = False,
    namespace: Optional[str] = None,
) -> list[dict[str, Any]]:
    clause = "" if include_retired else "AND d.status <> 'retired'"
    if namespace is not None:
        if namespace not in NAMESPACES:
            raise WorkflowValidationError(
                f"namespace must be one of: {', '.join(sorted(NAMESPACES))}."
            )
        # COALESCE, because rows written before the column existed carry NULL until the default
        # is applied — and a system workflow invisible to the system list is worse than noise.
        clause += " AND COALESCE(d.namespace, 'system') = :namespace"
    rows = connection.execute(
        text(
            f"""
            SELECT d.*,
                   COALESCE((SELECT count(*) FROM workflow_versions v
                             WHERE v.workflow_definition_id=d.id), 0) AS version_count,
                   -- HOW BIG IT IS, so a workflow the operator made can be DRAWN and not
                   -- merely counted. The catalogue grid renders shipped entries as cards with
                   -- a step rail; the operator's own appeared nowhere but a dropdown, because
                   -- this list carried no steps to draw. Reported as: "the workflow itself
                   -- does not exist in Catalogue and how is it coming to Edit mode".
                   COALESCE((SELECT jsonb_array_length(v.spec->'steps')
                             FROM workflow_versions v
                             WHERE v.id = d.active_version_id), 0) AS step_count,
                   -- AND WHICH STEPS THEY ARE. A card reading "Steps 4" tells an operator how
                   -- big their workflow is and nothing about what it does, so the only way to
                   -- find out was to open the editor. The names are three fields away in the
                   -- spec that was already being measured here.
                   (SELECT v.spec->'steps' FROM workflow_versions v
                     WHERE v.id = d.active_version_id) AS step_specs,
                   (SELECT v.version FROM workflow_versions v
                     WHERE v.id = d.active_version_id) AS active_version,
                   -- WHAT STARTING IT WILL ASK FOR. The console's start form renders FROM
                   -- this; without it a workflow whose steps read `from_input` is startable
                   -- only by a caller that already knows the field names.
                   COALESCE((SELECT v.spec->'input_schema'
                             FROM workflow_versions v
                             -- Doubled braces: this SQL is an f-string ({clause} above).
                             WHERE v.id = d.active_version_id), '{{}}'::jsonb) AS input_schema
            FROM workflow_definitions d
            WHERE d.project_id=:project_id {clause}
            ORDER BY d.updated_at DESC, d.workflow_key
            """
        ),
        {"project_id": project_id, **({"namespace": namespace} if namespace else {})},
    ).mappings().all()
    # Labels for the customer's own steps, resolved once for the whole list rather than per
    # workflow: a project has a handful of them and every workflow draws from the same set.
    step_labels: dict[str, str] = {}
    if _table_exists(connection, "workflow_custom_steps"):
        step_labels = {
            r["step_key"]: r["label"] for r in connection.execute(
                text("SELECT step_key, label FROM workflow_custom_steps WHERE project_id=:p"),
                {"p": project_id},
            ).mappings()
        }

    items = []
    for row in rows:
        item = _serialize_definition(row)
        item["version_count"] = int(row["version_count"])
        item["step_count"] = int(row["step_count"] or 0)
        item["steps"] = _step_outline(row["step_specs"], step_labels)
        item["active_version"] = (
            int(row["active_version"]) if row["active_version"] is not None else None)
        # WHAT STARTING IT WILL ASK FOR.
        #
        # SELECTED BY THE QUERY ABOVE SINCE THE DAY IT WAS WRITTEN, AND NEVER COPIED ONTO THE
        # ITEM. The comment in the SQL says exactly what it is for — "the console's start form
        # renders FROM this" — and the value never arrived. So the form drew nothing for every
        # workflow that declares inputs, and "Start one", which decides whether to open the form
        # by asking whether any were declared, always concluded "none" and started the run on a
        # sample instead. The operator pressed the button on a workflow needing a video link and
        # got a software-delivery sample.
        item["input_schema"] = row["input_schema"] if isinstance(row["input_schema"], dict) else {}
        items.append(item)
    return items


def _step_outline(specs: Any, step_labels: dict[str, str]) -> list[dict[str, Any]]:
    """Just enough of each step to DRAW a workflow rather than count it.

    Name, WHO RUNS IT, whether it is the customer's own, and whether it stops for a person —
    which is the set of facts somebody deciding whether to start a run actually needs. Not the
    bindings or the input wiring: those belong on the step itself, and a card that carried them
    would be a worse version of the editor.

    THE AGENT WAS MISSING AND IT IS THE FIRST THING PEOPLE LOOK FOR. The product's claim is that
    agents do the work and steps track it; a card that lists three step names and no agent
    invites exactly the question it was asked — "did you assign the agents or not?" — about a
    workflow where all three were assigned correctly.
    """
    if not isinstance(specs, list):
        return []
    outline: list[dict[str, Any]] = []
    for step in specs:
        if not isinstance(step, dict):
            continue
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        custom = str(binding.get("custom_step") or "")
        capability = str(binding.get("capability") or "")
        outline.append({
            "id": str(step.get("id") or ""),
            # The operator's own name for it where there is one. Falling back to the key beats
            # falling back to nothing: a step whose definition was deleted still has to appear.
            "label": step_labels.get(custom) or custom or capability or str(step.get("id") or ""),
            "kind": str(step.get("kind") or ""),
            "mine": bool(custom),
            # WHO ANSWERS FOR IT. Empty is meaningful and differs by kind: on an ordinary step it
            # is the anonymous workflow runner; on an agent step it is "whichever agent fits",
            # which BOSS resolves per run. The console says which.
            "captain": str(step.get("captain") or "").strip(),
            # A step that binds nothing and works from an instruction. Worth flagging on the card
            # because it is the one kind whose behaviour is not fixed by its name.
            "agent_work": bool(binding.get("agent_work")),
            "approval": str((step.get("approval") or {}).get("mode") or "auto"),
        })
    return outline


def get_definition(
    connection: Connection, project_id: str, definition_id: str
) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id=:id AND project_id=:project_id"),
        {"id": definition_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow definition not found.")
    versions = connection.execute(
        text(
            """
            SELECT * FROM workflow_versions
            WHERE workflow_definition_id=:definition_id
            ORDER BY version DESC
            """
        ),
        {"definition_id": definition_id},
    ).mappings().all()
    result = _serialize_definition(row)
    result["versions"] = [_serialize_version(version) for version in versions]
    return result


def list_definition_details(
    connection: Connection, project_id: str, *, include_retired: bool = False
) -> list[dict[str, Any]]:
    """List full definitions without issuing one versions query per definition."""
    definitions = list_definitions(
        connection, project_id, include_retired=include_retired
    )
    if not definitions:
        return []
    versions = connection.execute(
        text(
            """
            SELECT * FROM workflow_versions
            WHERE project_id=:project_id
            ORDER BY workflow_definition_id, version DESC
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    versions_by_definition: dict[str, list[dict[str, Any]]] = {}
    for version in versions:
        versions_by_definition.setdefault(
            version["workflow_definition_id"], []
        ).append(_serialize_version(version))
    for definition in definitions:
        definition["versions"] = versions_by_definition.get(definition["id"], [])
    return definitions


def publish_version(
    connection: Connection,
    *,
    project_id: str,
    definition_id: str,
    spec: dict[str, Any],
    actor: str,
    activate: bool = True,
) -> dict[str, Any]:
    definition = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id=:id AND project_id=:project_id FOR UPDATE"),
        {"id": definition_id, "project_id": project_id},
    ).mappings().first()
    if not definition:
        raise WorkflowNotFoundError("Workflow definition not found.")
    normalized = validate_workflow_spec(spec)

    # Custom steps are checked HERE as well as at start_run, and only these. Their risk tier
    # comes from the skill or MCP server they bind, so this is the first moment the real tier
    # is knowable — and the builder is where the customer can still do something about it.
    # Everything else stays at start_run, where it has always been: applying the rest of the
    # binding checks at publish would fail workflows that publish now and bind later.
    if str(definition.get("namespace") or SYSTEM_NAMESPACE) == PRODUCT_NAMESPACE:
        for step in normalized["steps"]:
            if (step.get("binding") or {}).get("custom_step"):
                problems = defined_step_problems(connection, project_id, step)
                if problems:
                    raise WorkflowBindingBlockedError(" ".join(problems))

    digest = json_hash(normalized)
    existing = connection.execute(
        text(
            """
            SELECT * FROM workflow_versions
            WHERE workflow_definition_id=:definition_id AND content_hash=:content_hash
            """
        ),
        {"definition_id": definition_id, "content_hash": digest},
    ).mappings().first()
    if existing:
        result = _serialize_version(existing)
        result["duplicate"] = True
        # IDENTICAL TO AN OLDER VERSION IS NOT THE SAME AS IDENTICAL TO THE CURRENT ONE.
        #
        # Returning the match and stopping is right when it IS the active version: nothing to
        # do. When it is an older one, the operator has just submitted a spec that differs from
        # what is live and been told nothing changed — so reverting by re-saving a previous
        # configuration silently did nothing and left the version they were correcting in place.
        # Found by restoring a workflow after a bad save and watching the bad one stay active.
        #
        # Publishing the same content twice must not create two versions, so the match is
        # ACTIVATED rather than copied: the history stays honest and the intent is carried out.
        if str(definition.get("active_version_id") or "") != str(existing["id"]):
            connection.execute(
                text(
                    """
                    UPDATE workflow_definitions
                    SET active_version_id=:version_id, status='active', updated_at=now()
                    WHERE id=:definition_id AND project_id=:project_id
                    """
                ),
                {"version_id": existing["id"], "definition_id": definition_id,
                 "project_id": project_id},
            )
            result["reactivated"] = True
            emit_event(
                connection, project_id, "workflow.version_reactivated", actor,
                "workflow_version", str(existing["id"]),
                {"definition_id": definition_id, "version": existing["version"]},
            )
        return result
    next_version = int(
        connection.execute(
            text(
                "SELECT COALESCE(max(version), 0) + 1 FROM workflow_versions "
                "WHERE workflow_definition_id=:definition_id"
            ),
            {"definition_id": definition_id},
        ).scalar_one()
    )
    version_id = make_id("wfv")
    row = connection.execute(
        text(
            """
            INSERT INTO workflow_versions (
                id, project_id, workflow_definition_id, version, status,
                spec, content_hash, created_by
            ) VALUES (
                :id, :project_id, :definition_id, :version, 'published',
                CAST(:spec AS JSONB), :content_hash, :actor
            ) RETURNING *
            """
        ),
        {
            "id": version_id,
            "project_id": project_id,
            "definition_id": definition_id,
            "version": next_version,
            "spec": json.dumps(normalized),
            "content_hash": digest,
            "actor": actor,
        },
    ).mappings().one()
    if activate:
        connection.execute(
            text(
                """
                UPDATE workflow_definitions
                SET active_version_id=:version_id, status='active', updated_at=now()
                WHERE id=:definition_id
                """
            ),
            {"version_id": version_id, "definition_id": definition_id},
        )
    emit_event(
        connection,
        project_id,
        "workflow.version_published",
        actor,
        "workflow_definition",
        definition_id,
        {
            "workflow_version_id": version_id,
            "version": next_version,
            "content_hash": digest,
            "activated": activate,
        },
    )
    result = _serialize_version(row)
    result["duplicate"] = False
    return result


def create_mapping(
    connection: Connection,
    *,
    project_id: str,
    workflow_version_id: str,
    selector_kind: str,
    selector_value: str,
    scope_type: str,
    scope_id: str,
    priority: int,
    actor: str,
) -> dict[str, Any]:
    version = connection.execute(
        text(
            """
            SELECT id FROM workflow_versions
            WHERE id=:id AND project_id=:project_id AND status='published'
            """
        ),
        {"id": workflow_version_id, "project_id": project_id},
    ).first()
    if not version:
        raise WorkflowNotFoundError("Published workflow version not found.")
    scope_type = _clean_text(scope_type, "scope_type").lower()
    if scope_type not in {"org", "workspace", "project"}:
        raise WorkflowValidationError("scope_type must be org, workspace, or project.")
    ancestry = _project_ancestry(connection, project_id)
    expected_scope = ancestry[f"{scope_type}_id"]
    if scope_id != expected_scope:
        raise WorkflowValidationError(
            f"scope_id does not match the workflow project's {scope_type}."
        )
    mapping_id = make_id("wfm")
    try:
        row = connection.execute(
            text(
                """
                INSERT INTO workflow_mappings (
                    id, project_id, workflow_version_id, selector_kind, selector_value,
                    scope_type, scope_id, priority, enabled, created_by
                ) VALUES (
                    :id, :project_id, :version_id, :selector_kind, :selector_value,
                    :scope_type, :scope_id, :priority, true, :actor
                ) RETURNING *
                """
            ),
            {
                "id": mapping_id,
                "project_id": project_id,
                "version_id": workflow_version_id,
                "selector_kind": _clean_text(selector_kind, "selector_kind").lower(),
                "selector_value": _clean_text(selector_value, "selector_value"),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "priority": int(priority),
                "actor": actor,
            },
        ).mappings().one()
    except Exception as error:
        if "uq_workflow_mappings_active_selector" in str(error):
            raise WorkflowConflictError(
                "An enabled mapping already occupies this selector, scope, and priority."
            ) from error
        raise
    emit_event(
        connection,
        project_id,
        "workflow.mapping_created",
        actor,
        "workflow_mapping",
        mapping_id,
        {
            "selector_kind": selector_kind,
            "selector_value": selector_value,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "priority": int(priority),
            "workflow_version_id": workflow_version_id,
        },
    )
    return _serialize_mapping(row)


def list_mappings(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_mappings
            WHERE project_id=:project_id
            ORDER BY enabled DESC, selector_kind, selector_value, priority DESC, created_at
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [_serialize_mapping(row) for row in rows]


def _project_ancestry(connection: Connection, project_id: str) -> dict[str, str]:
    row = connection.execute(
        text(
            """
            SELECT p.id AS project_id, p.workspace_id, w.org_id
            FROM projects p JOIN workspaces w ON w.id=p.workspace_id
            WHERE p.id=:project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Project not found.")
    return dict(row)


def resolve_mapping(
    connection: Connection,
    *,
    project_id: str,
    selector_kind: str,
    selector_value: str,
) -> dict[str, Any]:
    ancestry = _project_ancestry(connection, project_id)
    rows = connection.execute(
        text(
            """
            SELECT m.*, v.status AS version_status, d.status AS definition_status,
                   d.workflow_key, d.name AS workflow_name
            FROM workflow_mappings m
            JOIN workflow_versions v ON v.id=m.workflow_version_id
            JOIN workflow_definitions d ON d.id=v.workflow_definition_id
            WHERE m.project_id=:project_id
              AND m.selector_kind=:selector_kind
              AND m.selector_value IN (:selector_value, '*')
            ORDER BY m.created_at, m.id
            """
        ),
        {
            "project_id": project_id,
            "selector_kind": _clean_text(selector_kind, "selector_kind").lower(),
            "selector_value": _clean_text(selector_value, "selector_value"),
        },
    ).mappings().all()
    scope_score = {"org": 1, "workspace": 2, "project": 3}
    candidates: list[tuple[tuple[int, int, int], Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        reason: Optional[str] = None
        if not row["enabled"]:
            reason = "mapping_disabled"
        elif row["version_status"] != "published":
            reason = "version_not_published"
        elif row["definition_status"] != "active":
            reason = "definition_not_active"
        elif ancestry[f"{row['scope_type']}_id"] != row["scope_id"]:
            reason = "scope_mismatch"
        if reason:
            rejected.append({**_serialize_mapping(row), "reason": reason})
            continue
        score = (
            scope_score[row["scope_type"]],
            1 if row["selector_value"] == selector_value else 0,
            int(row["priority"]),
        )
        candidates.append((score, row))
    if not candidates:
        return {
            "status": "unmapped",
            "project_id": project_id,
            "selector_kind": selector_kind,
            "selector_value": selector_value,
            "selected": None,
            "rejected": rejected,
            "reason": "No approved workflow mapping matched this class of work.",
        }
    candidates.sort(key=lambda item: item[0], reverse=True)
    top_score = candidates[0][0]
    tied = [row for score, row in candidates if score == top_score]
    if len(tied) > 1:
        ids = ", ".join(row["id"] for row in tied)
        raise WorkflowMappingAmbiguousError(
            f"Ambiguous workflow mappings at equal precedence: {ids}."
        )
    selected_row = candidates[0][1]
    for score, row in candidates[1:]:
        rejected.append(
            {**_serialize_mapping(row), "reason": f"lower_precedence:{score}"}
        )
    selected = _serialize_mapping(selected_row)
    selected.update(
        {
            "workflow_key": selected_row["workflow_key"],
            "workflow_name": selected_row["workflow_name"],
            "precedence": list(top_score),
        }
    )
    return {
        "status": "selected",
        "project_id": project_id,
        "selector_kind": selector_kind,
        "selector_value": selector_value,
        "selected": selected,
        "rejected": rejected,
        "reason": "Most-specific scope, exact selector, and highest priority won.",
    }


def _version_row(connection: Connection, project_id: str, version_id: str) -> Any:
    row = connection.execute(
        text(
            """
            SELECT v.*, d.workflow_key, d.name AS workflow_name,
                   d.status AS definition_status
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.id=v.workflow_definition_id
            WHERE v.id=:id AND v.project_id=:project_id
            """
        ),
        {"id": version_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow version not found.")
    if row["status"] != "published" or row["definition_status"] != "active":
        raise WorkflowConflictError("Workflow version is not active and published.")
    return row


def _table_exists(connection: Connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _skill_facts(row: Any) -> dict[str, Any]:
    """What a project_skills row says about risk and provenance.

    `risk_tier` lives in the skill's config rather than in a column, so read it there and
    default to the FLOOR when it is absent — an unlabelled skill is treated as dangerous, not
    as safe. Guessing 0 for something nobody classified is how an ungoverned step ships.
    """
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    declared = config.get("risk_tier")
    return {
        "enabled": bool(row.get("enabled", True)),
        "risk_tier": int(declared) if declared is not None else 2,
        "provenance": provenance,
    }


def _validate_bindings(
    connection: Connection,
    project_id: str,
    version_id: str,
    spec: dict[str, Any],
    namespace: str = SYSTEM_NAMESPACE,
) -> None:
    for step in spec["steps"]:
        binding = step.get("binding") or {}
        role_key = binding.get("role_key")
        if role_key:
            row = connection.execute(
                text(
                    "SELECT 1 FROM roles WHERE project_id=:project_id AND role_key=:role_key"
                ),
                {"project_id": project_id, "role_key": role_key},
            ).first()
            if not row:
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' references missing role '{role_key}'."
                )
        if step["kind"] == "skill":
            skill_id = binding["skill_id"]
            row = connection.execute(
                text(
                    """
                    SELECT enabled, config, provenance FROM project_skills
                    WHERE project_id=:project_id AND skill_id=:skill_id
                    """
                ),
                {"project_id": project_id, "skill_id": skill_id},
            ).mappings().first()
            if not row or not bool(row["enabled"]):
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' skill '{skill_id}' is missing or disabled."
                )
            # THE FLOOR APPLIES TO A CUSTOMER'S OWN STEPS TOO — BUT ONLY TO THEIRS.
            #
            # This is the only place that can see a skill's real risk tier and provenance, so
            # it is the only place the check can be made. Without it, "attach your own skill"
            # would be the documented way around every gate in the product.
            #
            # Scoped to `product`, deliberately. BOSS's own thirty-three definitions were
            # written before this rule existed and are governed by the machinery around them;
            # applying a new check to them here would fail to publish workflows that have been
            # running correctly for months, which is exactly what "the infra workflows do not
            # change" rules out.
            if namespace == PRODUCT_NAMESPACE:
                problems = custom_step_problems(step, _skill_facts(row))
                if problems:
                    raise WorkflowBindingBlockedError(" ".join(problems))
        if step["kind"] == "capability" and binding.get("custom_step"):
            # THE ONLY PLACE A CUSTOM STEP'S REAL RISK CAN BE SEEN. The tier comes from the
            # skill or MCP server it binds, which means it takes a connection and a project to
            # read — and it is re-read here on every publish, because a step that was tier 0
            # when it was drawn can be tier 3 by the time it is published.
            problems = defined_step_problems(connection, project_id, step)
            if problems:
                raise WorkflowBindingBlockedError(" ".join(problems))
        if step["kind"] == "integration":
            if not _table_exists(connection, "integration_connections"):
                raise WorkflowBindingBlockedError(
                    "Integration runtime is not installed; integration binding fails closed."
                )
            row = connection.execute(
                text(
                    """
                    SELECT status FROM integration_connections
                    WHERE id=:id AND project_id=:project_id
                    """
                ),
                {"id": binding["integration_connection_id"], "project_id": project_id},
            ).mappings().first()
            if not row or row["status"] not in {"active", "healthy"}:
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' integration is missing, revoked, or unhealthy; "
                    "the integration binding fails closed."
                )
        if step["kind"] == "agent_group":
            if not _table_exists(connection, "agent_group_versions"):
                raise WorkflowBindingBlockedError(
                    "Agent Group runtime is not installed; group binding fails closed."
                )
            row = connection.execute(
                text(
                    """
                    SELECT status FROM agent_group_versions
                    WHERE id=:id AND project_id=:project_id
                    """
                ),
                {"id": binding["agent_group_version_id"], "project_id": project_id},
            ).mappings().first()
            if not row or row["status"] != "published":
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' Agent Group version is unavailable."
                )
        if step["kind"] == "subworkflow":
            child_id = str(binding["workflow_version_id"])
            if child_id == version_id:
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' cannot recursively invoke its own version."
                )
            _version_row(connection, project_id, child_id)


def start_run(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    input_data: dict[str, Any],
    dedupe_key: str,
    workflow_version_id: Optional[str] = None,
    selector_kind: Optional[str] = None,
    selector_value: Optional[str] = None,
    policy_snapshot: Optional[dict[str, Any]] = None,
    parent_run_id: Optional[str] = None,
) -> dict[str, Any]:
    dedupe_key = _clean_text(dedupe_key, "dedupe_key")
    existing = connection.execute(
        text(
            "SELECT id FROM workflow_runs WHERE project_id=:project_id AND dedupe_key=:dedupe_key"
        ),
        {"project_id": project_id, "dedupe_key": dedupe_key},
    ).mappings().first()
    if existing:
        result = get_run(connection, project_id, existing["id"])
        result["duplicate"] = True
        return result

    mapping_id: Optional[str] = None
    if not workflow_version_id:
        if not selector_kind or not selector_value:
            raise WorkflowValidationError(
                "workflow_version_id or selector_kind/selector_value is required."
            )
        resolution = resolve_mapping(
            connection,
            project_id=project_id,
            selector_kind=selector_kind,
            selector_value=selector_value,
        )
        if resolution["status"] != "selected":
            raise WorkflowMappingNotFoundError(resolution["reason"])
        mapping_id = resolution["selected"]["id"]
        workflow_version_id = resolution["selected"]["workflow_version_id"]
    version = _version_row(connection, project_id, str(workflow_version_id))
    spec = validate_workflow_spec(_json(version["spec"], {}))
    version_namespace = connection.execute(
        text(
            "SELECT COALESCE(d.namespace, 'system') FROM workflow_definitions d "
            "WHERE d.id = :definition_id"
        ),
        {"definition_id": version["workflow_definition_id"]},
    ).scalar() or SYSTEM_NAMESPACE
    _validate_bindings(connection, project_id, version["id"], spec, str(version_namespace))

    # REFUSED HERE, NOT FOUR STEPS IN. A workflow that fails on its third step because nobody
    # supplied a video URL has already paid for the first two, and the error it gives is about
    # a missing binding rather than about the thing the operator forgot.
    absent = missing_required_inputs(spec, input_data)
    if absent:
        declared = spec.get("input_schema") or {}
        named = ", ".join(
            f"{name} ({declared[name].get('label') or name})" if declared.get(name) else name
            for name in absent)
        raise WorkflowValidationError(
            f"this workflow needs {len(absent)} input(s) it was not given: {named}")

    run_id = make_id("wfr")
    root_run_id = run_id
    if parent_run_id:
        parent = connection.execute(
            text(
                "SELECT root_run_id FROM workflow_runs WHERE id=:id AND project_id=:project_id"
            ),
            {"id": parent_run_id, "project_id": project_id},
        ).mappings().first()
        if not parent:
            raise WorkflowNotFoundError("Parent workflow run not found.")
        root_run_id = parent["root_run_id"] or parent_run_id
    connection.execute(
        text(
            """
            INSERT INTO workflow_runs (
                id, project_id, workflow_definition_id, workflow_version_id,
                workflow_mapping_id, parent_run_id, root_run_id, actor, status,
                input, context, policy_snapshot, dedupe_key
            ) VALUES (
                :id, :project_id, :definition_id, :version_id, :mapping_id,
                :parent_run_id, :root_run_id, :actor, 'validating',
                CAST(:input AS JSONB), CAST(:context AS JSONB),
                CAST(:policy AS JSONB), :dedupe_key
            )
            """
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "definition_id": version["workflow_definition_id"],
            "version_id": version["id"],
            "mapping_id": mapping_id,
            "parent_run_id": parent_run_id,
            "root_run_id": root_run_id,
            "actor": actor,
            "input": json.dumps(input_data or {}),
            "context": json.dumps({}),
            "policy": json.dumps(policy_snapshot or {}),
            "dedupe_key": dedupe_key,
        },
    )
    for step in spec["steps"]:
        _insert_step_run(
            connection,
            project_id=project_id,
            run_id=run_id,
            step=step,
            iteration=0,
            status="pending",
        )
    connection.execute(
        text("UPDATE workflow_runs SET status='running', updated_at=now() WHERE id=:id"),
        {"id": run_id},
    )
    # WHO IS ON THIS, seeded before the first step can be refused for not knowing.
    #
    # Assignment is enforced at the gates, so an app that starts with nobody on it would have
    # every gate refuse — the feature would read as breakage rather than governance. The seed is
    # exactly the agents that would have done the work anyway: the STAGE_ROLES set BOSS already
    # attributes every model call to, plus whatever this workflow names as an approver. An app
    # that already has an assignment is left alone, so a re-run never overwrites a decision.
    try:
        from . import agent_assignment
        app_name = str((input_data or {}).get("title")
                       or (input_data or {}).get("directive") or "").strip()
        if app_name:
            agent_assignment.ensure_defaults(
                connection, project_id=project_id, app_name=app_name,
                actor=actor, spec=spec)
    except Exception as error:  # noqa: BLE001 — a run must not fail because seeding did
        logger.warning("could not seed agent assignment for %s: %s", run_id, error)
    emit_event(
        connection,
        project_id,
        "workflow.run_started",
        actor,
        "workflow_run",
        run_id,
        {
            "workflow_definition_id": version["workflow_definition_id"],
            "workflow_version_id": version["id"],
            "workflow_mapping_id": mapping_id,
            "dedupe_key": dedupe_key,
            "parent_run_id": parent_run_id,
        },
    )
    _activate_step(connection, run_id, spec["entry_step"], 0)
    _drain_control_steps(connection, run_id)
    result = get_run(connection, project_id, run_id)
    result["duplicate"] = False
    return result


def _insert_step_run(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    step: dict[str, Any],
    iteration: int,
    status: str,
) -> str:
    step_run_id = make_id("wfs")
    connection.execute(
        text(
            """
            INSERT INTO workflow_step_runs (
                id, project_id, workflow_run_id, step_id, step_kind, iteration,
                status, input, binding_snapshot, max_attempts
            ) VALUES (
                :id, :project_id, :run_id, :step_id, :step_kind, :iteration,
                :status, CAST(:input AS JSONB), CAST(:binding AS JSONB), :max_attempts
            ) ON CONFLICT (workflow_run_id, step_id, iteration) DO NOTHING
            """
        ),
        {
            "id": step_run_id,
            "project_id": project_id,
            "run_id": run_id,
            "step_id": step["id"],
            "step_kind": step["kind"],
            "iteration": iteration,
            "status": status,
            "input": json.dumps(step.get("input") or {}),
            "binding": json.dumps(step.get("binding") or {}),
            "max_attempts": int(step.get("max_attempts", 3)),
        },
    )
    return step_run_id


def _run_row(connection: Connection, run_id: str, *, lock: bool = False) -> Any:
    lock_sql = " FOR UPDATE" if lock else ""
    row = connection.execute(
        text(f"SELECT * FROM workflow_runs WHERE id=:id{lock_sql}"),
        {"id": run_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow run not found.")
    return row


def _run_spec(connection: Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT v.spec FROM workflow_runs r
            JOIN workflow_versions v ON v.id=r.workflow_version_id
            WHERE r.id=:run_id
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow run not found.")
    return validate_workflow_spec(_json(row["spec"], {}))


def _step_row(
    connection: Connection,
    run_id: str,
    step_id: str,
    iteration: int,
    *,
    lock: bool = False,
) -> Any:
    lock_sql = " FOR UPDATE" if lock else ""
    return connection.execute(
        text(
            f"""
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND step_id=:step_id AND iteration=:iteration
            {lock_sql}
            """
        ),
        {"run_id": run_id, "step_id": step_id, "iteration": iteration},
    ).mappings().first()


def _serialize_step(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_run_id": row["workflow_run_id"],
        "step_id": row["step_id"],
        "step_kind": row["step_kind"],
        "iteration": int(row["iteration"]),
        "status": row["status"],
        "input": _json(row["input"], {}),
        "output": _json(row["output"], {}),
        "binding_snapshot": _json(row["binding_snapshot"], {}),
        "evidence": _json(row["evidence"], []),
        "attempt": int(row["attempt"]),
        "max_attempts": int(row["max_attempts"]),
        "run_after": _iso(row["run_after"]),
        "lease_owner": row.get("lease_owner"),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
        "heartbeat_at": _iso(row.get("heartbeat_at")),
        "decision_id": row.get("decision_id"),
        "child_workflow_run_id": row.get("child_workflow_run_id"),
        "last_error": row.get("last_error"),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _serialize_run(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_definition_id": row["workflow_definition_id"],
        "workflow_version_id": row["workflow_version_id"],
        "workflow_mapping_id": row.get("workflow_mapping_id"),
        "parent_run_id": row.get("parent_run_id"),
        "root_run_id": row.get("root_run_id"),
        "actor": row["actor"],
        "status": row["status"],
        "input": _json(row["input"], {}),
        "output": _json(row["output"], {}),
        "context": _json(row["context"], {}),
        "policy_snapshot": _json(row["policy_snapshot"], {}),
        "current_frontier": _json(row["current_frontier"], []),
        "dedupe_key": row.get("dedupe_key"),
        "blocked_reason": row.get("blocked_reason"),
        "error": row.get("error"),
        "paused_at": _iso(row.get("paused_at")),
        "completed_at": _iso(row.get("completed_at")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def get_run(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT r.*, d.workflow_key, d.name AS workflow_name, v.version
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id=r.workflow_definition_id
            JOIN workflow_versions v ON v.id=r.workflow_version_id
            WHERE r.id=:run_id AND r.project_id=:project_id
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow run not found.")
    steps = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
            ORDER BY created_at, step_id, iteration
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    migrations = connection.execute(
        text(
            """
            SELECT * FROM workflow_migrations
            WHERE workflow_run_id=:run_id ORDER BY created_at DESC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    compensations = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id ORDER BY sequence DESC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    events = connection.execute(
        text(
            """
            SELECT id, event_type, actor, entity_type, entity_id, data, created_at
            FROM events
            WHERE project_id=:project_id AND entity_type='workflow_run' AND entity_id=:run_id
            ORDER BY created_at, id
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().all()
    result = _serialize_run(row)
    result.update(
        {
            "workflow_key": row["workflow_key"],
            "workflow_name": row["workflow_name"],
            "version": int(row["version"]),
            "steps": [_serialize_step(step) for step in steps],
            "migrations": [_serialize_migration(item) for item in migrations],
            "compensations": [_serialize_compensation(item) for item in compensations],
            "timeline": [
                {
                    "id": event["id"],
                    "event_type": event["event_type"],
                    "actor": event["actor"],
                    "data": _json(event["data"], {}),
                    "created_at": _iso(event["created_at"]),
                }
                for event in events
            ],
        }
    )
    return result


def list_runs(
    connection: Connection,
    project_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 200:
        raise WorkflowValidationError("limit must be between 1 and 200.")
    status_clause = "AND r.status=:status" if status else ""
    params: dict[str, Any] = {"project_id": project_id, "limit": limit}
    if status:
        params["status"] = status
    rows = connection.execute(
        text(
            f"""
            SELECT r.*, d.workflow_key, d.name AS workflow_name, v.version
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id=r.workflow_definition_id
            JOIN workflow_versions v ON v.id=r.workflow_version_id
            WHERE r.project_id=:project_id {status_clause}
            ORDER BY r.updated_at DESC LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    items = []
    for row in rows:
        item = _serialize_run(row)
        item.update(
            {
                "workflow_key": row["workflow_key"],
                "workflow_name": row["workflow_name"],
                "version": int(row["version"]),
            }
        )
        items.append(item)
    return items


def _run_context(connection: Connection, run_id: str) -> dict[str, Any]:
    run = _run_row(connection, run_id)
    rows = connection.execute(
        text(
            """
            SELECT step_id, iteration, status, output
            FROM workflow_step_runs WHERE workflow_run_id=:run_id
            ORDER BY iteration, created_at
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    outputs: dict[str, Any] = {}
    for row in rows:
        key = row["step_id"]
        value = {
            "status": row["status"],
            "iteration": int(row["iteration"]),
            "output": _json(row["output"], {}),
        }
        if key in outputs:
            existing = outputs[key]
            if not isinstance(existing, list):
                outputs[key] = [existing]
            outputs[key].append(value)
        else:
            outputs[key] = value
    return {
        "input": _json(run["input"], {}),
        "context": _json(run["context"], {}),
        "steps": outputs,
    }


def _lookup_path(payload: Any, path: str) -> tuple[bool, Any]:
    parts = path.strip().lstrip("$").lstrip(".").split(".") if path else []
    value = payload
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def evaluate_expression(expression: dict[str, Any], payload: dict[str, Any]) -> bool:
    if not isinstance(expression, dict):
        raise WorkflowValidationError("expression must be an object.")
    if "all" in expression:
        values = expression["all"]
        if not isinstance(values, list):
            raise WorkflowValidationError("expression.all must be a list.")
        return all(evaluate_expression(item, payload) for item in values)
    if "any" in expression:
        values = expression["any"]
        if not isinstance(values, list):
            raise WorkflowValidationError("expression.any must be a list.")
        return any(evaluate_expression(item, payload) for item in values)
    if "not" in expression:
        return not evaluate_expression(expression["not"], payload)
    path = _clean_text(expression.get("path"), "expression.path")
    operator = str(expression.get("op", "truthy")).lower()
    exists, actual = _lookup_path(payload, path)
    expected = expression.get("value")
    if operator == "exists":
        return exists
    if operator == "truthy":
        return exists and bool(actual)
    if operator == "eq":
        return exists and actual == expected
    if operator == "ne":
        return not exists or actual != expected
    if operator == "in":
        return exists and isinstance(expected, list) and actual in expected
    if operator == "not_in":
        return not exists or not isinstance(expected, list) or actual not in expected
    if operator in {"gt", "gte", "lt", "lte"}:
        if not exists:
            return False
        try:
            if operator == "gt":
                return actual > expected
            if operator == "gte":
                return actual >= expected
            if operator == "lt":
                return actual < expected
            return actual <= expected
        except TypeError:
            return False
    raise WorkflowValidationError(
        "expression.op must be exists, truthy, eq, ne, in, not_in, gt, gte, lt, or lte."
    )


def _emit_step_event(
    connection: Connection,
    run: Any,
    event_type: str,
    step: Any,
    data: Optional[dict[str, Any]] = None,
) -> None:
    emit_event(
        connection,
        run["project_id"],
        event_type,
        run["actor"],
        "workflow_run",
        run["id"],
        {
            "workflow_step_run_id": step["id"],
            "step_id": step["step_id"],
            "step_kind": step["step_kind"],
            "iteration": int(step["iteration"]),
            **(data or {}),
        },
    )


def _activate_step(
    connection: Connection, run_id: str, step_id: str, iteration: int = 0
) -> None:
    run = _run_row(connection, run_id)
    if run["status"] in TERMINAL_RUN_STATES or run["status"] in {"paused", "compensating"}:
        return
    spec = _run_spec(connection, run_id)
    template = _step_map(spec).get(step_id)
    if not template:
        raise WorkflowValidationError(f"workflow step '{step_id}' is missing from the pinned version.")
    row = _step_row(connection, run_id, step_id, iteration)
    if row is None:
        _insert_step_run(
            connection,
            project_id=run["project_id"],
            run_id=run_id,
            step=template,
            iteration=iteration,
            status="pending",
        )
        row = _step_row(connection, run_id, step_id, iteration)
    if row["status"] != "pending":
        return
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs SET status='ready', run_after=now(), updated_at=now()
            WHERE id=:id AND status='pending'
            """
        ),
        {"id": row["id"]},
    )
    ready = _step_row(connection, run_id, step_id, iteration)
    _emit_step_event(connection, run, "workflow.step_ready", ready)
    _refresh_frontier(connection, run_id)


def _mark_skipped(connection: Connection, run_id: str, step_id: str, reason: str) -> None:
    row = _step_row(connection, run_id, step_id, 0)
    if not row or row["status"] != "pending":
        return
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='skipped', output=CAST(:output AS JSONB), completed_at=now(), updated_at=now()
            WHERE id=:id AND status='pending'
            """
        ),
        {"id": row["id"], "output": json.dumps({"reason": reason})},
    )
    run = _run_row(connection, run_id)
    skipped = _step_row(connection, run_id, step_id, 0)
    _emit_step_event(
        connection, run, "workflow.step_skipped", skipped, {"reason": reason}
    )


def _succeed_internal(
    connection: Connection,
    run_id: str,
    step_id: str,
    iteration: int,
    output: dict[str, Any],
) -> Any:
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', output=CAST(:output AS JSONB),
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=now(), updated_at=now()
            WHERE workflow_run_id=:run_id AND step_id=:step_id AND iteration=:iteration
            """
        ),
        {
            "run_id": run_id,
            "step_id": step_id,
            "iteration": iteration,
            "output": json.dumps(output or {}),
        },
    )
    if output:
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET context=context || CAST(:output AS JSONB), updated_at=now()
                WHERE id=:run_id
                """
            ),
            {"run_id": run_id, "output": json.dumps(output)},
        )
    row = _step_row(connection, run_id, step_id, iteration)
    run = _run_row(connection, run_id)
    _emit_step_event(connection, run, "workflow.step_succeeded", row, {"output": output})
    return row


def _after_success(
    connection: Connection,
    run_id: str,
    step_id: str,
    iteration: int,
) -> None:
    run = _run_row(connection, run_id)
    if run["status"] not in {"running", "waiting"}:
        _refresh_frontier(connection, run_id)
        return
    spec = _run_spec(connection, run_id)
    steps = _step_map(spec)
    template = steps[step_id]

    # A loop body returns to its controller rather than following ordinary
    # downstream edges.  Each iteration gets distinct durable step rows.
    for controller in spec["steps"]:
        if controller["kind"] == "bounded_loop" and controller.get("body") == step_id:
            waiting = _step_row(connection, run_id, controller["id"], iteration)
            if waiting and waiting["status"] == "waiting":
                _succeed_internal(
                    connection,
                    run_id,
                    controller["id"],
                    iteration,
                    {"continued": True, "iteration": iteration},
                )
                _activate_step(connection, run_id, controller["id"], iteration + 1)
                _drain_control_steps(connection, run_id)
                return

    for target in template.get("next", []):
        _maybe_activate(connection, run_id, str(target), iteration)
    _drain_control_steps(connection, run_id)
    _maybe_finish_run(connection, run_id)


def _maybe_activate(
    connection: Connection, run_id: str, step_id: str, iteration: int = 0
) -> None:
    spec = _run_spec(connection, run_id)
    template = _step_map(spec)[step_id]
    if template["kind"] != "join":
        _activate_step(connection, run_id, step_id, iteration)
        return
    dependency_rows = connection.execute(
        text(
            """
            SELECT step_id, status FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND iteration=:iteration
              AND step_id IN :step_ids
            """
        ).bindparams(bindparam("step_ids", expanding=True)),
        {
            "run_id": run_id,
            "iteration": iteration,
            "step_ids": tuple(template["wait_for"]),
        },
    ).mappings().all()
    statuses = {row["step_id"]: row["status"] for row in dependency_rows}
    policy = template.get("success_policy", "all")
    ready = (
        all(statuses.get(item) == "succeeded" for item in template["wait_for"])
        if policy == "all"
        else any(statuses.get(item) == "succeeded" for item in template["wait_for"])
    )
    if ready:
        _activate_step(connection, run_id, step_id, iteration)


def _drain_control_steps(connection: Connection, run_id: str) -> None:
    for _ in range(500):
        run = _run_row(connection, run_id)
        if run["status"] not in {"running", "waiting"}:
            return
        row = connection.execute(
            text(
                """
                SELECT * FROM workflow_step_runs
                WHERE workflow_run_id=:run_id AND status='ready'
                  AND step_kind IN (
                    'condition', 'parallel', 'join', 'bounded_loop',
                    'decision', 'timer', 'subworkflow'
                  )
                  AND run_after <= now()
                ORDER BY created_at, iteration, step_id LIMIT 1
                """
            ),
            {"run_id": run_id},
        ).mappings().first()
        if not row:
            break
        _process_control_step(connection, run, row)
    else:
        raise WorkflowConflictError("Workflow control-step drain exceeded its safety bound.")
    _refresh_frontier(connection, run_id)
    _maybe_finish_run(connection, run_id)


def _process_control_step(connection: Connection, run: Any, row: Any) -> None:
    spec = _run_spec(connection, run["id"])
    template = _step_map(spec)[row["step_id"]]
    kind = template["kind"]
    iteration = int(row["iteration"])
    context = _run_context(connection, run["id"])
    if kind == "condition":
        passed = evaluate_expression(template["expression"], context)
        selected = template["on_true"] if passed else template["on_false"]
        rejected = template["on_false"] if passed else template["on_true"]
        _succeed_internal(
            connection,
            run["id"],
            row["step_id"],
            iteration,
            {"result": passed, "selected": selected},
        )
        _mark_skipped(connection, run["id"], rejected, "condition_not_selected")
        _activate_step(connection, run["id"], selected, iteration)
        return
    if kind == "parallel":
        _succeed_internal(
            connection,
            run["id"],
            row["step_id"],
            iteration,
            {"branches": template["branches"], "join": template["join"]},
        )
        for branch in template["branches"]:
            _activate_step(connection, run["id"], branch, iteration)
        return
    if kind == "join":
        _succeed_internal(
            connection,
            run["id"],
            row["step_id"],
            iteration,
            {"joined": template["wait_for"], "policy": template.get("success_policy", "all")},
        )
        _after_success(connection, run["id"], row["step_id"], iteration)
        return
    if kind == "bounded_loop":
        should_continue = evaluate_expression(template["expression"], context)
        maximum = int(template["max_iterations"])
        if should_continue and iteration >= maximum:
            reason = (
                f"bounded loop '{row['step_id']}' exhausted max_iterations={maximum}."
            )
            connection.execute(
                text(
                    """
                    UPDATE workflow_step_runs SET status='failed', last_error=:reason,
                        completed_at=now(), updated_at=now() WHERE id=:id
                    """
                ),
                {"id": row["id"], "reason": reason},
            )
            _block_run(connection, run["id"], reason)
            return
        if not should_continue:
            _succeed_internal(
                connection,
                run["id"],
                row["step_id"],
                iteration,
                {"continued": False, "iteration": iteration},
            )
            for target in template.get("next", []):
                _maybe_activate(connection, run["id"], target, 0)
            return
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs SET status='waiting',
                    output=CAST(:output AS JSONB), updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": row["id"],
                "output": json.dumps({"continued": True, "iteration": iteration}),
            },
        )
        _activate_step(connection, run["id"], template["body"], iteration)
        return
    if kind == "timer":
        wake_at = now_utc() + timedelta(seconds=int(template.get("wait_seconds", 0)))
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='waiting', run_after=:wake_at,
                    output=CAST(:output AS JSONB), updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": row["id"],
                "wake_at": wake_at,
                "output": json.dumps({"wake_at": wake_at.isoformat()}),
            },
        )
        connection.execute(
            text("UPDATE workflow_runs SET status='waiting', updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        _emit_step_event(
            connection,
            run,
            "workflow.step_waiting_timer",
            _step_row(connection, run["id"], row["step_id"], iteration),
            {"wake_at": wake_at.isoformat()},
        )
        return
    if kind == "decision":
        decision_id = create_blocking_decision(
            connection,
            run["project_id"],
            None,
            template["question"],
            template.get("recommendation") or {"action": "resume_workflow"},
            template.get("cost_summary") or {},
            f"Workflow {run['id']} is waiting at decision step {row['step_id']}.",
            risk_tier=int(template.get("risk_tier", 2)),
            default_action=str(template.get("default_action", "hold")),
        )
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='waiting', decision_id=:decision_id, updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": row["id"], "decision_id": decision_id},
        )
        connection.execute(
            text("UPDATE workflow_runs SET status='waiting', updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        _emit_step_event(
            connection,
            run,
            "workflow.step_waiting_decision",
            _step_row(connection, run["id"], row["step_id"], iteration),
            {"decision_id": decision_id},
        )
        return
    if kind == "subworkflow":
        binding = template["binding"]
        child = start_run(
            connection,
            project_id=run["project_id"],
            actor=run["actor"],
            input_data=template.get("input") or {},
            dedupe_key=f"subworkflow:{run['id']}:{row['step_id']}:{iteration}",
            workflow_version_id=binding["workflow_version_id"],
            policy_snapshot=_json(run["policy_snapshot"], {}),
            parent_run_id=run["id"],
        )
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='waiting', child_workflow_run_id=:child_id, updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": row["id"], "child_id": child["id"]},
        )
        connection.execute(
            text("UPDATE workflow_runs SET status='waiting', updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        return


def advance_run(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] in TERMINAL_RUN_STATES or run["status"] in {"paused", "compensating"}:
        return get_run(connection, project_id, run_id)
    waiting = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND status='waiting'
            ORDER BY created_at, iteration
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    spec = _run_spec(connection, run_id)
    templates = _step_map(spec)
    progressed = False
    for step in waiting:
        template = templates[step["step_id"]]
        if step["step_kind"] == "timer" and step["run_after"] <= now_utc():
            _succeed_internal(
                connection,
                run_id,
                step["step_id"],
                int(step["iteration"]),
                {"woke_at": now_utc().isoformat()},
            )
            _after_success(connection, run_id, step["step_id"], int(step["iteration"]))
            progressed = True
        elif step["step_kind"] == "decision" and step.get("decision_id"):
            decision = connection.execute(
                text("SELECT status FROM decisions WHERE id=:id"),
                {"id": step["decision_id"]},
            ).mappings().first()
            if decision and decision["status"] == "approved":
                _succeed_internal(
                    connection,
                    run_id,
                    step["step_id"],
                    int(step["iteration"]),
                    {"decision_id": step["decision_id"], "outcome": "approved"},
                )
                _after_success(connection, run_id, step["step_id"], int(step["iteration"]))
                progressed = True
            elif decision and decision["status"] in {"rejected", "expired"}:
                _block_run(
                    connection,
                    run_id,
                    f"decision {step['decision_id']} was {decision['status']}",
                )
                progressed = True
        elif step["step_kind"] == "subworkflow" and step.get("child_workflow_run_id"):
            child = _run_row(connection, step["child_workflow_run_id"])
            if child["status"] == "completed":
                _succeed_internal(
                    connection,
                    run_id,
                    step["step_id"],
                    int(step["iteration"]),
                    {"child_workflow_run_id": child["id"], "output": _json(child["output"], {})},
                )
                _after_success(connection, run_id, step["step_id"], int(step["iteration"]))
                progressed = True
            elif child["status"] in {"failed", "deadlettered", "cancelled"}:
                _block_run(
                    connection,
                    run_id,
                    f"subworkflow {child['id']} ended in {child['status']}",
                )
                progressed = True
        # Loop waiting steps resume only when their body completes.
        _ = template
    if progressed:
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='running', updated_at=now()
                WHERE id=:id AND status='waiting'
                """
            ),
            {"id": run_id},
        )
    _drain_control_steps(connection, run_id)
    _maybe_finish_run(connection, run_id)
    return get_run(connection, project_id, run_id)


def _refresh_frontier(connection: Connection, run_id: str) -> None:
    rows = connection.execute(
        text(
            """
            SELECT id, step_id, step_kind, iteration, status, lease_owner,
                   lease_expires_at, run_after, decision_id, child_workflow_run_id
            FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
              AND status IN ('ready', 'leased', 'running', 'waiting')
            ORDER BY created_at, iteration, step_id
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    frontier = [
        {
            "id": row["id"],
            "step_id": row["step_id"],
            "step_kind": row["step_kind"],
            "iteration": int(row["iteration"]),
            "status": row["status"],
            "lease_owner": row.get("lease_owner"),
            "lease_expires_at": _iso(row.get("lease_expires_at")),
            "run_after": _iso(row.get("run_after")),
            "decision_id": row.get("decision_id"),
            "child_workflow_run_id": row.get("child_workflow_run_id"),
        }
        for row in rows
    ]
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET current_frontier=CAST(:frontier AS JSONB), updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": run_id, "frontier": json.dumps(frontier)},
    )


def _maybe_finish_run(connection: Connection, run_id: str) -> None:
    run = _run_row(connection, run_id)
    if run["status"] not in {"running", "waiting"}:
        _refresh_frontier(connection, run_id)
        return
    counts = connection.execute(
        text(
            """
            SELECT status, count(*) AS count FROM workflow_step_runs
            WHERE workflow_run_id=:run_id GROUP BY status
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    by_status = {row["status"]: int(row["count"]) for row in counts}
    if any(by_status.get(status, 0) for status in ACTIVE_STEP_STATES):
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET status=CASE
                    WHEN EXISTS (
                      SELECT 1 FROM workflow_step_runs
                      WHERE workflow_run_id=:id AND status IN ('ready','leased','running')
                    ) THEN 'running' ELSE 'waiting' END,
                    updated_at=now()
                WHERE id=:id AND status IN ('running','waiting')
                """
            ),
            {"id": run_id},
        )
        _refresh_frontier(connection, run_id)
        return
    # Pending rows at this point are unreachable paths.  Record them as skipped
    # before completing so no step disappears silently.
    pending = connection.execute(
        text(
            "SELECT step_id FROM workflow_step_runs WHERE workflow_run_id=:id AND status='pending'"
        ),
        {"id": run_id},
    ).scalars().all()
    for step_id in pending:
        _mark_skipped(connection, run_id, step_id, "path_not_reached")
    connection.execute(
        text(
            """
            UPDATE workflow_runs
            SET status='completed', output=context, current_frontier='[]'::jsonb,
                completed_at=now(), updated_at=now()
            WHERE id=:id AND status IN ('running','waiting')
            """
        ),
        {"id": run_id},
    )
    emit_event(
        connection,
        run["project_id"],
        "workflow.run_completed",
        run["actor"],
        "workflow_run",
        run_id,
        {"status_counts": by_status},
    )


def _block_run(connection: Connection, run_id: str, reason: str) -> None:
    run = _run_row(connection, run_id)
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='blocked', blocked_reason=:reason, updated_at=now()
            WHERE id=:id AND status NOT IN ('completed','cancelled','compensated')
            """
        ),
        {"id": run_id, "reason": reason},
    )
    emit_event(
        connection,
        run["project_id"],
        "workflow.run_blocked",
        run["actor"],
        "workflow_run",
        run_id,
        {"reason": reason},
    )


def claim_step(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> Optional[dict[str, Any]]:
    if lease_seconds < 5 or lease_seconds > 3600:
        raise WorkflowValidationError("lease_seconds must be between 5 and 3600.")
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] not in {"running", "waiting"}:
        return None
    _drain_control_steps(connection, run_id)
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
              AND step_kind NOT IN (
                'condition','parallel','join','bounded_loop','decision','timer','subworkflow'
              )
              AND run_after <= now()
              AND (
                status='ready'
                OR (status IN ('leased','running') AND lease_expires_at < now())
              )
            ORDER BY run_after, created_at, iteration, step_id
            FOR UPDATE SKIP LOCKED LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        return None
    reclaimed = row["status"] in {"leased", "running"}
    if reclaimed:
        _emit_step_event(
            connection,
            run,
            "workflow.step_lease_reclaimed",
            row,
            {"previous_owner": row.get("lease_owner")},
        )
    expires_at = now_utc() + timedelta(seconds=lease_seconds)
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='leased', attempt=attempt+1, lease_owner=:worker_id,
                lease_expires_at=:expires_at, heartbeat_at=now(),
                started_at=COALESCE(started_at, now()), updated_at=now()
            WHERE id=:id
            RETURNING *
            """
        ),
        {"id": row["id"], "worker_id": worker_id, "expires_at": expires_at},
    ).mappings().one()
    connection.execute(
        text("UPDATE workflow_runs SET status='running', updated_at=now() WHERE id=:id"),
        {"id": run_id},
    )
    _emit_step_event(
        connection,
        run,
        "workflow.step_leased",
        updated,
        {"worker_id": worker_id, "lease_expires_at": expires_at.isoformat()},
    )
    _refresh_frontier(connection, run_id)
    return _serialize_step(updated)


def heartbeat_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> dict[str, Any]:
    expires_at = now_utc() + timedelta(seconds=lease_seconds)
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='running', heartbeat_at=now(), lease_expires_at=:expires_at,
                updated_at=now()
            WHERE id=:id AND project_id=:project_id AND lease_owner=:worker_id
              AND status IN ('leased','running') AND lease_expires_at >= now()
            RETURNING *
            """
        ),
        {
            "id": step_run_id,
            "project_id": project_id,
            "worker_id": worker_id,
            "expires_at": expires_at,
        },
    ).mappings().first()
    if not row:
        raise WorkflowLeaseError("Workflow step lease is missing, expired, or owned by another worker.")
    _refresh_frontier(connection, row["workflow_run_id"])
    return _serialize_step(row)


def force_complete_waiting_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Complete a step that was PARKED, not leased.

    `complete_step` requires the caller to hold the lease, which is right for work a worker is
    executing. A parked step deliberately holds none — nothing is running, it is waiting for a
    build that may take twenty minutes, and a lease held that long would be reclaimed as a
    crashed worker. So its completion is a separate door, and it only opens for a step actually
    in `waiting`.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', output=CAST(:output AS JSONB),
                completed_at=now(), updated_at=now()
            WHERE id=:id AND project_id=:project_id AND status='waiting'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id, "output": json.dumps(output)},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Waiting workflow step not found.")
    connection.execute(
        text("UPDATE workflow_runs SET status='running', updated_at=now() "
             "WHERE id=:id AND status='waiting'"),
        {"id": row["workflow_run_id"]},
    )
    # EVERYTHING `complete_step` DOES AFTER THE UPDATE, because a step finishing is more than a
    # status. The first version wrote the row and stopped, so the next step was never scheduled:
    # the run went straight to `completed` with the following step marked `skipped`. A build
    # that finished correctly still lost the deploy that was supposed to follow it.
    if output:
        # Into the run context, which is where `{"from_step": ..., "field": ...}` reads from.
        # Without this "deploy the thing you just built" resolves to nothing.
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET context=context || CAST(:output AS JSONB), updated_at=now()
                WHERE id=:run_id
                """
            ),
            {"run_id": row["workflow_run_id"], "output": json.dumps(output)},
        )
    _emit_step_event(
        connection, _run_row(connection, row["workflow_run_id"]),
        "workflow.step_succeeded", row, {"awaited": True},
    )
    _after_success(
        connection, row["workflow_run_id"], row["step_id"], int(row["iteration"]),
    )
    return _serialize_step(row)


def force_fail_waiting_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    error: str,
) -> dict[str, Any]:
    """Fail a parked step because the work it was waiting for failed.

    Not retried: the retry belongs to the thing that failed, not to the act of waiting for it.
    Re-running this step would start a SECOND build rather than reconsider the first.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='failed', last_error=:error, completed_at=now(), updated_at=now()
            WHERE id=:id AND project_id=:project_id AND status='waiting'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id, "error": error[:500]},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Waiting workflow step not found.")
    connection.execute(
        text("UPDATE workflow_runs SET status='failed', error=:error, updated_at=now() "
             "WHERE id=:id"),
        {"id": row["workflow_run_id"], "error": error[:500]},
    )
    return _serialize_step(row)


def repoint_waiting_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    handle: dict[str, Any],
    note: str,
) -> dict[str, Any]:
    """Move a parked step onto a LATER attempt at the same work.

    A step parks against one id. Retrying a build starts a fresh attempt with a new id, so the
    step went on waiting for the corpse — the operator watched a new build run to completion
    while the workflow above it read "failed — the code_task it started finished as
    'cancelled'", stuck at 3 of 8 permanently.

    The step stays WAITING. Nothing about it has succeeded or failed; it is waiting for
    different work, and the note records the substitution so the operator can see why the id
    under the step changed rather than finding a stranger there.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET output = COALESCE(output, '{}'::jsonb)
                         || jsonb_build_object('await', CAST(:handle AS JSONB),
                                               'awaited_note', CAST(:note AS TEXT),
                                               'code_task_run_id', CAST(:new_id AS TEXT)),
                updated_at = now()
            WHERE id=:id AND project_id=:project_id AND status='waiting'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id,
         "handle": json.dumps(handle), "note": note[:500],
         "new_id": str(handle.get("id") or "")},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Waiting workflow step not found.")
    # The RUN goes back to waiting too. A previous attempt may have marked it failed before the
    # retry existed, and leaving it failed above a step that is actively waiting is the same
    # contradiction in the other direction.
    connection.execute(
        text("UPDATE workflow_runs SET status='waiting', error=NULL, updated_at=now() "
             "WHERE id=:id"),
        {"id": row["workflow_run_id"]},
    )
    return _serialize_step(row)


def reopen_failed_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    handle: dict[str, Any],
    note: str,
) -> dict[str, Any]:
    """Put a step that failed with its build back to waiting, on the retry.

    The sequence that actually happens: the build dies, the step fails, and THEN the operator
    presses retry. So the recovery has to reach a step that has already been given its verdict —
    otherwise the operator watches a new build run to completion while the workflow above it
    reads "failed", permanently, at 3 of 8.

    Narrow on purpose. Only a step whose awaited work has a LIVE later attempt is reopened, only
    the await handle and the status change, and the note records the substitution — a step that
    silently changed which run it was watching would be worse than the stuck one.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting', last_error=NULL, completed_at=NULL,
                lease_owner=NULL, lease_expires_at=NULL,
                output = COALESCE(output, '{}'::jsonb)
                         || jsonb_build_object('await', CAST(:handle AS JSONB),
                                               'awaited_note', CAST(:note AS TEXT),
                                               'code_task_run_id', CAST(:new_id AS TEXT)),
                updated_at = now()
            WHERE id=:id AND project_id=:project_id AND status='failed'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id,
         "handle": json.dumps(handle), "note": note[:500],
         "new_id": str(handle.get("id") or "")},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Failed workflow step not found.")
    # The run was marked failed when the step was. It is waiting again now, and leaving it
    # failed above an actively-waiting step is the same contradiction the other way round.
    connection.execute(
        text("UPDATE workflow_runs SET status='waiting', error=NULL, updated_at=now() "
             "WHERE id=:id"),
        {"id": row["workflow_run_id"]},
    )
    return _serialize_step(row)


def park_step_awaiting(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Park a step that STARTED long-running work and must not be called done.

    An async capability's tool returns as soon as the work is accepted: create_code_task hands
    back a run id in milliseconds for a build that takes twenty minutes. Completing the step on
    that return made the workflow race past `build` into `review` and `test` while the build was
    still cloning — every run finishing in seconds, with a shape that looked like success.

    So the step goes to `waiting`, holding the id it is waiting on, and the runner completes it
    when the underlying work reaches a terminal state. The lease is released because nothing is
    executing: a lease held for twenty minutes would be reclaimed as a crashed worker.
    """
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting', output=CAST(:output AS JSONB),
                lease_owner=NULL, lease_expires_at=NULL,
                started_at=COALESCE(started_at, now()), updated_at=now()
            WHERE id=:id
            RETURNING *
            """
        ),
        {"id": step_run_id, "output": json.dumps(output)},
    ).mappings().first()
    connection.execute(
        text("UPDATE workflow_runs SET status='waiting', updated_at=now() WHERE id=:id"),
        {"id": row["workflow_run_id"]},
    )
    return _serialize_step(updated)


def complete_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    output: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] == "succeeded":
        return _serialize_step(row)
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    if row["lease_expires_at"] and row["lease_expires_at"] < now_utc():
        raise WorkflowLeaseError("Workflow step lease has expired.")
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', output=CAST(:output AS JSONB),
                evidence=CAST(:evidence AS JSONB), lease_owner=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=now(), updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {
            "id": step_run_id,
            "output": json.dumps(output or {}),
            "evidence": json.dumps(evidence or []),
        },
    ).mappings().one()
    if output:
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET context=context || CAST(:output AS JSONB), updated_at=now()
                WHERE id=:run_id
                """
            ),
            {"run_id": row["workflow_run_id"], "output": json.dumps(output)},
        )
    run = _run_row(connection, row["workflow_run_id"])
    _emit_step_event(
        connection,
        run,
        "workflow.step_succeeded",
        updated,
        {"evidence_count": len(evidence or [])},
    )
    spec = _run_spec(connection, row["workflow_run_id"])
    template = _step_map(spec)[row["step_id"]]
    compensation = template.get("compensation")
    if compensation:
        sequence = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(max(sequence), -1) + 1
                    FROM workflow_compensation_runs WHERE workflow_run_id=:run_id
                    """
                ),
                {"run_id": row["workflow_run_id"]},
            ).scalar_one()
        )
        connection.execute(
            text(
                """
                INSERT INTO workflow_compensation_runs (
                    id, project_id, workflow_run_id, workflow_step_run_id,
                    sequence, status, action, dedupe_key, max_attempts
                ) VALUES (
                    :id, :project_id, :run_id, :step_run_id, :sequence,
                    'pending', CAST(:action AS JSONB), :dedupe_key, :max_attempts
                ) ON CONFLICT (project_id, dedupe_key) DO NOTHING
                """
            ),
            {
                "id": make_id("wfc"),
                "project_id": project_id,
                "run_id": row["workflow_run_id"],
                "step_run_id": row["id"],
                "sequence": sequence,
                "action": json.dumps(compensation),
                "dedupe_key": f"workflow-compensate:{row['id']}",
                "max_attempts": int(compensation.get("max_attempts", 3)),
            },
        )
    _after_success(
        connection,
        row["workflow_run_id"],
        row["step_id"],
        int(row["iteration"]),
    )
    return _serialize_step(updated)


def fail_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    error: str,
    retryable: bool = True,
    retry_delay_seconds: int = 0,
    #: Whatever the capability returned. Kept because a failed step stored `{}` and the only
    #: record of what happened was one sentence — which, on the deploy gate that prompted this,
    #: read "the capability reported failure without a reason". Evidence outlives the summary.
    output: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    can_retry = retryable and int(row["attempt"]) < int(row["max_attempts"])
    status = "ready" if can_retry else "deadlettered"
    run_after = now_utc() + timedelta(seconds=max(0, int(retry_delay_seconds)))
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status=:status, last_error=:error, run_after=:run_after,
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=CASE WHEN :terminal THEN now() ELSE NULL END,
                -- MERGED, not replaced: a step that parked and then failed keeps the await
                -- handle it was waiting on, which is how anything can be reconstructed later.
                output = COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB),
                updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {
            "id": row["id"],
            "status": status,
            "error": _clean_text(error, "error"),
            "run_after": run_after,
            "terminal": not can_retry,
            "output": json.dumps(output or {}),
        },
    ).mappings().one()
    run = _run_row(connection, row["workflow_run_id"])
    _emit_step_event(
        connection,
        run,
        "workflow.step_retry_scheduled" if can_retry else "workflow.step_deadlettered",
        updated,
        {"error": error, "retryable": can_retry, "run_after": run_after.isoformat()},
    )
    if not can_retry:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, 'workflow_step', :source_id, :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": make_id("dlq"),
                "project_id": project_id,
                "source_id": row["id"],
                "reason": error,
                "payload": json.dumps(
                    {"workflow_run_id": row["workflow_run_id"], "step_id": row["step_id"]}
                ),
            },
        )
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='deadlettered', error=:error,
                    current_frontier='[]'::jsonb, updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": row["workflow_run_id"], "error": error},
        )
    _refresh_frontier(connection, row["workflow_run_id"])
    return _serialize_step(updated)


def pause_run(
    connection: Connection, project_id: str, run_id: str, actor: str
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] in TERMINAL_RUN_STATES:
        raise WorkflowConflictError("Terminal workflow runs cannot be paused.")
    if run["status"] != "paused":
        connection.execute(
            text(
                "UPDATE workflow_runs SET status='paused', paused_at=now(), updated_at=now() WHERE id=:id"
            ),
            {"id": run_id},
        )
        emit_event(
            connection,
            project_id,
            "workflow.run_paused",
            actor,
            "workflow_run",
            run_id,
            {},
        )
    return get_run(connection, project_id, run_id)


def resume_run(
    connection: Connection, project_id: str, run_id: str, actor: str
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "paused":
        raise WorkflowConflictError("Only paused workflow runs can resume.")
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='running', paused_at=NULL, updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": run_id},
    )
    emit_event(
        connection,
        project_id,
        "workflow.run_resumed",
        actor,
        "workflow_run",
        run_id,
        {},
    )
    return advance_run(connection, project_id, run_id)


def cancel_run(
    connection: Connection,
    project_id: str,
    run_id: str,
    actor: str,
    *,
    reason: str,
    compensate: bool = True,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] in TERMINAL_RUN_STATES:
        return get_run(connection, project_id, run_id)

    # STOP THE WORK, NOT JUST THE BOOKKEEPING.
    #
    # Cancelling used to mark the step rows and the run row and stop there, which cancels
    # nothing an operator would recognise as work: the code task the build step started keeps
    # running, on its own job, in its own sandbox.
    #
    # On 2026-09-02 that gap turned a cancelled verification run into a destroyed database. The
    # run was cancelled at 04:52 believing the build was stopped; the code task it had spawned
    # carried on to its QA step and, twenty minutes later, ran the API test suite against the
    # production DATABASE_URL it had inherited. 204 tables.
    #
    # "Cancel" has to mean the work stops. Anything else is a button that lies.
    cancelled_work = _cancel_work_this_run_started(connection, project_id, run_id, actor, reason)

    connection.execute(
        text(
            """
            UPDATE workflow_step_runs SET status='cancelled', completed_at=now(), updated_at=now()
            WHERE workflow_run_id=:run_id AND status IN ('pending','ready','waiting')
            """
        ),
        {"run_id": run_id},
    )
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='cancelled', error=:reason,
                current_frontier='[]'::jsonb, completed_at=now(), updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": run_id, "reason": reason},
    )
    emit_event(
        connection,
        project_id,
        "workflow.run_cancelled",
        actor,
        "workflow_run",
        run_id,
        {"reason": reason, "compensate": compensate},
    )
    if compensate:
        compensation_count = connection.execute(
            text(
                "SELECT count(*) FROM workflow_compensation_runs WHERE workflow_run_id=:run_id"
            ),
            {"run_id": run_id},
        ).scalar_one()
        if compensation_count:
            begin_compensation(connection, project_id, run_id, actor, reason=reason)
    return get_run(connection, project_id, run_id)


#: The long-running thing each async step starts, and the table that records its state. A step
#: parks against one of these; cancelling the run has to reach through to it.
_WORK_TABLES: dict[str, str] = {
    "code_task": "code_task_runs",
    "discovery": "discovery_runs",
    "meeting": "meeting_runs",
    "preview": "app_previews",
}

_WORK_TERMINAL: frozenset[str] = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "blocked", "stopped", "expired"}
)


#: What each kind of awaited work is called in the words the console already uses for the step
#: that starts it, so "waiting on ..." names something the operator can see on the same screen.
_AWAIT_LABELS: dict[str, str] = {
    "discovery": "the research to finish",
    "meeting": "the panel to finish",
    "code_task": "the build to finish",
    "preview": "the preview to come up",
}


def waiting_on_label(step: dict[str, Any]) -> Optional[str]:
    """What a waiting step is waiting FOR — or None when that is not recorded.

    THE BUG THIS REPLACES. The console said "waiting on a decision" for every waiting step
    that had no approver, because "a decision" was the fallback. A research step that had
    started a discovery run and was waiting for it to finish therefore told the operator to go
    and decide something that did not exist, on a run that was working perfectly:

        "waiting on a decision" — while drun_864aa33554b14eb0b541 was running normally.

    Sending someone to look for a decision that is not there is worse than saying nothing. It
    costs them the trip, and then it costs the console its credibility, because the next
    genuine "waiting on you" reads like noise.

    So the fallback is None: the status pill already says `waiting`, and silence is the honest
    answer to a question we cannot answer.
    """
    approver = (step.get("approval") or {}).get("approver_role")
    if approver:
        return str(approver)
    if step.get("decision_id"):
        return "a decision"
    awaited = (step.get("output") or {}).get("await")
    if isinstance(awaited, dict):
        label = _AWAIT_LABELS.get(str(awaited.get("kind") or "").strip())
        if label:
            return label
    return None


def _cancel_work_this_run_started(
    connection: Connection, project_id: str, run_id: str, actor: str, reason: str,
) -> list[dict[str, Any]]:
    """Stop the code tasks, discoveries, meetings and previews this run set going.

    Each async step records what it is waiting on in its output, as {"await": {kind, id}} — the
    same handle `resume_awaited` uses to complete it. That handle is the thread back to the real
    work, so cancellation follows it rather than guessing from step ids.

    Jobs are cancelled with force=True deliberately. The usual rule is that a running job is
    left alone because killing it leaves a half-written branch — but here an operator has said
    stop, and a half-written branch on an abandoned task is unambiguously better than an agent
    that keeps running after being cancelled. That is precisely the trade the incident settled.

    Best-effort per item, and never raises: a code task that cannot be stopped must not prevent
    the run from being cancelled, or "cancel" fails exactly when it is needed most.
    """
    # JobQueue HAS NEVER EXISTED HERE. `cancel_queued_job` is a method on JobLeaseService, so
    # this import raised ImportError the first time anybody cancelled a run — the worst moment
    # for a bad import, and directly above the comment promising that "cancel has to mean the
    # work stops. Anything else is a button that lies." It lied by 500.
    from ..config import get_settings
    from .jobs import JobLeaseService

    job_service = JobLeaseService(get_settings())
    stopped: list[dict[str, Any]] = []
    rows = connection.execute(
        text(
            """
            SELECT step_id, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND status IN ('waiting', 'running', 'leased')
            """
        ),
        {"run_id": run_id},
    ).mappings().all()

    for row in rows:
        output = _json(row["output"], {}) or {}
        awaited = output.get("await") if isinstance(output.get("await"), dict) else None
        if not awaited:
            continue
        kind = str(awaited.get("kind") or "")
        work_id = str(awaited.get("id") or "").strip()
        if not work_id or kind not in _WORK_TABLES:
            continue

        # The job first: that is the thing with a process behind it.
        try:
            jobs = connection.execute(
                text(
                    """
                    SELECT id FROM jobs
                    WHERE payload->>'run_id' = :work_id
                      AND status NOT IN ('succeeded','cancelled','deadlettered')
                    """
                ),
                {"work_id": work_id},
            ).scalars().all()
            for job_id in jobs:
                job_service.cancel_queued_job(
                    connection, job_id, force=True, actor=actor,
                    reason=f"workflow run {run_id} was cancelled: {reason}",
                )
        except Exception:  # noqa: BLE001 — a job that will not stop must not block the cancel
            logger.exception("could not cancel jobs for %s %s", kind, work_id)

        # Then the record, so the console and every resume path agree it is over.
        table = _WORK_TABLES[kind]
        try:
            connection.execute(
                text(
                    f"""
                    UPDATE {table} SET status='cancelled', updated_at=now()
                    WHERE id = :work_id AND status NOT IN :terminal
                    """
                ).bindparams(bindparam("terminal", expanding=True)),
                {"work_id": work_id, "terminal": sorted(_WORK_TERMINAL)},
            )
        except Exception:  # noqa: BLE001 — table shapes differ; the job cancel is what matters
            logger.exception("could not mark %s %s cancelled", kind, work_id)

        stopped.append({"step_id": row["step_id"], "kind": kind, "id": work_id})

    if stopped:
        emit_event(
            connection, project_id, "workflow.work_cancelled", actor, "workflow_run", run_id,
            {"stopped": stopped, "reason": reason},
        )
    return stopped


def begin_compensation(
    connection: Connection,
    project_id: str,
    run_id: str,
    actor: str,
    *,
    reason: str,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id AND status IN ('pending','ready')
            ORDER BY sequence DESC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    if not rows:
        raise WorkflowConflictError("Workflow run has no pending compensation actions.")
    connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs SET status='pending', updated_at=now()
            WHERE workflow_run_id=:run_id AND status='ready'
            """
        ),
        {"run_id": run_id},
    )
    connection.execute(
        text(
            "UPDATE workflow_compensation_runs SET status='ready', updated_at=now() WHERE id=:id"
        ),
        {"id": rows[0]["id"]},
    )
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='compensating', error=:reason,
                updated_at=now() WHERE id=:id
            """
        ),
        {"id": run_id, "reason": reason},
    )
    emit_event(
        connection,
        project_id,
        "workflow.compensation_started",
        actor,
        "workflow_run",
        run_id,
        {"reason": reason, "action_count": len(rows)},
    )
    return get_run(connection, project_id, run_id)


def claim_compensation(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> Optional[dict[str, Any]]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "compensating":
        return None
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id AND (
              status='ready' OR (status IN ('leased','running') AND lease_expires_at < now())
            ) ORDER BY sequence DESC FOR UPDATE SKIP LOCKED LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        return None
    updated = connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs
            SET status='leased', attempt=attempt+1, lease_owner=:worker_id,
                lease_expires_at=:expires_at, heartbeat_at=now(), updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {
            "id": row["id"],
            "worker_id": worker_id,
            "expires_at": now_utc() + timedelta(seconds=lease_seconds),
        },
    ).mappings().one()
    return _serialize_compensation(updated)


def complete_compensation(
    connection: Connection,
    *,
    project_id: str,
    compensation_id: str,
    worker_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": compensation_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow compensation run not found.")
    if row["status"] == "succeeded":
        return _serialize_compensation(row)
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Compensation is not leased by this worker.")
    updated = connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs
            SET status='succeeded', output=CAST(:output AS JSONB), lease_owner=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL, updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {"id": compensation_id, "output": json.dumps(output or {})},
    ).mappings().one()
    next_row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id AND status='pending'
            ORDER BY sequence DESC LIMIT 1
            """
        ),
        {"run_id": row["workflow_run_id"]},
    ).mappings().first()
    run = _run_row(connection, row["workflow_run_id"])
    if next_row:
        connection.execute(
            text(
                "UPDATE workflow_compensation_runs SET status='ready', updated_at=now() WHERE id=:id"
            ),
            {"id": next_row["id"]},
        )
    else:
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='compensated', completed_at=now(),
                    updated_at=now() WHERE id=:id
                """
            ),
            {"id": run["id"]},
        )
        emit_event(
            connection,
            project_id,
            "workflow.compensation_completed",
            run["actor"],
            "workflow_run",
            run["id"],
            {},
        )
    return _serialize_compensation(updated)


def fail_compensation(
    connection: Connection,
    *,
    project_id: str,
    compensation_id: str,
    worker_id: str,
    error: str,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": compensation_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow compensation run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Compensation is not leased by this worker.")
    retry = int(row["attempt"]) < int(row["max_attempts"])
    status = "ready" if retry else "deadlettered"
    updated = connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs
            SET status=:status, last_error=:error, lease_owner=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL, updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {"id": compensation_id, "status": status, "error": error},
    ).mappings().one()
    if not retry:
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='deadlettered',
                    error=:error, updated_at=now() WHERE id=:id
                """
            ),
            {"id": row["workflow_run_id"], "error": f"compensation failed: {error}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, 'workflow_compensation', :source_id,
                        :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": make_id("dlq"),
                "project_id": project_id,
                "source_id": compensation_id,
                "reason": error,
                "payload": json.dumps({"workflow_run_id": row["workflow_run_id"]}),
            },
        )
    return _serialize_compensation(updated)


def _serialize_compensation(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_run_id": row["workflow_run_id"],
        "workflow_step_run_id": row["workflow_step_run_id"],
        "sequence": int(row["sequence"]),
        "status": row["status"],
        "action": _json(row["action"], {}),
        "output": _json(row["output"], {}),
        "dedupe_key": row["dedupe_key"],
        "attempt": int(row["attempt"]),
        "max_attempts": int(row["max_attempts"]),
        "lease_owner": row.get("lease_owner"),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
        "last_error": row.get("last_error"),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def migrate_run(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    target_version_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "paused":
        raise WorkflowConflictError("Only paused workflow runs can migrate.")
    active = connection.execute(
        text(
            """
            SELECT count(*) FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND status IN ('leased','running')
            """
        ),
        {"run_id": run_id},
    ).scalar_one()
    if active:
        raise WorkflowConflictError("A paused run with active leases cannot migrate.")
    target = _version_row(connection, project_id, target_version_id)
    if target["workflow_definition_id"] != run["workflow_definition_id"]:
        raise WorkflowValidationError("Target version belongs to another workflow definition.")
    old_spec = _run_spec(connection, run_id)
    new_spec = validate_workflow_spec(_json(target["spec"], {}))
    old_steps = _step_map(old_spec)
    new_steps = _step_map(new_spec)
    rows = connection.execute(
        text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:run_id ORDER BY created_at"),
        {"run_id": run_id},
    ).mappings().all()
    incompatible: list[str] = []
    for row in rows:
        if row["status"] in {"succeeded", "ready", "waiting"}:
            target_step = new_steps.get(row["step_id"])
            if not target_step or target_step["kind"] != row["step_kind"]:
                incompatible.append(row["step_id"])
    if incompatible:
        raise WorkflowConflictError(
            "Target version is incompatible with completed/frontier steps: "
            + ", ".join(sorted(set(incompatible)))
        )
    snapshot = {
        "workflow_version_id": run["workflow_version_id"],
        "steps": [
            {"id": row["id"], "step_id": row["step_id"], "iteration": int(row["iteration"]), "status": row["status"]}
            for row in rows
        ],
    }
    migration_id = make_id("wfmg")
    connection.execute(
        text(
            """
            INSERT INTO workflow_migrations (
                id, project_id, workflow_run_id, from_version_id, to_version_id,
                status, compatibility, rollback_snapshot, reason, created_by
            ) VALUES (
                :id, :project_id, :run_id, :from_version_id, :to_version_id,
                'completed', CAST(:compatibility AS JSONB), CAST(:snapshot AS JSONB),
                :reason, :actor
            )
            """
        ),
        {
            "id": migration_id,
            "project_id": project_id,
            "run_id": run_id,
            "from_version_id": run["workflow_version_id"],
            "to_version_id": target_version_id,
            "compatibility": json.dumps({"compatible": True, "preserved_steps": len(rows)}),
            "snapshot": json.dumps(snapshot),
            "reason": _clean_text(reason, "reason"),
            "actor": actor,
        },
    )
    connection.execute(
        text(
            "UPDATE workflow_runs SET workflow_version_id=:version_id, updated_at=now() WHERE id=:id"
        ),
        {"version_id": target_version_id, "id": run_id},
    )
    existing_ids = {(row["step_id"], int(row["iteration"])) for row in rows}
    for step in new_spec["steps"]:
        if (step["id"], 0) not in existing_ids:
            _insert_step_run(
                connection,
                project_id=project_id,
                run_id=run_id,
                step=step,
                iteration=0,
                status="pending",
            )
    for step_id in set(old_steps) - set(new_steps):
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs SET status='cancelled', updated_at=now()
                WHERE workflow_run_id=:run_id AND step_id=:step_id AND status='pending'
                """
            ),
            {"run_id": run_id, "step_id": step_id},
        )
    emit_event(
        connection,
        project_id,
        "workflow.run_migrated",
        actor,
        "workflow_run",
        run_id,
        {
            "migration_id": migration_id,
            "from_version_id": run["workflow_version_id"],
            "to_version_id": target_version_id,
            "reason": reason,
        },
    )
    return get_run(connection, project_id, run_id)


def rollback_migration(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    migration_id: str,
    actor: str,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "paused":
        raise WorkflowConflictError("Only paused workflow runs can roll back migration.")
    migration = connection.execute(
        text(
            """
            SELECT * FROM workflow_migrations
            WHERE id=:id AND workflow_run_id=:run_id AND project_id=:project_id
            FOR UPDATE
            """
        ),
        {"id": migration_id, "run_id": run_id, "project_id": project_id},
    ).mappings().first()
    if not migration:
        raise WorkflowNotFoundError("Workflow migration not found.")
    if migration["status"] == "rolled_back":
        return get_run(connection, project_id, run_id)
    if migration["status"] != "completed" or run["workflow_version_id"] != migration["to_version_id"]:
        raise WorkflowConflictError("Migration is not the active completed migration.")
    snapshot = _json(migration["rollback_snapshot"], {})
    keep_ids = {item["id"] for item in snapshot.get("steps", [])}
    rows = connection.execute(
        text("SELECT id, status FROM workflow_step_runs WHERE workflow_run_id=:run_id"),
        {"run_id": run_id},
    ).mappings().all()
    new_rows = [row for row in rows if row["id"] not in keep_ids]
    if any(row["status"] not in {"pending", "cancelled", "skipped"} for row in new_rows):
        raise WorkflowConflictError("New-version steps have executed; migration rollback is unsafe.")
    if new_rows:
        connection.execute(
            text("DELETE FROM workflow_step_runs WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": tuple(row["id"] for row in new_rows)},
        )
    for item in snapshot.get("steps", []):
        connection.execute(
            text("UPDATE workflow_step_runs SET status=:status, updated_at=now() WHERE id=:id"),
            {"id": item["id"], "status": item["status"]},
        )
    connection.execute(
        text(
            "UPDATE workflow_runs SET workflow_version_id=:version_id, updated_at=now() WHERE id=:id"
        ),
        {"version_id": migration["from_version_id"], "id": run_id},
    )
    connection.execute(
        text(
            """
            UPDATE workflow_migrations SET status='rolled_back', rolled_back_by=:actor,
                rolled_back_at=now() WHERE id=:id
            """
        ),
        {"id": migration_id, "actor": actor},
    )
    emit_event(
        connection,
        project_id,
        "workflow.migration_rolled_back",
        actor,
        "workflow_run",
        run_id,
        {"migration_id": migration_id, "restored_version_id": migration["from_version_id"]},
    )
    return get_run(connection, project_id, run_id)


def _serialize_migration(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_run_id": row["workflow_run_id"],
        "from_version_id": row["from_version_id"],
        "to_version_id": row["to_version_id"],
        "status": row["status"],
        "compatibility": _json(row["compatibility"], {}),
        "reason": row["reason"],
        "created_by": row["created_by"],
        "rolled_back_by": row.get("rolled_back_by"),
        "created_at": _iso(row["created_at"]),
        "rolled_back_at": _iso(row.get("rolled_back_at")),
    }


def seed_specpack_workflows(
    connection: Connection,
    *,
    project_id: str,
    workflow_root: Path,
    actor: str = "bootstrap",
) -> list[dict[str, Any]]:
    """Register the shipped YAML workflow contracts as immutable runtime versions.

    The historical specpack schema describes ordered ``phases`` rather than
    executable runtime ``steps``.  The adapter preserves the complete source
    contract as metadata while producing a conservative, sequential task graph;
    future native runtime specs can use ``entry_step``/``steps`` directly.
    """
    registered: list[dict[str, Any]] = []
    for path in sorted(workflow_root.glob("*.yaml")):
        source = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            continue
        workflow_key = str(source.get("id") or path.stem).strip()
        definition_row = connection.execute(
            text(
                """
                SELECT * FROM workflow_definitions
                WHERE project_id=:project_id AND workflow_key=:workflow_key
                """
            ),
            {"project_id": project_id, "workflow_key": workflow_key},
        ).mappings().first()
        if definition_row:
            definition = _serialize_definition(definition_row)
        else:
            definition = create_definition(
                connection,
                project_id=project_id,
                workflow_key=workflow_key,
                name=str(source.get("name") or workflow_key.replace("_", " ").title()),
                description=source.get("description") or source.get("purpose"),
                actor=actor,
            )

        if isinstance(source.get("steps"), list) and source.get("entry_step"):
            runtime_spec = source
        else:
            raw_phases = source.get("phases") or source.get("states") or ["contract"]
            phases = raw_phases if isinstance(raw_phases, list) else ["contract"]
            steps: list[dict[str, Any]] = []
            used_ids: set[str] = set()
            for index, phase in enumerate(phases):
                raw_id = phase.get("id") if isinstance(phase, dict) else phase
                base_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw_id or f"phase-{index + 1}"))
                base_id = base_id.strip("-.") or f"phase-{index + 1}"
                step_id = base_id
                suffix = 2
                while step_id in used_ids:
                    step_id = f"{base_id}-{suffix}"
                    suffix += 1
                used_ids.add(step_id)
                step: dict[str, Any] = {
                    "id": step_id,
                    "kind": "task",
                    "input": {
                        "source_phase": phase,
                        "source_file": path.name,
                    },
                }
                steps.append(step)
            for current, following in zip(steps, steps[1:]):
                current["next"] = [following["id"]]
            runtime_spec = {
                "entry_step": steps[0]["id"],
                "steps": steps,
                "source_contract": source,
                "source_file": path.name,
                "source_schema": "specpack.workflow.v0",
            }
        version = publish_version(
            connection,
            project_id=project_id,
            definition_id=definition["id"],
            spec=runtime_spec,
            actor=actor,
            activate=True,
        )
        registered.append(
            {
                "workflow_definition_id": definition["id"],
                "workflow_version_id": version["id"],
                "workflow_key": workflow_key,
                "duplicate": bool(version.get("duplicate")),
                "source_file": path.name,
            }
        )
    return registered


#: THE SELECTOR THE PRODUCT ALREADY USES.
#:
#: `POST /v1/delivery-workflows/apply` — the "use this workflow for this kind of work" action in
#: the Workflows area — creates mappings with `selector_kind="work_class"`. My first version of
#: this resolver invented `selector_kind="code_task"` instead, which nothing writes: applying a
#: workflow in the console would have had no effect on what a project resolved to, and the two
#: halves of the same feature would have been permanently unable to see each other.
#:
#: A code task is the build of a product, so it resolves against the same work classes the
#: catalogue declares. `'*'` is not used here — the catalogue's own wildcard is `"any"`.
CODE_TASK_SELECTOR_KIND = "work_class"

#: What a code task counts AS, in work-class terms. `project.new` is what the catalogue gives
#: Standard Product Delivery, and `any` is the catalogue's own wildcard — tried in order, so a
#: workflow applied specifically to new projects beats one applied to everything.
CODE_TASK_WORK_CLASSES = ("project.new", "any")


def workflow_for_code_task(
    connection: Connection,
    *,
    project_id: str,
    task_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Which workflow governs this code task, or None when nothing maps to it.

    The workflow runtime and the coding pipeline were built as separate systems: 54 definitions
    existed, one workflow run had ever been started, and no code task had ever recorded which
    workflow it belonged to — there was nowhere to record it. So the console could show a
    catalogue of workflows and a list of runs with no way to say that either had anything to do
    with the other.

    This is the first half of the join: the fact, resolved and written down at creation. It
    does NOT change how a run executes — the pipeline's phase sequence is still its own — and
    saying otherwise on screen would be worse than saying nothing.

    Returns None rather than raising. A project with no mapping is the normal state today, and
    an unmapped task must still run.
    """
    selected: dict[str, Any] = {}
    for work_class in CODE_TASK_WORK_CLASSES:
        try:
            resolution = resolve_mapping(
                connection,
                project_id=project_id,
                selector_kind=CODE_TASK_SELECTOR_KIND,
                selector_value=work_class,
            )
        except Exception:  # noqa: BLE001 — a missing link must never stop a task starting
            return None
        if resolution.get("status") == "selected":
            selected = resolution.get("selected") or {}
            break
    version_id = selected.get("workflow_version_id")
    if not version_id:
        return _default_workflow(connection, project_id)
    row = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, d.name, v.version
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.id = v.workflow_definition_id
            WHERE v.id = :version_id
            """
        ),
        {"version_id": version_id},
    ).mappings().first()
    if not row:
        return None
    return {
        "definition_id": row["definition_id"],
        "version_id": version_id,
        "mapping_id": selected.get("id"),
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "version": row["version"],
        # A mapping somebody made, as opposed to the fallback below. The console says which,
        # because "this project was deliberately put on this workflow" and "nothing was chosen
        # so it got the standard one" are different facts and only one of them is a decision.
        "source": "mapping",
    }


#: The workflow a project gets when nobody has chosen one. `key` is stable across fleets by
#: design, which is what makes a default safe to hard-code here.
DEFAULT_WORKFLOW_KEY = "standard_product_delivery"


def _default_workflow(
    connection: Connection, project_id: str
) -> Optional[dict[str, Any]]:
    """The standard workflow, when no mapping selects one.

    Every project is unmapped until somebody maps it, so without this the answer to "which
    workflow governs my project" would be "none" for every project that has ever existed —
    technically true and useless. A new project is on the standard path unless told otherwise,
    which is what the catalogue is FOR.

    Marked `source: "default"` and NOT written as a mapping. Inventing a mapping row on the
    customer's behalf would make an inferred fallback indistinguishable from a choice they
    made, and the next person to read the mappings table could not tell which was which.
    """
    row = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, d.name, v.id AS version_id, v.version
            FROM workflow_definitions d
            JOIN workflow_versions v ON v.workflow_definition_id = d.id
            WHERE d.project_id = :project_id
              AND d.workflow_key = :key
              AND d.status = 'active'
              AND v.status = 'published'
            ORDER BY v.version DESC
            LIMIT 1
            """
        ),
        {"project_id": project_id, "key": DEFAULT_WORKFLOW_KEY},
    ).mappings().first()
    if not row:
        return None
    return {
        "definition_id": row["definition_id"],
        "version_id": row["version_id"],
        "mapping_id": None,
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "version": row["version"],
        "source": "default",
    }


def workflow_by_version(
    connection: Connection, project_id: str, version_id: str
) -> Optional[dict[str, Any]]:
    """A workflow the operator PICKED, resolved from the version they picked.

    An explicit choice outranks a mapping and outranks the default: the person starting the
    work said which path it should follow, and nothing inferred later should quietly override
    that. Recorded as `source: "chosen"` so the console can say so.

    Scoped to the project on purpose — a version id from another project must not be usable by
    passing it in the payload.
    """
    row = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, d.name, v.id AS version_id, v.version
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.id = v.workflow_definition_id
            WHERE v.id = :version_id AND d.project_id = :project_id
            """
        ),
        {"version_id": version_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        return None
    return {
        "definition_id": row["definition_id"],
        "version_id": row["version_id"],
        "mapping_id": None,
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "version": row["version"],
        "source": "chosen",
    }


#: The work class a new project is. `start_project_run` resolves the project's workflow through
#: the same mapping the console's "apply" action writes, so what the operator chose in the
#: console is what a project actually runs.
PROJECT_WORK_CLASS = "project.new"


def start_project_run(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    title: str,
    directive: str,
    workflow_version_id: Optional[str] = None,
    extra_input: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Start the OUTER workflow for a project, and return the run.

    A project's life is research → panel → approve the spec → build → review → test → preview →
    deploy → monitor. Every one of those is a workflow step with a duration, a status and
    something it produced. Until now nothing started such a run for real work: the console
    created a code task directly, so the only thing anyone could watch was the coding
    pipeline's own internal phases — which are one step of the nine, and the step the operator
    cares least about.

    Returns None when the project has no workflow to run, rather than raising. A fleet that has
    not installed the catalogue must still be able to start work; it simply gets the old direct
    path and no outer run to watch.
    """
    resolved = None
    if workflow_version_id:
        resolved = workflow_by_version(connection, project_id, str(workflow_version_id))
    if resolved is None:
        resolved = workflow_for_code_task(connection, project_id=project_id, task_id=None)
    if not resolved or not resolved.get("version_id"):
        return None
    run_input = {
        "title": title,
        # `directive` is what the catalogue's first steps read: research seeds from it and the
        # panel debates it. It is the instructions, not the title — a title is a label, and a
        # panel handed a label has nothing to discuss.
        "directive": directive,
        **{k: v for k, v in (extra_input or {}).items() if v not in (None, "", [], {})},
    }
    return start_run(
        connection,
        project_id=project_id,
        actor=actor,
        workflow_version_id=str(resolved["version_id"]),
        input_data=run_input,
        # One outer run per project. Starting the same project twice — a double-click, a retry
        # of the request — must not produce two nine-step pipelines racing each other over one
        # repository.
        dedupe_key=f"project:{project_id}:{title[:80]}:{directive[:120]}",
    )
