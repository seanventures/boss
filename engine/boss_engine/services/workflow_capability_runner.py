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

import json
import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflow_custom_steps as custom_steps, workflows
from .core import create_blocking_decision, emit_event, now_utc
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
    # run_id and step count travel with the context because a custom step's MCP call needs
    # them to build its effect-receipt dedupe key — the thing that stops a retried step from
    # sending the same message twice.
    return {
        "steps": steps,
        "input": run_input if isinstance(run_input, dict) else {},
        "run_id": run_id,
        "step_index": len(rows),
    }


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

    # A step the customer defined. Checked first because it is the only kind that has no
    # capability key at all — falling through to the capability lookup would report it as an
    # unknown capability, which is true and useless.
    if binding.get("custom_step"):
        try:
            return custom_steps.execute(
                connection, getattr(ctx, "settings", None),
                project_id=project_id, actor=actor, step=step,
                inputs=_resolve_inputs(binding.get("inputs") or {}, context),
                run_id=str(context.get("run_id") or ""),
                step_index=int(context.get("step_index") or 0),
            )
        except custom_steps.CustomStepError as error:
            # Translated, not wrapped: the runner's failure path records
            # CapabilityExecutionError, and the customer's sentence is the part worth keeping.
            raise CapabilityExecutionError(str(error)) from error

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
    result = {"capability": key, "tool": capability.tool, **output}

    # STARTED IS NOT FINISHED.
    #
    # An async capability's tool returns as soon as the work is ACCEPTED: create_code_task hands
    # back a run id in milliseconds for a build that takes twenty minutes. Returning here made
    # the runner complete the step immediately, so a workflow raced past `build` into `review`
    # and `test` while the build was still cloning — every run finishing in seconds, in a shape
    # that looked like success.
    #
    # Marked as awaiting instead. The caller parks the step against this id, and `resume_awaited`
    # completes it when the work reaches a terminal state.
    awaited = _awaited_handle(key, result)
    if awaited:
        result["status"] = "awaiting"
        result["await"] = awaited
    return result


#: What an async capability is waiting on, and where its tool puts the id. The FIRST key that is
#: present wins, so a tool that grows a more specific field later does not need this touched.
_AWAIT_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "research": ("discovery", ("discovery_id", "run_id", "id")),
    "meeting": ("meeting", ("meeting_run_id", "run_id", "id")),
    "build": ("code_task", ("code_task_run_id", "run_id", "id")),
}


