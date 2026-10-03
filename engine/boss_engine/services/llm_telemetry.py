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

# {db_engine (sqlalchemy), project_id, run_id, stage, act_id, role_key, settings}
_llm_ctx: ContextVar[dict] = ContextVar("llm_call_context", default={})


def set_llm_context(**kwargs) -> None:
    """Merge fields into the current thread's LLM-call context. Recognised keys:
    ``db_engine`` (SQLAlchemy engine for the insert), ``project_id``, ``run_id``, ``stage``,
    ``act_id`` and ``role_key`` (the agent making the call; role_for_stage() is only the
    fallback when no role_key is set). Pass a key as None to clear it."""
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
    "standards": "chief_architect", "spec": "chief_architect", "plan": "chief_architect",
    "review_plan": "chief_architect", "design": "chief_architect",
    "write_tests": "qa",
    "implement": "software_engineer", "repair": "software_engineer",
    "review_code": "adversarial_reviewer", "consistency_check": "adversarial_reviewer",
    "security_check": "security_reviewer",
    "check_tests": "qa", "run_all_tests": "qa",
    "guest": "software_engineer", "solo": "software_engineer",
}

#: Role names this map used to write, and the registered role each one actually meant.
#:
#: BOSS attributed work to "architect" and "reviewer" while its own registry held
#: "chief_architect" and "adversarial_reviewer". Nothing joined them, so that spend could never
#: be matched to an agent and the org chart showed two agents doing significant work with no
#: trust score, no autonomy level and no violation tracking -- while the registered agents that
#: WERE those same reviewers sat idle beside them.
#:
#: The map above is corrected, so new rows are attributed to the registered names. Historical
#: rows keep what they were written with, and are folded in here rather than by rewriting
#: months of cost history -- a bulk UPDATE over the ledger to fix a display problem is a much
#: worse trade than a six-line lookup.
ROLE_ALIASES = {
    "architect": "chief_architect",
    "reviewer": "adversarial_reviewer",
}


