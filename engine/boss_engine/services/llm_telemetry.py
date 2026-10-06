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

# {db_engine (sqlalchemy), project_id, run_id, stage, seat?, act_id?, role_key?, workflow_run_id?, settings?}
_llm_ctx: ContextVar[dict] = ContextVar("llm_call_context", default={})


def set_llm_context(**kwargs) -> None:
    """Merge fields into the current thread's LLM-call context. Recognised keys:
    ``db_engine`` (SQLAlchemy engine for the insert), ``project_id``, ``run_id``, ``stage``,
    and the Agent World keys ``act_id`` / ``role_key`` / ``workflow_run_id`` (who is spending:
    set by a caller that already holds its act -- a chat turn, a room seat -- otherwise the
    stage's act is found per call; role_for_stage() is only the fallback when no role is known).
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


def attribute_llm_calls(connection, project_id: str, hours: float) -> list[dict]:
    """WHO SPENT EACH CALL, decided once (org-view spec §6.1): every llm_calls row in the
    window, grouped, with the agent it belongs to.

    In order: the act the call was made under (`act_id` → that act's role); else the role the
    call recorded (`llm_calls.role`); else its stage's owner (STAGE_ROLES); else NOBODY —
    `role: None`, shown as "not tied to an agent", never folded into a neighbour.

    A call on a subscription (`included_in_subscription`) is counted as a call and never as
    money: `metered_micros` is the sum over the others only. Organization and the Directory
    (console_v2_workforce._llm_work) both read this, so they cannot disagree on an agent's $.

    Each row: {role, stage, model, included, calls, turns, tokens, micros, metered_micros,
    retries, ok_calls, last_at}."""
    rows = connection.execute(
        text(
            """
            SELECT a.role_key AS act_role, c.role AS call_role, c.stage, c.model,
                   COALESCE(c.included_in_subscription, false) AS included,
                   count(*)                          AS calls,
                   COALESCE(sum(c.turns), 0)         AS turns,
                   COALESCE(sum(c.total_tokens), 0)  AS tokens,
                   COALESCE(sum(c.cost_usd_micros), 0) AS micros,
                   COALESCE(sum(c.retries), 0)       AS retries,
                   count(*) FILTER (WHERE c.ok)      AS ok_calls,
                   max(c.created_at)                 AS last_at
            FROM llm_calls c
            LEFT JOIN agent_acts a ON a.act_id = c.act_id
            WHERE c.project_id = :p
              AND c.created_at > now() - make_interval(secs => :s)
            GROUP BY 1, 2, 3, 4, 5
            """
        ),
        {"p": project_id, "s": float(hours) * 3600.0},
    ).mappings().all()
    out: list[dict] = []
    for r in rows:
        role = (canonical_role(r.get("act_role")) or canonical_role(r.get("call_role"))
                or canonical_role(role_for_stage(r.get("stage"))))
        micros = int(r.get("micros") or 0)
        included = bool(r.get("included"))
        out.append({
            "role": role, "stage": str(r.get("stage") or "").strip().lower() or None,
            "model": r.get("model"), "included": included,
            "calls": int(r.get("calls") or 0), "turns": int(r.get("turns") or 0),
            "tokens": int(r.get("tokens") or 0), "micros": micros,
            "metered_micros": 0 if included else micros,
            "retries": int(r.get("retries") or 0), "ok_calls": int(r.get("ok_calls") or 0),
            "last_at": r.get("last_at"),
        })
    return out


def _signed_by(c, ctx: dict, stage: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """WHICH ACT THIS CALL IS PART OF, and so which agent spent it.

    The context's own act wins (a caller that opened one says so). Otherwise a build stage's
    act is found — opened on first use — from the code task and stage, so every call a build
    makes is signed without each of its fifteen call sites knowing the ledger exists. The role
    written on the row is the ACT's role: STAGE_ROLES is only the fallback when there is no act,
    because the agent the operator put on a build is who spent its implement stage.
    """
    named = canonical_role(ctx.get("role_key"))
    if ctx.get("act_id"):
        return str(ctx["act_id"]), named or role_for_stage(stage)
    if not ctx.get("run_id"):
        return None, named or role_for_stage(stage)
    from . import agent_acts

    found = agent_acts.quietly(c, "sign a model call", agent_acts.stage_act,
                               project_id=ctx.get("project_id"),
                               code_task_run_id=str(ctx["run_id"]), stage=stage,
                               # A seat is a review panel's; a pooled thread that sat on the
                               # panel and then ran another stage must not carry it over.
                               seat=ctx.get("seat") if (stage or "") == "review_code" else None)
    if not found:
        return None, named or role_for_stage(stage)
    return found["act_id"], canonical_role(found.get("role_key"))


def engine_call_is_included(engine_name: Optional[str], usage: Optional[dict]) -> bool:
    """Whether a call on `engine_name` ran on included capacity (a subscription or a local
    model). The adapter's own `billing` stamp wins; otherwise the engine decides, by the same
    list the router reserves nothing for (model_routing.UNMETERED)."""
    from .included_spend import usage_is_included
    from .model_routing import UNMETERED

    if isinstance(usage, dict) and usage.get("billing"):
        return usage_is_included(usage)
    name = str(engine_name or "").strip().lower()
    if name.startswith("guest:"):
        name = name.split(":", 1)[1]
    return name in UNMETERED


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
    route_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> None:
    """Insert one llm_calls row from the call result + the ambient context. Never raises.
    `route_id` / `project_id` name the model router's decision and project when the caller
    holds them (a routed chat call), over whatever the ambient context says."""
    try:
        from ..db import begin
        from .openai_adapter import price_for_model

        ctx = dict(_llm_ctx.get())
        if project_id and not ctx.get("project_id"):
            ctx["project_id"] = project_id
        u = usage or {}
        model = str(u.get("model") or "")
        # Self-reported cost (Claude CLI) is exact; otherwise the price table estimated it,
        # and a model absent from the table means the DEFAULT_PRICE fallback was used.
        price_estimated = False
        if u.get("cost_usd_micros") is None and cost_usd_micros:
            _, _, known = price_for_model(model)
            price_estimated = not known
        # cost_usd_micros is what the call is worth at list price. On a subscription or a local
        # model that is not money (included_spend.py): the row says so, and the code task's
        # budget settlement (code_task_service) charges only the rows that are money.
        included = engine_call_is_included(engine_name, u)
        with begin(ctx.get("db_engine")) as c:
            act_id, role = _signed_by(c, ctx, stage or ctx.get("stage"))
            # The act column is written only when there is an act, so a store that predates
            # the ledger (or a test's minimal table) records the call exactly as before.
            signed = (", act_id", ", :act") if act_id else ("", "")
            # The route that chose this call's model (model_router): by stage, else the last.
            routes = ctx.get("routes") if isinstance(ctx.get("routes"), dict) else {}
            route_id = route_id or routes.get(stage or ctx.get("stage") or "") \
                or ctx.get("route_id")
            if route_id:
                signed = (signed[0] + ", route_id", signed[1] + ", :route")
            c.execute(
                text(f"""
                    INSERT INTO llm_calls (
                        id, project_id, code_task_run_id, stage, engine, model, role,
                        prompt_tokens, completion_tokens, total_tokens,
                        cache_read_tokens, cache_write_tokens, cost_usd_micros,
                        price_estimated, latency_ms, retries, turns, ok, error,
                        included_in_subscription{signed[0]})
                    VALUES (:id, :p, :run, :stage, :eng, :model, :role, :pt, :ct, :tt,
                            :crt, :cwt, :cost, :pe, :lat, :retries, :turns, :ok, :err,
                            :included{signed[1]})
                """),
                {
                    "id": f"llmc_{uuid4().hex[:20]}",
                    "p": ctx.get("project_id"),
                    "run": ctx.get("run_id"),
                    "stage": stage or ctx.get("stage"),
                    "role": role,
                    "act": act_id,
                    "route": route_id,
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
                    "included": included,
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
                # SUBSCRIPTION HEADROOM (model_router.headroom): the CLI's own window numbers
                # and its limit sentences, folded into the source's observed windows.
                # Pooled per ACCOUNT, written only when a window's state changes; a success
                # clears the rejected windows it proves wrong (model_router.accounts).
                from .model_router.policy import SUBSCRIPTION_ENGINES

                if str(engine_name) in SUBSCRIPTION_ENGINES:
                    from .model_router.store import observe

                    observe(c, ctx.get("settings"), str(project_id), str(engine_name),
                            event=u.get("rate_limit") if isinstance(u.get("rate_limit"), dict)
                            else None, error=None if ok else error,
                            ok_model=(model or "") if ok and not u.get("rate_limit") else None)
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
            act_id, role = _signed_by(c, ctx, resolved_stage)
            return record_turns(
                c,
                turns,
                project_id=ctx.get("project_id"),
                code_task_run_id=ctx.get("run_id"),
                stage=resolved_stage,
                role_key=role,
                act_id=act_id,
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
    since: Optional[str] = None,
) -> dict:
    """List llm_calls rows for a project with optional filters (operator read path).

    `since` (ISO time) keeps calls made at or after it — Observability › Models' group drawer
    reads the window it is showing."""
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
    if since:
        clauses.append("created_at >= CAST(:since AS timestamptz)")
        params["since"] = since

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
