"""Deterministic Claude monthly spend-cap engine failover.

When the builder is ``claude_code`` and Anthropic returns the org monthly
spend limit, retry the SAME code.task once on a configured alternate engine
(openai/native). Never invent success. Never auto-apply decisions. One hop
only — a second spend-cap on the alternate escalates as today.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..ports import sql
from .core import emit_event
from .receipts import ReceiptService


OFFICIAL_ENGINES = frozenset({"openai", "native", "claude_code"})
FAILOVER_EVENT = "code_task.spend_cap_failover"
FAILOVER_EFFECT_KIND = "code_task.spend_cap_failover"

_MONTHLY_SPEND_MARKERS = (
    "you've hit your org's monthly spend limit",
    "you've hit your monthly spend limit",
)


def is_claude_monthly_spend_limit(error: Optional[str]) -> bool:
    """True only for Anthropic's monthly spend-cap copy — not generic 429s."""
    text = (error or "").lower()
    return any(marker in text for marker in _MONTHLY_SPEND_MARKERS)


def _engine_name(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def spend_cap_already_failed_over(
    spec: Optional[dict] = None,
    finalize_ctx: Optional[dict] = None,
    state: Optional[dict] = None,
) -> bool:
    for src in (spec, finalize_ctx, state):
        if isinstance(src, dict) and src.get("spend_cap_failover"):
            return True
    return False


def resolve_spend_cap_failover_engine(
    settings: Any,
    *,
    current_engine: str,
    already_failed_over: bool = False,
) -> Optional[str]:
    """Return the one-shot alternate engine, or None when failover is not honest."""
    if already_failed_over:
        return None
    current = _engine_name(current_engine) or _engine_name(
        getattr(settings, "code_agent_engine", None)
    ) or "claude_code"
    if current != "claude_code":
        return None
    explicit = _engine_name(getattr(settings, "code_agent_failover_engine", None))
    if explicit and explicit != "claude_code":
        return explicit
    low_cost = _engine_name(getattr(settings, "code_agent_low_cost_engine", None))
    if low_cost and low_cost != "claude_code":
        return low_cost
    if getattr(settings, "openai_api_key", None):
        return "openai"
    return None


def pin_engine_model_config(
    engine: str, existing: Optional[dict] = None
) -> dict:
    """Pin default + any claude_code step pins onto ``engine``."""
    target = _engine_name(engine)
    mc = dict(existing or {})
    default = mc.get("default")
    if isinstance(default, dict):
        mc["default"] = {**default, "engine": target}
    else:
        mc["default"] = {"engine": target}
    steps = mc.get("steps")
    if isinstance(steps, dict):
        rewritten = {}
        for step, value in steps.items():
            if isinstance(value, dict):
                if _engine_name(value.get("engine")) == "claude_code":
                    rewritten[step] = {**value, "engine": target}
                else:
                    rewritten[step] = value
            elif isinstance(value, str) and _engine_name(value).startswith("claude_code"):
                rewritten[step] = {"engine": target}
            else:
                rewritten[step] = value
        mc["steps"] = rewritten
    return mc


def failover_dedupe_key(run_id: str) -> str:
    return f"code_task:spend_cap_failover:{run_id}"


def failover_job_dedupe_key(run_id: str) -> str:
    return f"code.task:spend_cap_failover:{run_id}"


def plan_spend_cap_failover(
    *,
    error: Optional[str],
    current_engine: str,
    settings: Any,
    already_failed_over: bool = False,
    spec: Optional[dict] = None,
    run_id: str = "",
) -> Optional[dict]:
    if already_failed_over or spend_cap_already_failed_over(spec):
        return None
    if not is_claude_monthly_spend_limit(error):
        return None
    to_engine = resolve_spend_cap_failover_engine(
        settings, current_engine=current_engine, already_failed_over=False
    )
    if not to_engine:
        return None
    existing = (spec or {}).get("model_config") if isinstance(spec, dict) else None
    existing = existing if isinstance(existing, dict) else None
    return {
        "from_engine": _engine_name(current_engine) or "claude_code",
        "to_engine": to_engine,
        "reason": "claude_monthly_spend_limit",
        "dedupe_key": failover_dedupe_key(run_id or "unknown"),
        "model_config": pin_engine_model_config(to_engine, existing),
    }


def _project_scope(connection: Any, project_id: str) -> Optional[dict[str, str]]:
    try:
        row = connection.execute(
            sql(
                """
                SELECT w.org_id, p.workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — receipt is best-effort; event still fires
        return None
    if not row:
        return None
    return {"org_id": str(row["org_id"]), "workspace_id": str(row["workspace_id"])}


def _persist_run_failover(
    connection: Any, *, run_id: str, plan: dict, record: dict
) -> None:
    if hasattr(connection, "save_run"):
        run = connection.get_run(run_id) or {}
        spec = dict(run.get("spec") or {})
        spec["model_config"] = plan["model_config"]
        spec["spend_cap_failover"] = record
        fctx = dict(run.get("finalize_ctx") or {})
        fctx["spend_cap_failover"] = record
        state = dict(run.get("state") or {})
        state["spend_cap_failover"] = record
        connection.save_run(
            run_id,
            {
                "status": "running",
                "phase": "queued",
                "error": None,
                "spec": spec,
                "finalize_ctx": fctx,
                "state": state,
            },
        )
        return
    connection.execute(
        sql(
            """
            UPDATE code_task_runs
            SET spec = COALESCE(spec, '{}'::jsonb)
                    || jsonb_build_object(
                        'model_config', CAST(:mc AS jsonb),
                        'spend_cap_failover', CAST(:fo AS jsonb)
                    ),
                finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb)
                    || jsonb_build_object('spend_cap_failover', CAST(:fo AS jsonb)),
                state = COALESCE(state, '{}'::jsonb)
                    || jsonb_build_object('spend_cap_failover', CAST(:fo AS jsonb)),
                status = 'running',
                phase = 'queued',
                error = NULL,
                updated_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": run_id,
            "mc": json.dumps(plan["model_config"]),
            "fo": json.dumps(record),
        },
    )