def canonical_role(role: Optional[str]) -> Optional[str]:
    """The registered role a recorded role name refers to."""
    if not role:
        return None
    return ROLE_ALIASES.get(role.strip().lower(), role.strip().lower())


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
        # act_id is named only when the call has one, so a store that predates the column
        # (an old sqlite fixture, a not-yet-converged database) still records the call.
        with begin(ctx.get("db_engine")) as c:
            c.execute(
                text("""
                    INSERT INTO llm_calls (
                        id, project_id, code_task_run_id, stage, engine, model, role,
                        prompt_tokens, completion_tokens, total_tokens,
                        cache_read_tokens, cache_write_tokens, cost_usd_micros,
                        price_estimated, latency_ms, retries, turns, ok, error{act_col})
                    VALUES (:id, :p, :run, :stage, :eng, :model, :role, :pt, :ct, :tt,
                            :crt, :cwt, :cost, :pe, :lat, :retries, :turns, :ok, :err{act_val})
                """.format(act_col=", act_id" if ctx.get("act_id") else "",
                           act_val=", :act" if ctx.get("act_id") else "")),
                {
                    "id": f"llmc_{uuid4().hex[:20]}",
                    "p": ctx.get("project_id"),
                    "run": ctx.get("run_id"),
                    "stage": stage or ctx.get("stage"),
                    "role": (canonical_role(ctx.get("role_key"))
                             or role_for_stage(stage or ctx.get("stage"))),
                    "act": ctx.get("act_id"),
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
            # A PROVIDER'S CAPACITY IS A GOVERNED FACT, AND THIS IS WHERE IT IS OBSERVED.
            #
            # Every engine already reports here, which makes it the one place that can know
            # "codex is out of capacity until Saturday" without a copy per adapter. Until this
            # existed, that sentence lived only in a container log: a subscription hit its
            # weekly cap mid-run, the panel stalled, a build filled with red crosses, and the
            # operator — who could have switched models in ten seconds — never saw it at all.
            project_id = ctx.get("project_id")
            if project_id:
                from .provider_limits import record_call_outcome
                record_call_outcome(c, ctx.get("settings"), str(project_id), engine_name,
                                    ok=bool(ok), error=error)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the pipeline
        log.warning("llm_call telemetry write failed (non-fatal): %s", exc)


def record_agent_turns(
    turns: list,
    *,
    engine_name: str,
    model: Optional[str] = None,
    stage: Optional[str] = None,
    prompt: Optional[str] = None,
) -> int:
    """Persist a captured agent<->model conversation against the ambient run context.

    Sits beside `record_llm_call` deliberately: the two describe the same invocation at
    different resolutions — one row for the ledger, one row per turn for the operator — and
    resolving project/run/stage/role in one place is what keeps them agreeing with each other.

    Never raises. A transcript that cannot be written must not fail the build it describes.
    """
    try:
        from ..db import begin
        from .agent_turns import capture_enabled, record_turns

        if not capture_enabled():
            return 0
        ctx = _llm_ctx.get()
        resolved_stage = stage or ctx.get("stage")
        with begin(ctx.get("db_engine")) as c:
            return record_turns(
                c,
                turns,
                project_id=ctx.get("project_id"),
                code_task_run_id=ctx.get("run_id"),
                stage=resolved_stage,
                role_key=role_for_stage(resolved_stage),
                engine=engine_name,
                model=model,
                prompt=prompt,
            )
    except Exception as exc:  # noqa: BLE001 — see above
        log.warning("agent turn capture failed (non-fatal): %s", exc)
        return 0


# ── Operator read APIs (query / rollup over llm_calls) ─────────────────────────

_SUMMARY_GROUP_COLS = {
    "stage": "COALESCE(stage, '?')",
    "role": "COALESCE(role, '?')",
    "model": "COALESCE(model, '?')",
    "engine": "COALESCE(engine, '?')",
}


def list_llm_calls(
    connection,
    *,
    project_id: str,
    stage: Optional[str] = None,
    role: Optional[str] = None,
    model: Optional[str] = None,
    engine_name: Optional[str] = None,
    ok: Optional[bool] = None,
    code_task_run_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List llm_calls rows for a project with optional filters (operator read path)."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    clauses = ["project_id = :p"]
    params: dict = {"p": project_id.strip(), "lim": limit, "off": offset}
    if stage:
        clauses.append("stage = :stage")
        params["stage"] = stage
    if role:
        clauses.append("role = :role")
        params["role"] = role
    if model:
        clauses.append("model = :model")
        params["model"] = model
    if engine_name:
        clauses.append("engine = :eng")
        params["eng"] = engine_name
    if ok is not None:
        clauses.append("ok = :ok")
        params["ok"] = bool(ok)
    if code_task_run_id:
        clauses.append("code_task_run_id = :run")
        params["run"] = code_task_run_id

    where = " AND ".join(clauses)
    total = connection.execute(
        text(f"SELECT count(*) FROM llm_calls WHERE {where}"), params
    ).scalar() or 0
    rows = connection.execute(
        text(
            f"""
            SELECT id, project_id, code_task_run_id, stage, engine, model, role,
                   prompt_tokens, completion_tokens, total_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd_micros,
                   price_estimated, latency_ms, retries, turns, ok, error, created_at
            FROM llm_calls
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :lim OFFSET :off
            """
        ),
        params,
    ).mappings().all()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("created_at") is not None:
            d["created_at"] = d["created_at"].isoformat() if hasattr(d["created_at"], "isoformat") else str(d["created_at"])
        items.append(d)
    return {
        "project_id": project_id.strip(),
        "count": len(items),
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def summarize_llm_calls(
    connection,
    *,
    project_id: str,
    group_by: str = "stage",
) -> dict:
    """Roll up llm_calls spend/tokens by stage|role|model|engine."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    key = (group_by or "stage").strip().lower()
    if key not in _SUMMARY_GROUP_COLS:
        raise ValueError(
            f"group_by must be one of {sorted(_SUMMARY_GROUP_COLS)}; got {group_by!r}."
        )
    col = _SUMMARY_GROUP_COLS[key]
    rows = connection.execute(
        text(
            f"""
            SELECT {col} AS group_key,
                   count(*) AS calls,
                   COALESCE(SUM(cost_usd_micros), 0) AS cost_usd_micros,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(CASE WHEN ok THEN 1 ELSE 0 END), 0) AS ok_calls,
                   COALESCE(SUM(CASE WHEN NOT ok THEN 1 ELSE 0 END), 0) AS failed_calls,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
            FROM llm_calls
            WHERE project_id = :p
            GROUP BY 1
            ORDER BY cost_usd_micros DESC
            """
        ),
        {"p": project_id.strip()},
    ).mappings().all()
    groups = []
    total_cost = 0
    total_calls = 0
    for r in rows:
        cost = int(r["cost_usd_micros"] or 0)
        calls = int(r["calls"] or 0)
        total_cost += cost
        total_calls += calls
        groups.append(
            {
                "group_key": str(r["group_key"]),
                "calls": calls,
                "cost_usd_micros": cost,
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "ok_calls": int(r["ok_calls"] or 0),
                "failed_calls": int(r["failed_calls"] or 0),
                "avg_latency_ms": float(r["avg_latency_ms"] or 0),
            }
        )
    return {
        "project_id": project_id.strip(),
        "group_by": key,
        "total_calls": total_calls,
        "total_cost_usd_micros": total_cost,
        "groups": groups,
    }