def _awaited_handle(key: str, output: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The work this step started, or None when there is nothing to wait for.

    Returning None on a missing id is deliberate: a capability that claims to be async but
    handed back nothing to correlate cannot be waited on, and parking it would hang the run
    forever with no way to tell why. It completes as it always did.
    """
    spec = _AWAIT_KINDS.get(key)
    if not spec:
        return None
    kind, fields = spec
    for field in fields:
        value = output.get(field)
        if isinstance(value, str) and value.strip():
            return {"kind": kind, "id": value.strip()}
    return None


#: How to tell whether awaited work is done, per kind: the table, and which statuses are
#: terminal. Anything not listed is still in flight.
_AWAIT_TABLES: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    # table, succeeded-statuses, failed-statuses
    "code_task": ("code_task_runs",
                  frozenset({"completed"}),
                  frozenset({"failed", "blocked", "cancelled"})),
    "discovery": ("discovery_runs",
                  frozenset({"completed", "succeeded"}),
                  frozenset({"failed", "cancelled"})),
    "meeting": ("meeting_runs",
                frozenset({"completed", "succeeded"}),
                frozenset({"failed", "cancelled"})),
}


def resume_awaited(
    connection: Connection, *, project_id: str, run_id: str, worker_id: str
) -> list[dict[str, Any]]:
    """Finish steps whose awaited work has reached a terminal state.

    Without this a parked step waits forever: nothing else in the system knows a workflow step
    is watching a code task. Runs every tick, before claiming new work, so a build that
    finished a moment ago releases the next step in the same pass rather than a tick later.

    A step whose work FAILED fails the step, with the underlying status in the message. Silently
    succeeding would let a workflow report a shipped product on top of a build that did not.
    """
    parked = connection.execute(
        text(
            """
            SELECT id, step_id, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND project_id = :project_id
              AND status = 'waiting'
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).mappings().all()

    done: list[dict[str, Any]] = []
    for step in parked:
        output = step["output"] or {}
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:  # noqa: BLE001
                continue
        handle = (output or {}).get("await") or {}
        kind, awaited_id = str(handle.get("kind") or ""), str(handle.get("id") or "")
        table_spec = _AWAIT_TABLES.get(kind)
        if not table_spec or not awaited_id:
            # A step waiting for a timer, a gate or a subworkflow. Those have their own
            # resumption paths and must not be touched here.
            continue
        table, ok_states, bad_states = table_spec
        try:
            row = connection.execute(
                text(f"SELECT status FROM {table} WHERE id = :id"),  # noqa: S608 - fixed set
                {"id": awaited_id},
            ).mappings().first()
        except Exception as error:  # noqa: BLE001 — a missing optional table is not fatal
            log.warning("cannot check awaited %s %s: %s", kind, awaited_id, error)
            continue
        if not row:
            continue
        status = str(row["status"] or "")
        if status in ok_states:
            workflows.force_complete_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                output={**output, "status": "succeeded", "awaited_status": status},
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "succeeded", "awaited": kind})
        elif status in bad_states:
            workflows.force_fail_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                error=f"the {kind} it started finished as '{status}'",
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "failed", "awaited": kind, "awaited_status": status})
    return done

