"""Per-LLM-call telemetry — VAG Phase 0 (docs/product/BOSS_VERIFICATION_AMPLIFIED_GENERATION.md).

Every role-scoped model invocation is recorded to the ``llm_calls`` table so cost and
quality can be measured per (model, stage, task type) — the inputs the economic
controller and the model bake-off need. Two design constraints:

  - Best-effort: a telemetry failure must NEVER fail the pipeline. record_llm_call
    swallows every exception (logging at warning) — the LLM result always wins.
  - Context travels out-of-band: the chokepoint (CodeAgent.run_role) has no run_id or
    stage, so callers publish them via set_llm_context(). The context is a ContextVar —
    per-thread by construction, so the governed review panel's ThreadPoolExecutor
    closures must each call set_llm_context() INSIDE the worker thread (a new thread
    starts with an empty context; the main thread's values do not propagate).
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Optional
from uuid import uuid4

from sqlalchemy import text

log = logging.getLogger("boss.llm_telemetry")

# {db_engine (sqlalchemy), project_id, run_id, stage}
_llm_ctx: ContextVar[dict] = ContextVar("llm_call_context", default={})


def set_llm_context(**kwargs) -> None:
    """Merge fields into the current thread's LLM-call context. Recognised keys:
    ``db_engine`` (SQLAlchemy engine for the insert), ``project_id``, ``run_id``, ``stage``.
    Pass a key as None to clear it."""
    ctx = dict(_llm_ctx.get())
    for k, v in kwargs.items():
        if v is None:
            ctx.pop(k, None)
        else:
            ctx[k] = v
    _llm_ctx.set(ctx)


def clear_llm_context() -> None:
    """Reset the context. Job handlers MUST call this in their ``finally`` — request/worker
    threads are pooled and reused, and a stale run_id would mis-attribute later calls."""
    _llm_ctx.set({})


def get_llm_context() -> dict:
    return dict(_llm_ctx.get())


# Which AGENT a pipeline stage belongs to — derived at write time so per-agent spend
# ($/qa vs $/implementer vs $/reviewer) is a plain GROUP BY, no joins (BOSS-280 P1).
STAGE_ROLES = {
    "standards": "architect", "spec": "architect", "plan": "architect",
    "review_plan": "architect", "design": "architect",
    "write_tests": "qa",
    "implement": "software_engineer", "repair": "software_engineer",
    "review_code": "reviewer", "consistency_check": "reviewer",
    "security_check": "security_reviewer",
    "check_tests": "qa", "run_all_tests": "qa",
    "guest": "software_engineer", "solo": "software_engineer",
}


def role_for_stage(stage: Optional[str]) -> Optional[str]:
    return STAGE_ROLES.get((stage or "").strip().lower())


def record_llm_call(
    *,
    engine_name: str,
    usage: Optional[dict] = None,
    cost_usd_micros: int = 0,
    latency_ms: int = 0,
    turns: int = 0,
    ok: bool = True,
    error: Optional[str] = None,
    stage: Optional[str] = None,
) -> None:
    """Insert one llm_calls row from the call result + the ambient context. Never raises."""
    try:
        from ..db import begin
        from .openai_adapter import price_for_model

        ctx = _llm_ctx.get()
        u = usage or {}
        model = str(u.get("model") or "")
        # Self-reported cost (Claude CLI) is exact; otherwise the price table estimated it,
        # and a model absent from the table means the DEFAULT_PRICE fallback was used.
        price_estimated = False
        if u.get("cost_usd_micros") is None and cost_usd_micros:
            _, _, known = price_for_model(model)
            price_estimated = not known
        with begin(ctx.get("db_engine")) as c:
            c.execute(
                text("""
                    INSERT INTO llm_calls (
                        id, project_id, code_task_run_id, stage, engine, model, role,
                        prompt_tokens, completion_tokens, total_tokens,
                        cache_read_tokens, cache_write_tokens, cost_usd_micros,
                        price_estimated, latency_ms, retries, turns, ok, error)
                    VALUES (:id, :p, :run, :stage, :eng, :model, :role, :pt, :ct, :tt,
                            :crt, :cwt, :cost, :pe, :lat, :retries, :turns, :ok, :err)
                """),
                {
                    "id": f"llmc_{uuid4().hex[:20]}",
                    "p": ctx.get("project_id"),
                    "run": ctx.get("run_id"),
                    "stage": stage or ctx.get("stage"),
                    "role": role_for_stage(stage or ctx.get("stage")),
                    "eng": engine_name,
                    "model": model or None,
                    "pt": int(u.get("prompt_tokens") or 0),
                    "ct": int(u.get("completion_tokens") or 0),
                    "tt": int(u.get("total_tokens") or 0),
                    "crt": int(u.get("cache_read_tokens") or 0),
                    "cwt": int(u.get("cache_write_tokens") or 0),
                    "cost": int(cost_usd_micros or 0),
                    "pe": price_estimated,
                    "lat": int(latency_ms or 0),
                    "retries": int(u.get("retries") or 0),
                    "turns": int(turns or 0),
                    "ok": bool(ok),
                    "err": (error or None) and str(error)[:500],
                },
            )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the pipeline
        log.warning("llm_call telemetry write failed (non-fatal): %s", exc)
