"""QL-4 (BOSSAPP-798): Caps, lossless hand-off to full workflow, PR evidence, and CI-after-open.

Caps (docs/product/QUICK_LANE.md §3.3):
- Wall-clock cap: 20 minutes (1200s)
- Cost cap: configurable (default $3.00 = 3_000_000 micros)
- Diff cap: <= 3 files, <= 150 lines (delegated to fix_lane_runner.check_escalation)
- Protected paths: never touched (delegated to fix_lane_runner.check_escalation)
- Blast radius cap: <= 15 KG callers (exceeding hands off to the full workflow)

Lossless hand-off carries study notes, reproduction, partial diff, carried test, context bundle,
and escalation reason so nothing done in the quick lane is thrown away.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import fix_lane_runner
from ..core import create_blocking_decision, emit_event
from ..receipts import ReceiptService
from .context import ContextBundle, ReproductionRecord
from .validator import ValidationVerdict

#: Wall-clock cap: 20 minutes (QUICK_LANE.md §3.3).
MAX_WALL_CLOCK_SECONDS = 20 * 60

#: Default quick-lane cost cap ($3.00 in USD micros).
DEFAULT_COST_CAP_USD_MICROS = 3_000_000

#: Maximum blast-radius callers before handing off to the full workflow.
MAX_BLAST_RADIUS_CALLERS = 15

#: Maximum CI-after-open reopen rounds before escalating.
MAX_CI_REOPENS = 2


@dataclass(frozen=True)
class QuickLimitCheck:
    """Result of checking wall-clock, cost, diff, protected-path, and blast-radius caps."""

    exceeded: bool
    reason: Optional[str] = None
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EscalationHandoff:
    """Lossless package passed from Quick Lane to the full workflow."""

    quick_run_id: str
    reason: str
    detail: str
    study_notes: list[dict[str, Any]]
    reproduction: Optional[dict[str, Any]]
    partial_diff: dict[str, str]
    carried_test: Optional[dict[str, Any]]
    context_bundle: dict[str, Any]
    escalated_run_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "quick_run_id": self.quick_run_id,
            "reason": self.reason,
            "detail": self.detail,
            "study_notes": self.study_notes,
            "reproduction": self.reproduction,
            "partial_diff": self.partial_diff,
            "carried_test": self.carried_test,
            "context_bundle": self.context_bundle,
            "escalated_run_id": self.escalated_run_id,
        }


def check_quick_limits(
    spec: dict[str, Any],
    *,
    files: int,
    lines: int,
    touched_paths: list[str],
    elapsed_seconds: float = 0.0,
    cost_usd_micros: int = 0,
    blast_radius_callers: int = 0,
    wall_clock_cap_seconds: float = MAX_WALL_CLOCK_SECONDS,
    cost_cap_usd_micros: int = DEFAULT_COST_CAP_USD_MICROS,
    blast_radius_cap: int = MAX_BLAST_RADIUS_CALLERS,
    carried_test: Optional[Any] = None,
) -> QuickLimitCheck:
    """Evaluate all Quick Lane caps; delegates diff + protected-path checks to ``fix_lane_runner``."""
    metrics = {
        "files": files,
        "lines": lines,
        "elapsed_seconds": elapsed_seconds,
        "cost_usd_micros": cost_usd_micros,
        "blast_radius_callers": blast_radius_callers,
    }
    if elapsed_seconds > wall_clock_cap_seconds:
        return QuickLimitCheck(
            exceeded=True,
            reason="wall_clock_exceeded",
            detail=f"Elapsed {elapsed_seconds:.1f}s exceeded {wall_clock_cap_seconds:.0f}s quick-lane cap.",
            metrics=metrics,
        )
    if cost_usd_micros > cost_cap_usd_micros:
        return QuickLimitCheck(
            exceeded=True,
            reason="cost_cap_exceeded",
            detail=f"Cost {cost_usd_micros} micros exceeded {cost_cap_usd_micros} micros quick-lane cap.",
            metrics=metrics,
        )
    if blast_radius_callers > blast_radius_cap:
        return QuickLimitCheck(
            exceeded=True,
            reason="blast_radius_too_wide",
            detail=f"Blast radius ({blast_radius_callers} callers) exceeded cap of {blast_radius_cap}.",
            metrics=metrics,
        )

    triage_spec = {
        "kind": spec.get("kind") or "fix",
        "repro": spec.get("repro") or "reproduced",
        **spec,
    }
    esc = fix_lane_runner.check_escalation(
        triage_spec,
        files=files,
        lines=lines,
        touched_paths=touched_paths,
        carried_test=carried_test,
    )
    if esc.escalated:
        return QuickLimitCheck(
            exceeded=True,
            reason=esc.reason or "diff_exceeded",
            detail=f"Escalated by fix_lane_runner.check_escalation: {esc.reason}.",
            metrics=metrics,
        )
    return QuickLimitCheck(exceeded=False, reason=None, detail="Within all quick-lane caps.", metrics=metrics)


def escalate_to_full_workflow(
    connection: Optional[Connection],
    *,
    project_id: str,
    quick_run_id: str,
    actor: str,
    reason: str,
    detail: str,
    spec: dict[str, Any],
    context_bundle: ContextBundle,
    reproduction: Optional[ReproductionRecord],
    partial_diff: dict[str, str],
    carried_test: Optional[dict[str, Any]] = None,
    full_launcher: Optional[Callable[[dict[str, Any]], str]] = None,
) -> EscalationHandoff:
    """Stop the quick-lane job and hand all accumulated work losslessly to the full workflow."""
    handoff_payload = {
        "from_quick_lane_run_id": quick_run_id,
        "escalation_reason": reason,
        "escalation_detail": detail,
        "title": spec.get("title") or "Escalated quick fix",
        "instructions": spec.get("instructions") or "",
        "study_notes": context_bundle.study_notes,
        "reproduction": reproduction.to_dict() if reproduction else None,
        "partial_diff": dict(partial_diff or {}),
        "carried_test": carried_test,
        "context_bundle": context_bundle.to_dict(),
        "forced_lane": "full",
    }

    escalated_run_id: Optional[str] = None
    if full_launcher is not None:
        escalated_run_id = str(full_launcher(handoff_payload) or "") or None

    handoff = EscalationHandoff(
        quick_run_id=quick_run_id,
        reason=reason,
        detail=detail,
        study_notes=context_bundle.study_notes,
        reproduction=reproduction.to_dict() if reproduction else None,
        partial_diff=dict(partial_diff or {}),
        carried_test=carried_test,
        context_bundle=context_bundle.to_dict(),
        escalated_run_id=escalated_run_id,
    )

    if connection is not None:
        connection.execute(
            text(
                "UPDATE quick_lane_runs "
                "SET status = 'escalated', phase = 'escalated', "
                "    escalation = CAST(:esc AS jsonb), "
                "    escalated_run_id = :escalated_run_id, "
                "    updated_at = now() "
                "WHERE id = :id AND project_id = :project_id"
            ),
            {
                "id": quick_run_id,
                "project_id": project_id,
                "esc": json.dumps(handoff.to_dict(), default=str),
                "escalated_run_id": escalated_run_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "quick_lane.escalated",
            actor,
            "quick_lane_run",
            quick_run_id,
            {
                "reason": reason,
                "detail": detail,
                "escalated_run_id": escalated_run_id,
                "carried_files": sorted(partial_diff.keys()),
                "has_study_notes": bool(context_bundle.study_notes),
                "has_reproduction": reproduction is not None,
            },
        )
    return handoff


def build_pr_evidence(
    *,
    reproduction: ReproductionRecord,
    targeted_test_output: str,
    validation: ValidationVerdict,
    left_out: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build structured PR evidence carrying all five required sections (§3.4)."""
    return {
        "reproduction": {
            "command": reproduction.command,
            "exit_code": reproduction.exit_code,
            "output": reproduction.output,
        },
        "causes": [c.to_dict() for c in reproduction.causes],
        "targeted_test_output": targeted_test_output,
        "validator_review": {
            "validator_role": validation.validator_role,
            "validator_model": validation.validator_model,
            "builder_model": validation.builder_model,
            "approved": validation.approved,
            "findings": [f.to_dict() for f in validation.findings],
            "independent_test": (
                validation.independent_test.to_dict() if validation.independent_test else None
            ),
        },
        "left_out": list(left_out or []),
    }