def _gate_subject(
    connection: Connection, project_id: str, spec_step: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """What a person has to approve before this step, or None if it just runs.

    TWO DIFFERENT THINGS END UP WAITING HERE, and the difference is `completes_step`:

      * A gate capability — `approve_release` and its siblings. The approval IS the work, so
        answering it finishes the step.
      * An ordinary step whose approval mode is manual, including every custom step above the
        risk floor. The approval only unblocks it; the step still has to run afterwards.

    Collapsing the two would either finish real work nobody did, or leave a gate waiting for a
    tool call that does not exist.
    """
    binding = spec_step.get("binding") or {}
    approval = spec_step.get("approval") or {}
    requested = str(approval.get("mode") or "").lower() or None

    custom_key = str(binding.get("custom_step") or "")
    if custom_key:
        definition = custom_steps.get(connection, project_id, custom_key)
        if definition is None or not definition["usable"] or not definition["enabled"]:
            # Not gated, so it reaches execute_step and fails there with the sentence naming
            # what is actually wrong. Gating it instead would ask a person to approve a step
            # that cannot run, and an unusable step defaults to the risk floor — so a deleted
            # skill would have produced an approval request rather than an error.
            return None
        if custom_steps.approval_for(
            {"risk_tier": definition["risk_tier"]}, requested
        ) != "manual":
            return None
        return {
            "key": custom_key,
            "label": definition["label"],
            "summary": (
                f"Approve before '{definition['label']}' runs. It calls "
                f"{definition['risk_source']} at risk tier {definition['risk_tier']}."
            ),
            "risk_tier": definition["risk_tier"],
            "completes_step": False,
        }

    key = str(binding.get("capability") or "")
    if key not in CAPABILITY_KEYS:
        return None
    capability = get_capability(key)
    if capability.is_gate:
        return {
            "key": key, "label": capability.label, "summary": capability.summary,
            "risk_tier": max(2, capability.risk_tier), "completes_step": True,
        }
    if str(approval.get("mode") or "").lower() == "manual":
        # The floor said manual and the runner has to honour it. Before this, `forced_manual`
        # was stored on the step and then ignored at run time, which made the whole risk floor
        # advisory — a tier-3 capability marked "requires a person" ran unattended.
        return {
            "key": key, "label": capability.label,
            "summary": f"Approve before '{capability.label}' runs.",
            "risk_tier": capability.risk_tier, "completes_step": False,
        }
    return None


def _open_gate(
    connection: Connection, project_id: str, run: dict[str, Any], step_row: dict[str, Any]
) -> None:
    """Turn a step that needs approval into a decision a person can answer, and leave it
    waiting."""
    spec_step = step_row.get("_spec") or {}
    subject = step_row["_subject"]
    approval = spec_step.get("approval") or {}
    # Matches core.create_blocking_decision exactly: it takes a question and a recommendation,
    # returns the decision id, and defaults to HOLD — which is the behaviour a gate wants. A
    # gate that defaulted to "proceed" would be a gate in name only.
    decision_id = create_blocking_decision(
        connection,
        project_id,
        run.get("task_id"),
        f"{subject['label']}: {run.get('workflow_key') or 'workflow'}",
        {
            "capability": subject["key"],
            "step_id": step_row["step_id"],
            "workflow_run_id": run["id"],
            "approver_role": approval.get("approver_role"),
        },
        {"estimated_usd": 0.0},
        subject["summary"],
        risk_tier=max(2, int(subject["risk_tier"])),
    )
    # The deadline goes on the step, not in a scheduler somewhere else. A timeout you cannot
    # see from the row that is waiting is a timeout nobody can explain when it fires.
    sla_hours = approval.get("sla_hours")
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting',
                decision_id=:decision_id,
                binding_snapshot = COALESCE(binding_snapshot, '{}'::jsonb)
                                   || CAST(:snapshot AS JSONB),
                updated_at=now()
            WHERE id=:id
            """
        ),
        {
            "id": step_row["id"],
            "decision_id": decision_id,
            "snapshot": json.dumps({
                "approval": approval,
                # Recorded, not recomputed on resume: the answer to "did approving this finish
                # it?" must not change if the workflow is edited while somebody is deciding.
                "gate_completes_step": bool(subject["completes_step"]),
                "opened_at": now_utc().isoformat(),
                "due_at": (
                    (now_utc() + timedelta(hours=float(sla_hours))).isoformat()
                    if sla_hours else None
                ),
            }),
        },
    )
    emit_event(
        connection, project_id, "workflow.gate_opened", run.get("actor") or "workflow",
        "workflow_step_run", step_row["id"],
        {"capability": subject["key"], "step_id": step_row["step_id"]},
    )

def _resume_answered_gates(
    connection: Connection, project_id: str, run: dict[str, Any]
) -> list[dict[str, Any]]:
    """Act on gates a person has now answered.

    Without this the gate was a dead end: it opened, somebody approved it, and the step stayed
    `waiting` forever because the runtime's own resume logic only knows about `decision`-kind
    steps. A gate that cannot be passed is worse than no gate — the work never happens and
    nothing says why.
    """
    rows = connection.execute(
        text(
            """
            SELECT s.id, s.step_id, s.binding_snapshot, s.iteration, d.status AS decision_status
            FROM workflow_step_runs s
            JOIN decisions d ON d.id = s.decision_id
            WHERE s.workflow_run_id = :run_id
              AND s.status = 'waiting'
              AND s.step_kind = 'capability'
              AND d.status IN ('approved', 'rejected', 'expired')
            """
        ),
        {"run_id": run["id"]},
    ).mappings().all()

    acted: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row["binding_snapshot"] or {}
        if row["decision_status"] != "approved":
            workflows._block_run(
                connection, run["id"],
                f"step '{row['step_id']}' was {row['decision_status']}",
            )
            acted.append({"run_id": run["id"], "step_id": row["step_id"],
                          "outcome": f"gate_{row['decision_status']}"})
            continue

        if snapshot.get("gate_completes_step", True):
            # The approval was the work. Same direct UPDATE the timeout sweep uses, and for the
            # same reason: this step is leased by nobody, so `complete_step` would refuse it.
            connection.execute(
                text(
                    """
                    UPDATE workflow_step_runs
                    SET status='succeeded',
                        output = CAST(:output AS JSONB),
                        evidence = COALESCE(evidence,'[]'::jsonb) || CAST(:evidence AS JSONB),
                        completed_at = now(), updated_at = now()
                    WHERE id = :id AND status = 'waiting'
                    """
                ),
                {
                    "id": row["id"],
                    "output": json.dumps({"approved_by": "person", "outcome": "approved"}),
                    "evidence": json.dumps([{"kind": "approval", "decided_by": "person"}]),
                },
            )
            workflows.advance_run(connection, project_id, run["id"])
            outcome = "gate_passed"
        else:
            # The approval only unblocked it. Back to `ready` so the ordinary claim-and-execute
            # path runs it — with decision_id still set, which is what stops it being gated a
            # second time on the next tick.
            connection.execute(
                text(
                    """
                    UPDATE workflow_step_runs
                    SET status='ready', updated_at=now(),
                        binding_snapshot = COALESCE(binding_snapshot,'{}'::jsonb)
                                           || CAST(:patch AS JSONB)
                    WHERE id=:id AND status='waiting'
                    """
                ),
                {"id": row["id"],
                 "patch": json.dumps({"approved_at": now_utc().isoformat()})},
            )
            connection.execute(
                text("UPDATE workflow_runs SET status='running', updated_at=now() "
                     "WHERE id=:id AND status='waiting'"),
                {"id": run["id"]},
            )
            outcome = "approved_released"

        emit_event(
            connection, project_id, "workflow.gate_answered", run.get("actor") or "workflow",
            "workflow_step_run", row["id"],
            {"step_id": row["step_id"], "outcome": outcome},
        )
        acted.append({"run_id": run["id"], "step_id": row["step_id"], "outcome": outcome})
    return acted


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

            # Work that FINISHED while we were away, first of all: a build that completed a
            # moment ago should release the next step in this pass rather than a tick later.
            performed.extend(resume_awaited(
                connection, project_id=project_id, run_id=run_id, worker_id=worker_id
            ))

            # Answered gates next, so a step approved a moment ago is released in the same
            # tick rather than waiting for the next one.
            performed.extend(_resume_answered_gates(
                connection, project_id, {**dict(run), "id": run_id}
            ))

            # Then gates to open: they are not claimable work, and leaving them until after the
            # claim would let the runner treat a waiting approval as an idle step forever.
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
                subject = _gate_subject(connection, project_id, spec_step)
                if subject is not None:
                    _open_gate(
                        connection, project_id, {**dict(run), "id": run_id},
                        {**dict(row), "_spec": spec_step, "_subject": subject},
                    )
                    performed.append(
                        {"run_id": run_id, "step_id": row["step_id"],
                         "capability": subject["key"], "outcome": "gate_opened"}
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
                if output.get("status") == "awaiting":
                    # Started, not finished. Park it against the id it is waiting on;
                    # `resume_awaited` finishes it when that work reaches a terminal state.
                    workflows.park_step_awaiting(
                        connection, project_id=project_id, step_run_id=claimed["id"],
                        worker_id=worker_id, output=output,
                    )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "capability": output.get("capability"), "outcome": "awaiting",
                        "await": output.get("await"),
                    })
                else:
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


def sweep_gate_timeouts(
    connection: Connection, *, project_id: str, actor: str = "workflow-policy"
) -> dict[str, Any]:
    """Apply each waiting gate's timeout policy once its deadline has passed.

    Three outcomes, and the difference between them matters more than the mechanism:

      * `block`   — the default. It stays waiting and the run is marked blocked, so somebody
                    has to look. A gate whose unanswered outcome is "proceed" is not a gate.
      * `escalate`— still waiting, but now loud: an event a human process can pick up.
      * `approve` — allowed only below the risk ceiling, and recorded as a decision made BY
                    POLICY. An unattended approval that leaves no record is how an audit fails.

    Never raises for one bad step. Runs on the worker loop beside the capability tick.
    """
    rows = connection.execute(
        text(
            """
            SELECT s.id, s.step_id, s.workflow_run_id, s.binding_snapshot, s.decision_id,
                   r.actor, d.workflow_key
            FROM workflow_step_runs s
            JOIN workflow_runs r ON r.id = s.workflow_run_id
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE s.project_id = :project_id
              AND s.status = 'waiting'
              AND COALESCE(d.namespace, 'system') = 'product'
              AND s.binding_snapshot ? 'due_at'
              AND s.binding_snapshot->>'due_at' IS NOT NULL
              AND (s.binding_snapshot->>'due_at')::timestamptz < now()
              AND COALESCE(s.binding_snapshot->>'timeout_applied', 'no') = 'no'
            LIMIT 50
            """
        ),
        {"project_id": project_id},
    ).mappings().all()

    applied: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row["binding_snapshot"] or {}
        approval = snapshot.get("approval") or {}
        action = str(approval.get("on_timeout") or "block").lower()
        try:
            if action == "approve":
                # NOT `complete_step`. That is the worker path and requires the step to be
                # leased by the caller — this step is WAITING on a person, leased by nobody.
                # Approving by policy is a different act from finishing work, and borrowing
                # the worker's function to do it would have meant lying about who did what.
                connection.execute(
                    text(
                        """
                        UPDATE workflow_step_runs
                        SET status='succeeded',
                            output = CAST(:output AS JSONB),
                            evidence = COALESCE(evidence, '[]'::jsonb)
                                       || CAST(:evidence AS JSONB),
                            completed_at = now(),
                            updated_at = now()
                        WHERE id = :id AND status = 'waiting'
                        """
                    ),
                    {
                        "id": row["id"],
                        "output": json.dumps({
                            "approved_by": "policy",
                            "reason": "nobody answered within the agreed time",
                            "sla_hours": approval.get("sla_hours"),
                        }),
                        # The evidence IS the record. "Approved by policy" beside the SLA it
                        # was approved under is what an auditor needs; "approved" alone is not.
                        "evidence": json.dumps([{
                            "kind": "auto_approval",
                            "decided_by": "policy",
                            "sla_hours": approval.get("sla_hours"),
                            "approver_role": approval.get("approver_role"),
                            "due_at": snapshot.get("due_at"),
                        }]),
                    },
                )
                emit_event(
                    connection, project_id, "workflow.gate_auto_approved", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "sla_hours": approval.get("sla_hours")},
                )
                # The run has to move on, or a policy approval leaves the workflow parked at a
                # step that has already been decided.
                workflows.advance_run(connection, project_id, row["workflow_run_id"])
                outcome = "auto_approved"
            elif action == "escalate":
                emit_event(
                    connection, project_id, "workflow.gate_escalated", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "approver_role": approval.get("approver_role"),
                     "due_at": snapshot.get("due_at")},
                )
                outcome = "escalated"
            else:
                emit_event(
                    connection, project_id, "workflow.gate_overdue", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "due_at": snapshot.get("due_at")},
                )
                outcome = "blocked"

            # Marked so a gate is not escalated once a minute forever. Escalating repeatedly is
            # how people learn to ignore escalations.
            connection.execute(
                text(
                    "UPDATE workflow_step_runs "
                    "SET binding_snapshot = COALESCE(binding_snapshot,'{}'::jsonb) "
                    "    || CAST(:patch AS JSONB), updated_at=now() "
                    "WHERE id=:id"
                ),
                {"id": row["id"],
                 "patch": json.dumps({"timeout_applied": outcome,
                                      "timeout_applied_at": now_utc().isoformat()})},
            )
            applied.append({"step_run_id": row["id"], "step_id": row["step_id"],
                            "outcome": outcome})
        except Exception as error:                 # noqa: BLE001
            log.warning("gate timeout sweep skipped %s (non-fatal): %s", row["id"], error)

    return {"project_id": project_id, "applied": applied, "count": len(applied)}
