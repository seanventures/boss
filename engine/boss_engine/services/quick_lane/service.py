"""QL-5 (BOSSAPP-799) + end-to-end Quick Lane orchestrator (QL-1..QL-5).

Single governed entry point shared by all four surfaces:
- Ask BOSS chat (`source="ask_boss"`)
- Console New build / Quick fix (`source="console"`)
- MCP `create_code_task(..., lane="quick")` (`source="mcp"`)
- Plane / GitHub label `boss:quick` (`source="ticket_label"`)

Preserves identity lineage, BudgetGovernor reservations, Receipt dedupe keys,
durable run record (`quick_lane_runs`), and human merge approval (`review_pull_request`).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...config import Settings
from .. import fix_lane_triage
from ..budget import BudgetGovernor
from ..core import create_blocking_decision, emit_event, make_id
from ..jobs import enqueue_job
from ..receipts import ReceiptService
from .context import (
    ReproductionRecord,
    ReproductionRequiredError,
    load_full_context,
    render_context_prompt,
    validate_reproduction,
)
from .limits import (
    DEFAULT_COST_CAP_USD_MICROS,
    MAX_BLAST_RADIUS_CALLERS,
    MAX_WALL_CLOCK_SECONDS,
    build_pr_evidence,
    check_quick_limits,
    escalate_to_full_workflow,
    render_pr_body,
)
from .queue import (
    DEFAULT_REQUIRED_MEMORY_MB,
    JOB_KIND,
    WORKLOAD_CLASS,
    admit_quick_job,
    priority_for_requester,
)
from .validator import (
    MAX_VALIDATION_ROUNDS,
    IndependentTestResult,
    ValidationFinding,
    run_validation_loop,
)

#: Ticket/GitHub label that routes a ticket into the Quick Lane (QUICK_LANE.md §3.5).
QUICK_LANE_LABEL = "boss:quick"

#: Default budget reservation for a quick-lane fix ($1.50 in USD micros).
DEFAULT_RESERVE_USD_MICROS = 1_500_000

VALID_SOURCES = frozenset({"ask_boss", "console", "mcp", "ticket_label", "api"})


def lane_from_labels(labels: Optional[Iterable[str]]) -> Optional[str]:
    """Return ``"quick"`` when ``boss:quick`` is present in ``labels`` (case-insensitive)."""
    for label in labels or ():
        if str(label).strip().lower() == QUICK_LANE_LABEL:
            return "quick"
    return None


def triage_quick_lane(
    spec: dict[str, Any],
    *,
    diff_estimate: Optional[Any] = None,
    force_lane: Optional[str] = None,
    labels: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Suggest ``quick`` or ``full`` lane; caller override or ``boss:quick`` label wins."""
    normalized_force: Optional[str] = None
    if force_lane is not None:
        raw = str(force_lane).strip().lower()
        if raw in ("quick", "fix"):
            normalized_force = fix_lane_triage.LANE_FIX
        elif raw == "full":
            normalized_force = fix_lane_triage.LANE_FULL
    elif lane_from_labels(labels or spec.get("labels")) == "quick":
        normalized_force = fix_lane_triage.LANE_FIX

    verdict = fix_lane_triage.classify(
        spec,
        diff_estimate=diff_estimate,
        forced_lane=normalized_force,
    )
    suggested = "quick" if verdict.lane == fix_lane_triage.LANE_FIX else "full"
    return {
        "lane": suggested,
        "triage_lane": verdict.lane,
        "reason": verdict.reason,
        "signals": verdict.signals,
        "forced": normalized_force is not None,
    }