def render_pr_body(evidence: dict[str, Any]) -> str:
    """Render the Quick Lane PR markdown body with every required evidence section."""
    repro = evidence.get("reproduction") or {}
    causes = evidence.get("causes") or []
    val = evidence.get("validator_review") or {}
    ind = val.get("independent_test") or {}
    left_out = evidence.get("left_out") or []

    lines = [
        "## Reproduction",
        f"- Command: `{repro.get('command', '')}` (exit `{repro.get('exit_code', '')}`)",
        f"- Output: {repro.get('output', '')}",
        "",
        "## Causes",
    ]
    for c in causes:
        lines.append(f"- `{c.get('file')}:{c.get('line')}` — {c.get('explanation')}")

    lines.extend(
        [
            "",
            "## Targeted Test Output",
            str(evidence.get("targeted_test_output") or ""),
            "",
            "## Independent Validation",
            f"- Validator: `{val.get('validator_role')}` on `{val.get('validator_model')}` "
            f"(builder ran on `{val.get('builder_model')}`)",
            f"- Approved: `{val.get('approved')}`",
            f"- Independent test: `{ind.get('command', '')}` -> passed=`{ind.get('passed')}`",
            "",
            "## What Was Left Out and Why",
        ]
    )
    if left_out:
        for item in left_out:
            lines.append(f"- {item}")
    else:
        lines.append("- None (scoped entirely to the reproduced cause).")
    return "\n".join(lines)