def apply_spend_cap_failover(
    connection: Any,
    *,
    jobs: Any,
    project_id: str,
    run_id: str,
    task_id: Optional[str],
    settings: Any,
    current_engine: str,
    error: Optional[str],
    spec: Optional[dict] = None,
    finalize_ctx: Optional[dict] = None,
    state: Optional[dict] = None,
    receipts: Optional[ReceiptService] = None,
) -> Optional[dict]:
    """Pin the alternate engine on this run, receipt the hop, enqueue one job.

    Returns a worker-safe ``requeued`` result, or None when the existing
    needs_human / escalate path must stay in charge.
    """
    already = spend_cap_already_failed_over(spec, finalize_ctx, state)
    plan = plan_spend_cap_failover(
        error=error,
        current_engine=current_engine,
        settings=settings,
        already_failed_over=already,
        spec=spec,
        run_id=run_id,
    )
    if plan is None:
        return None

    record = {
        "from_engine": plan["from_engine"],
        "to_engine": plan["to_engine"],
        "reason": plan["reason"],
        "attempted": True,
        "invented_success": False,
    }
    _persist_run_failover(connection, run_id=run_id, plan=plan, record=record)

    receipt_id = None
    receipts = receipts or ReceiptService()
    scope = _project_scope(connection, project_id)
    if scope:
        try:
            receipt, _created = receipts.create_or_reuse(
                connection,
                scope["org_id"],
                scope["workspace_id"],
                project_id,
                plan["dedupe_key"],
                FAILOVER_EFFECT_KIND,
                {"run_id": run_id, "to_engine": plan["to_engine"]},
                {
                    "from_engine": plan["from_engine"],
                    "to_engine": plan["to_engine"],
                    "reason": plan["reason"],
                    "invented_success": False,
                },
            )
            receipt_id = receipt.get("id")
            receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref=plan["dedupe_key"],
                result={
                    "from_engine": plan["from_engine"],
                    "to_engine": plan["to_engine"],
                    "reason": plan["reason"],
                    "run_id": run_id,
                    "invented_success": False,
                },
            )
        except Exception as exc:  # noqa: BLE001 — never mask failover on receipt I/O
            emit_event(
                connection,
                project_id,
                "code_task.spend_cap_failover_receipt_failed",
                "boss",
                "code_task_run",
                run_id,
                {"error": f"{type(exc).__name__}: {exc}"[:200]},
            )
    else:
        emit_event(
            connection,
            project_id,
            "code_task.spend_cap_failover_receipt_failed",
            "boss",
            "code_task_run",
            run_id,
            {"error": "project_scope_missing"},
        )

    emit_event(
        connection,
        project_id,
        FAILOVER_EVENT,
        "boss",
        "code_task_run",
        run_id,
        {
            "from_engine": plan["from_engine"],
            "to_engine": plan["to_engine"],
            "reason": plan["reason"],
            "dedupe_key": plan["dedupe_key"],
            "receipt_id": receipt_id,
            "invented_success": False,
        },
    )

    jobs.create_job(
        connection,
        project_id=project_id,
        task_id=task_id,
        kind="code.task",
        priority=46,
        payload={"run_id": run_id, "spend_cap_failover": True},
        dedupe_key=failover_job_dedupe_key(run_id),
    )
    return {
        "status": "requeued",
        "done": True,
        "run_id": run_id,
        "from_engine": plan["from_engine"],
        "to_engine": plan["to_engine"],
        "reason": plan["reason"],
        "receipt_id": receipt_id,
        "dedupe_key": plan["dedupe_key"],
        "invented_success": False,
        "auto_applied_decision": False,
    }
