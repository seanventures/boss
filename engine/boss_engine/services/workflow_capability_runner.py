"""Executes the capability steps of a customer's delivery workflow.

The runtime schedules steps, leases them and records their evidence; it has never executed
anything itself. Control steps it drains internally, and everything else has always been
claimed and completed by an outside caller. For customer workflows there was no such caller —
so this is it.

Three rules shape the whole file:

  * IT ONLY TOUCHES PRODUCT WORKFLOWS. BOSS's own thirty-three definitions are claimed by the
    machinery that has always claimed them. A runner that reached across the namespace could
    execute the governed coding pipeline twice.
  * IT DOES NOT REIMPLEMENT ANYTHING. Every capability dispatches to a tool BOSS already has,
    already governs by risk tier, and already runs from the chat agent and MCP. A second path
    to "deploy" is a second definition of what deploying means.
  * A GATE IS NOT WORK. A step that waits for a person is never executed here; it raises a
    decision and stays waiting, which is the runtime's existing mechanism.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflows
from .core import create_blocking_decision, emit_event
from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

log = logging.getLogger(__name__)

#: Long enough for a tool call that talks to GitHub or a sandbox, short enough that a runner
#: which dies does not strand the step for long. The runtime reclaims an expired lease.
LEASE_SECONDS = 120

#: Per tick. The point is steady progress across many runs, not draining one to completion
#: while the others wait.
MAX_STEPS_PER_TICK = 10


class CapabilityExecutionError(RuntimeError):
    """The capability could not be performed. Carries the reason the step will record."""


def _resolve_inputs(
    inputs: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Substitute references to earlier steps' outputs.

    `{"run_id": {"from_step": "build", "field": "code_task_run_id"}}` becomes the id that the
    build step actually produced. Without this a workflow could only ever pass literals, and
    "deploy the thing you just built" — the most ordinary sentence in delivery — would be
    impossible to express.
    """
    resolved: dict[str, Any] = {}
    for name, value in (inputs or {}).items():
        if isinstance(value, dict) and "from_input" in value:
            # What the run was STARTED with. A catalogue workflow cannot hard-code the thing
            # being built — "Standard Product Delivery" has to work for any directive — so its
            # first steps read from the run's input instead of a literal.
            field = str(value["from_input"])
            run_input = context.get("input") or {}
            if field not in run_input:
                raise CapabilityExecutionError(
                    f"this workflow was started without '{field}', which this step needs."
                )
            resolved[name] = run_input[field]
        elif isinstance(value, dict) and "from_step" in value:
            step_id = str(value["from_step"])
            field = str(value.get("field") or "")
            produced = (context.get("steps") or {}).get(step_id) or {}
            if field:
                if field not in produced:
                    raise CapabilityExecutionError(
                        f"step '{step_id}' did not produce '{field}', which this step needs."
                    )
                resolved[name] = produced[field]
            else:
                resolved[name] = produced
        else:
            resolved[name] = value
    return resolved