def handle_ci_result(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    ci_status: str,
    ci_logs: str = "",
    actor: str = "ci_webhook",
    max_reopens: int = MAX_CI_REOPENS,
    requeue_fn: Optional[Callable[[Connection, str, str, int, str], Any]] = None,
) -> dict[str, Any]:
    """Process full-suite CI outcome after a quick-lane PR opens (BOSSAPP-798).

    - ``passed`` / ``success`` -> transitions run to ``completed``.
    - ``failed`` / ``failure`` -> reopens the quick job with a deduped receipt up to
      ``max_reopens`` times; beyond the cap, escalates with a blocking decision.
    """
    row = connection.execute(
        text(
            "SELECT id, project_id, status, ci_reopen_count, pull_request_url, spec "
            "FROM quick_lane_runs WHERE id = :id AND project_id = :project_id FOR UPDATE"
        ),
        {"id": run_id, "project_id": project_id},
    ).mappings().first()
    if row is None:
        raise LookupError(f"Quick-lane run {run_id} not found in project {project_id}.")

    norm = ci_status.strip().lower()
    if norm in ("passed", "success", "green"):
        connection.execute(
            text(
                "UPDATE quick_lane_runs "
                "SET status = 'completed', phase = 'completed', updated_at = now() "
                "WHERE id = :id AND project_id = :project_id"
            ),
            {"id": run_id, "project_id": project_id},
        )
        emit_event(
            connection,
            project_id,
            "quick_lane.ci_passed",
            actor,
            "quick_lane_run",
            run_id,
            {"pull_request_url": row["pull_request_url"]},
        )
        return {"run_id": run_id, "status": "completed", "reopened": False}

    reopen_count = int(row["ci_reopen_count"] or 0)
    if reopen_count >= max_reopens:
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            f"Quick-lane CI still failing after {reopen_count} reopen(s)",
            {
                "action": "escalate_full_workflow",
                "run_id": run_id,
                "ci_reopen_count": reopen_count,
            },
            {"ci_reopen_count": reopen_count},
            ci_logs[:800] or "Full-suite CI failed past the quick-lane reopen cap.",
        )
        connection.execute(
            text(
                "UPDATE quick_lane_runs "
                "SET status = 'escalated', phase = 'ci_exhausted', "
                "    decision_id = :decision_id, error = :error, updated_at = now() "
                "WHERE id = :id AND project_id = :project_id"
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "decision_id": decision_id,
                "error": f"CI failed after {reopen_count} reopen(s).",
            },
        )
        emit_event(
            connection,
            project_id,
            "quick_lane.ci_exhausted",
            actor,
            "quick_lane_run",
            run_id,
            {"ci_reopen_count": reopen_count, "decision_id": decision_id},
        )
        return {
            "run_id": run_id,
            "status": "escalated",
            "reopened": False,
            "ci_reopen_count": reopen_count,
            "decision_id": decision_id,
        }

    next_round = reopen_count + 1
    org_ws = connection.execute(
        text(
            "SELECT w.org_id, p.workspace_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    org_id = str(org_ws["org_id"]) if org_ws else "org_default"
    workspace_id = str(org_ws["workspace_id"]) if org_ws else "ws_default"
    receipts = ReceiptService()
    receipt, _ = receipts.record_completed(
        connection,
        org_id,
        workspace_id,
        project_id,
        f"quick_lane:{run_id}:ci_reopen:{next_round}",
        "quick_lane.ci_reopen",
        {"run_id": run_id, "target": row["pull_request_url"] or run_id},
        {"run_id": run_id, "round": next_round, "ci_logs": ci_logs[:1000], "actor": actor},
    )
    connection.execute(
        text(
            "UPDATE quick_lane_runs "
            "SET status = 'ci_reopened', phase = 'ci_repair', "
            "    ci_reopen_count = :n, waiting_reason = NULL, updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {"id": run_id, "project_id": project_id, "n": next_round},
    )
    emit_event(
        connection,
        project_id,
        "quick_lane.ci_reopened",
        actor,
        "quick_lane_run",
        run_id,
        {"round": next_round, "receipt_id": receipt["id"], "ci_logs": ci_logs[:500]},
    )
    if requeue_fn is not None:
        requeue_fn(connection, project_id, run_id, next_round, ci_logs)
    return {
        "run_id": run_id,
        "status": "ci_reopened",
        "reopened": True,
        "ci_reopen_count": next_round,
        "receipt_id": receipt["id"],
    }