def suggest_quick_fix_offer(
    *,
    title: str,
    instructions: str,
    project_id: str,
    repro: Optional[str] = None,
    touched_paths: Optional[list[str]] = None,
    diff_estimate: Optional[dict[str, Any]] = None,
    extends_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a one-button Ask BOSS offer for a small specified fix (BOSSAPP-799 / 1373).

    Returns ``tool="create_code_task"`` with ``lane="quick"`` when triage recommends quick,
    otherwise ``tool="start_workflow"`` so bigger asks stay on the full workflow path.
    """
    spec: dict[str, Any] = {
        "kind": "bug" if repro else "",
        "title": title,
        "instructions": instructions,
        "description": instructions,
        "repro": repro,
        "touched_paths": touched_paths or [],
    }
    estimate = diff_estimate or (
        {"files": len(touched_paths), "lines": 60, "touched_paths": touched_paths}
        if touched_paths
        else None
    )
    triaged = triage_quick_lane(spec, diff_estimate=estimate)
    if triaged["lane"] == "quick":
        args: dict[str, Any] = {
            "title": title,
            "instructions": instructions,
            "lane": "quick",
            "project_id": project_id,
        }
        if repro:
            args["repro"] = repro
        if extends_run_id:
            args["extends_run_id"] = extends_run_id
        return {
            "lane": "quick",
            "tool": "create_code_task",
            "label": f"Quick fix: {title}",
            "args": args,
            "reason": triaged["reason"],
        }
    return {
        "lane": "full",
        "tool": "start_workflow",
        "label": f"Start workflow: {title}",
        "args": {
            "project_id": project_id,
            "title": title,
            "directive": instructions,
        },
        "reason": triaged["reason"],
    }


def _row_to_quick_run(row: Any) -> dict[str, Any]:
    def _j(v: Any) -> dict[str, Any]:
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}
        return {}

    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "code_task_run_id": row["code_task_run_id"],
        "task_id": row["task_id"],
        "title": str(row["title"]),
        "status": str(row["status"]),
        "phase": str(row["phase"]),
        "source": str(row["source"]),
        "requested_by_person": bool(row["requested_by_person"]),
        "actor": str(row["actor"]),
        "host_id": row["host_id"],
        "waiting_reason": row["waiting_reason"],
        "spec": _j(row["spec"]),
        "context_bundle": _j(row["context_bundle"]),
        "reproduction": _j(row["reproduction"]),
        "validation": _j(row["validation"]),
        "escalation": _j(row["escalation"]),
        "pr_evidence": _j(row["pr_evidence"]),
        "reservation_id": row["reservation_id"],
        "receipt_id": row["receipt_id"],
        "pull_request_url": row["pull_request_url"],
        "pull_request_number": row["pull_request_number"],
        "decision_id": row["decision_id"],
        "escalated_run_id": row["escalated_run_id"],
        "ci_reopen_count": int(row["ci_reopen_count"] or 0),
        "validation_rounds": int(row["validation_rounds"] or 0),
        "cost_usd_micros": int(row["cost_usd_micros"] or 0),
        "elapsed_seconds": float(row["elapsed_seconds"] or 0.0),
        "error": row["error"],
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
        "updated_at": row["updated_at"].isoformat() if hasattr(row["updated_at"], "isoformat") else str(row["updated_at"]),
    }


def get_quick_run(connection: Connection, *, project_id: str, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            "SELECT * FROM quick_lane_runs WHERE id = :id AND project_id = :project_id"
        ),
        {"id": run_id, "project_id": project_id},
    ).mappings().first()
    return _row_to_quick_run(row) if row else None


def list_quick_runs(
    connection: Connection,
    *,
    project_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where = "project_id = :project_id"
    params: dict[str, Any] = {"project_id": project_id, "lim": max(1, min(200, int(limit)))}
    if status:
        where += " AND status = :status"
        params["status"] = status
    rows = connection.execute(
        text(
            f"SELECT * FROM quick_lane_runs WHERE {where} "
            "ORDER BY requested_by_person DESC, created_at DESC LIMIT :lim"
        ),
        params,
    ).mappings().all()
    return [_row_to_quick_run(r) for r in rows]


def _project_scope(connection: Connection, project_id: str) -> tuple[str, str]:
    row = connection.execute(
        text(
            "SELECT w.org_id, p.workspace_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if row:
        return str(row["org_id"]), str(row["workspace_id"])
    return "org_default", "ws_default"


def _governor(budget_governor: Optional[BudgetGovernor]) -> BudgetGovernor:
    return budget_governor or BudgetGovernor(Settings())


def start_quick_fix(
    connection: Connection,
    *,
    project_id: str,
    title: str,
    instructions: str,
    actor: str = "operator",
    source: str = "console",
    requested_by_person: Optional[bool] = None,
    task_id: Optional[str] = None,
    spec_extra: Optional[dict[str, Any]] = None,
    reserve_usd_micros: int = DEFAULT_RESERVE_USD_MICROS,
    cost_cap_usd_micros: int = DEFAULT_COST_CAP_USD_MICROS,
    wall_clock_cap_seconds: float = MAX_WALL_CLOCK_SECONDS,
    dedupe_key: Optional[str] = None,
    on_behalf_of: Optional[Any] = None,
    role_key: Optional[str] = None,
    budget_governor: Optional[BudgetGovernor] = None,
) -> dict[str, Any]:
    """Create a governed quick-lane run, reserve budget, and enqueue a ``quick.fix`` job."""
    from .. import projects_archive

    projects_archive.refuse_new_work(connection, project_id, "new quick fixes")
    src = source if source in VALID_SOURCES else "api"
    by_person = (
        bool(requested_by_person)
        if requested_by_person is not None
        else src in ("ask_boss", "console", "mcp")
    )

    run_id = make_id("qlr")
    spec = {
        "kind": "fix",
        "title": title,
        "instructions": instructions,
        "description": instructions,
        "source": src,
        "cost_cap_usd_micros": int(cost_cap_usd_micros),
        "wall_clock_cap_seconds": float(wall_clock_cap_seconds),
        **(spec_extra or {}),
    }

    reservation_id: Optional[str] = None
    if reserve_usd_micros > 0:
        gov = _governor(budget_governor)
        reservation = gov.reserve(
            connection,
            project_id,
            "quick_lane",
            int(reserve_usd_micros),
            f"quick_lane:{run_id}",
            30,
        )
        reservation_id = str(reservation.get("reservation_id") or reservation.get("id") or "") or None

    connection.execute(
        text(
            "INSERT INTO quick_lane_runs ("
            "  id, project_id, task_id, title, status, phase, source, requested_by_person, "
            "  actor, spec, reservation_id"
            ") VALUES ("
            "  :id, :project_id, :task_id, :title, 'queued', 'queued', :source, :by_person, "
            "  :actor, CAST(:spec AS jsonb), :reservation_id"
            ")"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "task_id": task_id,
            "title": title,
            "source": src,
            "by_person": by_person,
            "actor": actor,
            "spec": json.dumps(spec, default=str),
            "reservation_id": reservation_id,
        },
    )

    job = enqueue_job(
        connection,
        project_id,
        JOB_KIND,
        {
            "run_id": run_id,
            "title": title,
            "source": src,
            "requested_by_person": by_person,
        },
        task_id=task_id,
        priority=priority_for_requester(by_person),
        workload_class=WORKLOAD_CLASS,
        dedupe_key=dedupe_key,
        on_behalf_of=on_behalf_of,
        role_key=role_key or "quick_builder",
    )

    emit_event(
        connection,
        project_id,
        "quick_lane.queued",
        actor,
        "quick_lane_run",
        run_id,
        {
            "job_id": job["id"],
            "source": src,
            "requested_by_person": by_person,
            "reservation_id": reservation_id,
        },
    )
    return {
        "run_id": run_id,
        "quick_lane_run_id": run_id,
        "job_id": job["id"],
        "project_id": project_id,
        "lane": "quick",
        "status": "queued",
        "source": src,
        "requested_by_person": by_person,
        "reservation_id": reservation_id,
    }


def execute_quick_job(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    host_id: str,
    builder_session: Callable[[str, dict[str, Any]], dict[str, Any]],
    builder_revise: Optional[Callable[[list[ValidationFinding], int, str], dict[str, Any]]] = None,
    independent_tester: Optional[Callable[[dict[str, Any], int], IndependentTestResult]] = None,
    extra_reviewer: Optional[Callable[[dict[str, Any]], list[ValidationFinding]]] = None,
    pr_opener: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    full_launcher: Optional[Callable[[dict[str, Any]], str]] = None,
    decider: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    available_memory_mb: Optional[int] = None,
    required_memory_mb: int = DEFAULT_REQUIRED_MEMORY_MB,
    memory_probe: Optional[Callable[[], Optional[int]]] = None,
    builder_model: str = "claude-sonnet",
    available_models: Optional[Iterable[str]] = None,
    actor: str = "worker",
    budget_governor: Optional[BudgetGovernor] = None,
) -> dict[str, Any]:
    """Execute one quick-lane job end to end inside a single warm session.

    Flow:
      1. Host slot + memory-aware admission (QL-1).
      2. Load full context (Study notes, KG, learnings, standards, evidence) before first edit (QL-2).
      3. Run warm builder session; enforce reproduce-first gate (QL-2).
      4. Check wall-clock, cost, diff, protected-path, and blast-radius caps; escalate losslessly
         if exceeded (QL-4).
      5. Run independent validator (fresh context, different model, review + independent test) with
         bounded warm-session revisions and decision-ladder fallback (QL-3).
      6. Open receipt-deduped PR carrying all 5 evidence sections + create ``review_pull_request``
         human merge gate + settle budget reservation (QL-1 / QL-4).
    """
    run = get_quick_run(connection, project_id=project_id, run_id=run_id)
    if run is None:
        raise LookupError(f"Quick-lane run {run_id} not found in project {project_id}.")
    if run["pull_request_url"] and run["status"] in ("pr_opened", "completed"):
        return {"run_id": run_id, "status": run["status"], "idempotent": True, "pull_request_url": run["pull_request_url"]}

    # 1. Admission
    admit = admit_quick_job(
        connection,
        run_id=run_id,
        project_id=project_id,
        host_id=host_id,
        available_memory_mb=available_memory_mb,
        required_memory_mb=required_memory_mb,
        memory_probe=memory_probe,
        actor=actor,
    )
    if not admit.admitted:
        return {
            "run_id": run_id,
            "status": "waiting_memory" if admit.reason == "insufficient_host_memory" else run["status"],
            "deferred": True,
            "reason": admit.reason,
            "detail": admit.detail,
        }

    spec = run["spec"]
    gov = _governor(budget_governor)

    # 2. Full context in BEFORE first edit
    context_bundle = load_full_context(
        connection,
        project_id=project_id,
        spec=spec,
        touched_paths=spec.get("touched_paths"),
        symbols=spec.get("symbols"),
    )
    prompt = render_context_prompt(context_bundle, spec)
    connection.execute(
        text(
            "UPDATE quick_lane_runs "
            "SET phase = 'implement', context_bundle = CAST(:ctx AS jsonb), updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "ctx": json.dumps({**context_bundle.to_dict(), "prompt": prompt}, default=str),
        },
    )
    emit_event(
        connection,
        project_id,
        "quick_lane.context_loaded",
        actor,
        "quick_lane_run",
        run_id,
        {"sources_used": context_bundle.sources_used()},
    )

    # 3. Single warm builder session + reproduce-first gate
    session_id = f"ql_session_{run_id}"
    builder_out = builder_session(prompt, {"run_id": run_id, "session_id": session_id, "spec": spec, "context": context_bundle.to_dict()})
    elapsed_seconds = float(builder_out.get("elapsed_seconds") or 0.0)
    cost_usd_micros = int(builder_out.get("cost_usd_micros") or 0)
    diff_files: dict[str, str] = dict(builder_out.get("diff_files") or {})

    try:
        repro: ReproductionRecord = validate_reproduction(builder_out.get("reproduction"))
    except ReproductionRequiredError as exc:
        connection.execute(
            text(
                "UPDATE quick_lane_runs "
                "SET status = 'failed', phase = 'repro_missing', error = :err, "
                "    elapsed_seconds = :elapsed, cost_usd_micros = :cost, updated_at = now() "
                "WHERE id = :id AND project_id = :project_id"
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "err": str(exc),
                "elapsed": elapsed_seconds,
                "cost": cost_usd_micros,
            },
        )
        if run["reservation_id"]:
            gov.release(connection, project_id, run["reservation_id"], "repro_missing")
        emit_event(
            connection,
            project_id,
            "quick_lane.repro_missing",
            actor,
            "quick_lane_run",
            run_id,
            {"error": str(exc)},
        )
        return {"run_id": run_id, "status": "failed", "reason": "repro_missing", "error": str(exc)}

    connection.execute(
        text(
            "UPDATE quick_lane_runs "
            "SET reproduction = CAST(:repro AS jsonb), elapsed_seconds = :elapsed, "
            "    cost_usd_micros = :cost, updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "repro": json.dumps(repro.to_dict(), default=str),
            "elapsed": elapsed_seconds,
            "cost": cost_usd_micros,
        },
    )

    # 4. Caps + lossless escalation check
    diff_lines = sum(
        len([ln for ln in content.splitlines() if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))])
        or len(content.splitlines())
        for content in diff_files.values()
    )
    limit_check = check_quick_limits(
        spec,
        files=len(diff_files),
        lines=diff_lines,
        touched_paths=list(diff_files.keys()),
        elapsed_seconds=elapsed_seconds,
        cost_usd_micros=cost_usd_micros,
        blast_radius_callers=len(context_bundle.kg.get("callers") or []),
        wall_clock_cap_seconds=float(spec.get("wall_clock_cap_seconds") or MAX_WALL_CLOCK_SECONDS),
        cost_cap_usd_micros=int(spec.get("cost_cap_usd_micros") or DEFAULT_COST_CAP_USD_MICROS),
        blast_radius_cap=int(spec.get("blast_radius_cap") or MAX_BLAST_RADIUS_CALLERS),
        carried_test=builder_out.get("carried_test"),
    )
    if limit_check.exceeded:
        handoff = escalate_to_full_workflow(
            connection,
            project_id=project_id,
            quick_run_id=run_id,
            actor=actor,
            reason=limit_check.reason or "cap_exceeded",
            detail=limit_check.detail,
            spec=spec,
            context_bundle=context_bundle,
            reproduction=repro,
            partial_diff=diff_files,
            carried_test=builder_out.get("carried_test"),
            full_launcher=full_launcher,
        )
        if run["reservation_id"]:
            if cost_usd_micros > 0:
                org_id, ws_id = _project_scope(connection, project_id)
                esc_receipt, _ = ReceiptService().record_completed(
                    connection,
                    org_id,
                    ws_id,
                    project_id,
                    f"quick_lane:{run_id}:escalated",
                    "quick_lane.escalated",
                    {"run_id": run_id, "reason": handoff.reason},
                    {"run_id": run_id, "reason": handoff.reason, "cost_usd_micros": cost_usd_micros},
                )
                gov.consume(
                    connection,
                    project_id,
                    run["reservation_id"],
                    str(esc_receipt["id"]),
                    "quick_lane",
                    builder_model,
                    DEFAULT_RESERVE_USD_MICROS,
                    cost_usd_micros,
                    actor,
                )
            else:
                gov.release(connection, project_id, run["reservation_id"], handoff.reason)
        return {
            "run_id": run_id,
            "status": "escalated",
            "reason": handoff.reason,
            "handoff": handoff.to_dict(),
        }

    # 5. Independent validation by a different agent/model before PR opens
    connection.execute(
        text(
            "UPDATE quick_lane_runs SET status = 'validating', phase = 'validating', updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {"id": run_id, "project_id": project_id},
    )
    verdict, final_state, val_history = run_validation_loop(
        connection=connection,
        project_id=project_id,
        run_id=run_id,
        actor=actor,
        spec=spec,
        context_bundle=context_bundle,
        initial_state={
            "reproduction": repro,
            "diff_files": diff_files,
            "pr_claims": builder_out.get("pr_claims") or [],
            "builder_test_paths": builder_out.get("builder_test_paths") or [],
            "builder_tests_used_synthetic_fixture": bool(
                builder_out.get("builder_tests_used_synthetic_fixture", False)
            ),
            "independent_test": builder_out.get("independent_test"),
        },
        builder_revise=builder_revise,
        independent_tester=independent_tester,
        extra_reviewer=extra_reviewer,
        decider=decider,
        builder_model=builder_model,
        available_models=available_models,
        max_rounds=int(spec.get("max_validation_rounds") or MAX_VALIDATION_ROUNDS),
        session_id=session_id,
    )

    connection.execute(
        text(
            "UPDATE quick_lane_runs "
            "SET validation = CAST(:val AS jsonb), validation_rounds = :rounds, updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "val": json.dumps(
                {"final": verdict.to_dict(), "rounds": [v.to_dict() for v in val_history]},
                default=str,
            ),
            "rounds": len(val_history),
        },
    )

    if not verdict.approved:
        handoff = escalate_to_full_workflow(
            connection,
            project_id=project_id,
            quick_run_id=run_id,
            actor=actor,
            reason="validation_unresolved",
            detail=f"Validator blocked PR after {len(val_history)} round(s).",
            spec=spec,
            context_bundle=context_bundle,
            reproduction=repro,
            partial_diff=final_state.get("diff_files") or diff_files,
            carried_test=builder_out.get("carried_test"),
            full_launcher=full_launcher,
        )
        if run["reservation_id"]:
            if cost_usd_micros > 0:
                org_id, ws_id = _project_scope(connection, project_id)
                val_receipt, _ = ReceiptService().record_completed(
                    connection,
                    org_id,
                    ws_id,
                    project_id,
                    f"quick_lane:{run_id}:validation_escalated",
                    "quick_lane.validation_escalated",
                    {"run_id": run_id},
                    {"run_id": run_id, "cost_usd_micros": cost_usd_micros},
                )
                gov.consume(
                    connection,
                    project_id,
                    run["reservation_id"],
                    str(val_receipt["id"]),
                    "quick_lane",
                    builder_model,
                    DEFAULT_RESERVE_USD_MICROS,
                    cost_usd_micros,
                    actor,
                )
            else:
                gov.release(connection, project_id, run["reservation_id"], "validation_unresolved")
        return {
            "run_id": run_id,
            "status": "escalated",
            "reason": "validation_unresolved",
            "validation": verdict.to_dict(),
            "handoff": handoff.to_dict(),
        }

    # 6. Open PR carrying all 5 evidence sections + deduped Receipt + human merge decision
    pr_evidence = build_pr_evidence(
        reproduction=repro,
        targeted_test_output=str(final_state.get("targeted_test_output") or builder_out.get("targeted_test_output") or ""),
        validation=verdict,
        left_out=builder_out.get("left_out"),
    )
    pr_body = render_pr_body(pr_evidence)

    pr_info = (
        pr_opener({"run_id": run_id, "title": run["title"], "body": pr_body, "diff_files": final_state.get("diff_files") or diff_files})
        if pr_opener is not None
        else {"url": f"https://github.com/local/repo/pull/{run_id}", "number": 1}
    )
    pr_url = str(pr_info.get("url") or "")
    pr_num = int(pr_info["number"]) if pr_info.get("number") is not None else None

    org_id, ws_id = _project_scope(connection, project_id)
    receipt, _ = ReceiptService().record_completed(
        connection,
        org_id,
        ws_id,
        project_id,
        f"quick_lane:{run_id}:pr",
        "quick_lane.open_pr",
        {"run_id": run_id, "target": pr_url or run_id},
        {"run_id": run_id, "pull_request_url": pr_url, "pull_request_number": pr_num, "actor": actor},
        provider_ref=pr_url or None,
    )

    merge_decision_id = create_blocking_decision(
        connection,
        project_id,
        run["task_id"],
        f"Review Quick Fix PR: {run['title']}",
        {
            "action": "merge_pull_request",
            "quick_lane_run_id": run_id,
            "pull_request_url": pr_url,
            "pull_request_number": pr_num,
        },
        {"cost_usd_micros": cost_usd_micros, "elapsed_seconds": elapsed_seconds},
        f"Quick-lane PR {pr_url} passed independent validation and awaits human merge decision.",
    )

    if run["reservation_id"]:
        gov.consume(
            connection,
            project_id,
            run["reservation_id"],
            str(receipt["id"]),
            "quick_lane",
            builder_model,
            DEFAULT_RESERVE_USD_MICROS,
            cost_usd_micros,
            actor,
        )

    connection.execute(
        text(
            "UPDATE quick_lane_runs "
            "SET status = 'pr_opened', phase = 'ci_watch', "
            "    pr_evidence = CAST(:ev AS jsonb), receipt_id = :receipt_id, "
            "    pull_request_url = :pr_url, pull_request_number = :pr_num, "
            "    decision_id = :decision_id, updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "ev": json.dumps({**pr_evidence, "body_markdown": pr_body}, default=str),
            "receipt_id": receipt["id"],
            "pr_url": pr_url,
            "pr_num": pr_num,
            "decision_id": merge_decision_id,
        },
    )
    emit_event(
        connection,
        project_id,
        "quick_lane.pr_opened",
        actor,
        "quick_lane_run",
        run_id,
        {
            "pull_request_url": pr_url,
            "pull_request_number": pr_num,
            "receipt_id": receipt["id"],
            "decision_id": merge_decision_id,
        },
    )
    return {
        "run_id": run_id,
        "status": "pr_opened",
        "pull_request_url": pr_url,
        "pull_request_number": pr_num,
        "receipt_id": receipt["id"],
        "decision_id": merge_decision_id,
        "validation": verdict.to_dict(),
        "pr_evidence": pr_evidence,
    }