def _run_context(connection: Connection, run_id: str) -> dict[str, Any]:
    """What earlier steps in this run produced, keyed by step id."""
    rows = connection.execute(
        text(
            """
            SELECT step_id, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND status = 'succeeded'
            ORDER BY completed_at
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    steps: dict[str, Any] = {}
    for row in rows:
        output = row["output"]
        if isinstance(output, dict):
            steps[row["step_id"]] = output
    run_input = connection.execute(
        text("SELECT input FROM workflow_runs WHERE id=:r"), {"r": run_id}
    ).scalar()
    return {"steps": steps, "input": run_input if isinstance(run_input, dict) else {}}


def _tool_registry():
    """The same registry the chat agent and MCP use. Imported lazily so this module can be
    read (and unit-tested) without constructing the whole tool layer."""
    from ..tools.builtin import build_default_registry

    return build_default_registry()


def _invoke_tool(
    ctx: Any, tool_name: str, project_id: str, actor: str, args: dict[str, Any]
) -> dict[str, Any]:
    registry = _tool_registry()
    spec = {s.name: s for s in registry.specs()}.get(tool_name)
    if spec is None:
        raise CapabilityExecutionError(
            f"this BOSS does not have the '{tool_name}' tool installed."
        )
    result = spec.handler(ctx, project_id=project_id, actor=actor, args=args)
    if isinstance(result, dict) and result.get("ok") is False:
        raise CapabilityExecutionError(str(result.get("error") or f"{tool_name} failed."))
    return result if isinstance(result, dict) else {"result": result}


def execute_step(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Perform one capability. Returns the step's output.

    Raises CapabilityExecutionError with a sentence an operator can act on — the step records
    it, and "step failed" with no reason is the thing this exists to avoid.
    """
    binding = step.get("binding") or {}
    key = str(binding.get("capability") or "")
    if key not in CAPABILITY_KEYS:
        raise CapabilityExecutionError(f"unknown capability '{key}'.")
    capability = get_capability(key)

    if capability.is_gate:
        # Should never arrive here — gates are turned into decisions before they are claimable.
        raise CapabilityExecutionError(
            f"'{capability.label}' waits for a person and is not executable work."
        )

    inputs = _resolve_inputs(binding.get("inputs") or {}, context)

    if capability.tool is None:
        # Capabilities with no tool yet are honest no-ops that say so, rather than silently
        # succeeding as though the work had happened. `review`, `test` and `monitor` are
        # observed from the run they belong to rather than started separately.
        return {
            "capability": key,
            "status": "recorded",
            "note": f"'{capability.label}' is recorded from the work it accompanies.",
            "inputs": inputs,
        }

    output = _invoke_tool(ctx, capability.tool, project_id, actor, inputs)
    return {"capability": key, "tool": capability.tool, **output}


def _open_gate(
    connection: Connection, project_id: str, run: dict[str, Any], step_row: dict[str, Any]
) -> None:
    """Turn a gate step into a decision a person can answer, and leave it waiting."""
    spec_step = step_row.get("_spec") or {}
    binding = spec_step.get("binding") or {}
    capability = get_capability(str(binding.get("capability")))
    approval = spec_step.get("approval") or {}
    # Matches core.create_blocking_decision exactly: it takes a question and a recommendation,
    # returns the decision id, and defaults to HOLD — which is the behaviour a gate wants. A
    # gate that defaulted to "proceed" would be a gate in name only.
    decision_id = create_blocking_decision(
        connection,
        project_id,
        run.get("task_id"),
        f"{capability.label}: {run.get('workflow_key') or 'workflow'}",
        {
            "capability": capability.key,
            "step_id": step_row["step_id"],
            "workflow_run_id": run["id"],
            "approver_role": approval.get("approver_role"),
        },
        {"estimated_usd": 0.0},
        capability.summary,
        risk_tier=max(2, capability.risk_tier),
    )
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting', decision_id=:decision_id, updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": step_row["id"], "decision_id": decision_id},
    )
    emit_event(
        connection, project_id, "workflow.gate_opened", run.get("actor") or "workflow",
        "workflow_step_run", step_row["id"],
        {"capability": capability.key, "step_id": step_row["step_id"]},
    )


def tick(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    worker_id: str = "capability-runner",
    limit: int = MAX_STEPS_PER_TICK,
) -> dict[str, Any]:
    """Advance every product workflow run that has work ready.

    Returns what it did, per step, so a caller can log something more useful than a count.
    Never raises for a step-level failure: one broken workflow must not stop the others.
    """
    runs = connection.execute(
        text(
            """
            SELECT r.id, r.actor, r.status, d.workflow_key, d.namespace
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE r.project_id = :project_id
              AND r.status IN ('running', 'waiting')
              AND COALESCE(d.namespace, 'system') = 'product'
            ORDER BY r.updated_at
            LIMIT 50
            """
        ),
        {"project_id": project_id},
    ).mappings().all()

    performed: list[dict[str, Any]] = []
    for run in runs:
        if len(performed) >= limit:
            break
        run_id = run["id"]
        try:
            spec = workflows._run_spec(connection, run_id)
            spec_steps = {s["id"]: s for s in spec.get("steps", [])}

            # Gates first: they are not claimable work, and leaving them until after the claim
            # would let the runner treat a waiting approval as an idle step forever.
            gates = connection.execute(
                text(
                    """
                    SELECT * FROM workflow_step_runs
                    WHERE workflow_run_id=:run_id AND status='ready' AND step_kind='capability'
                      AND decision_id IS NULL
                    """
                ),
                {"run_id": run_id},
            ).mappings().all()
            for row in gates:
                spec_step = spec_steps.get(row["step_id"]) or {}
                key = str((spec_step.get("binding") or {}).get("capability") or "")
                if key in CAPABILITY_KEYS and get_capability(key).is_gate:
                    _open_gate(
                        connection, project_id, {**dict(run), "id": run_id},
                        {**dict(row), "_spec": spec_step},
                    )
                    performed.append(
                        {"run_id": run_id, "step_id": row["step_id"],
                         "capability": key, "outcome": "gate_opened"}
                    )

            claimed = workflows.claim_step(
                connection, project_id=project_id, run_id=run_id,
                worker_id=worker_id, lease_seconds=LEASE_SECONDS,
            )
            if claimed is None:
                continue
            if claimed.get("step_kind") != "capability":
                # Somebody else's step kind. Release it by letting the lease lapse rather than
                # failing it — this runner is not the only possible claimant.
                continue

            spec_step = spec_steps.get(claimed["step_id"]) or {}
            try:
                output = execute_step(
                    connection, ctx, project_id=project_id,
                    actor=run.get("actor") or "workflow",
                    step=spec_step, context=_run_context(connection, run_id),
                )
                workflows.complete_step(
                    connection, project_id=project_id, step_run_id=claimed["id"],
                    worker_id=worker_id, output=output,
                    evidence=[{
                        "kind": "capability",
                        "capability": output.get("capability"),
                        "tool": output.get("tool"),
                    }],
                )
                performed.append({
                    "run_id": run_id, "step_id": claimed["step_id"],
                    "capability": output.get("capability"), "outcome": "succeeded",
                })
            except CapabilityExecutionError as error:
                workflows.fail_step(
                    connection, project_id=project_id, step_run_id=claimed["id"],
                    worker_id=worker_id, error=str(error), retryable=True,
                )
                performed.append({
                    "run_id": run_id, "step_id": claimed["step_id"],
                    "outcome": "failed", "error": str(error),
                })
        except Exception as error:                 # noqa: BLE001 - one run must not stop the rest
            log.warning("capability runner skipped run %s (non-fatal): %s", run_id, error)
            performed.append({"run_id": run_id, "outcome": "skipped", "error": str(error)})

    return {"project_id": project_id, "performed": performed, "count": len(performed)}
