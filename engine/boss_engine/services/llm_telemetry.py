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
from contextlib import contextmanager
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


#: WHO SPEAKS IN A TURN NO AGENT WAS ADDRESSED IN: BOSS itself, said so (never a blank name on a
#: rule, a learning or a decision recorded in that turn). An addressed turn names its agent.
GENERAL_ROLE = "boss"
GENERAL_NAME = "BOSS (general)"


def speaker() -> dict:
    """Who is speaking in the call in progress, for provenance: the addressed agent of a chat
    turn (`speaker_role`, `agent_name`, `agent_id`, set by AgentService.chat), else the stage's
    agent (`role_key`), with its name and Passport id when the caller knows them. Empty when
    nothing set either: a worker's own provenance rules apply then."""
    ctx = dict(_llm_ctx.get())
    role = ctx.get("speaker_role") or ctx.get("role_key")
    return {"agent_role": role or None,
            "agent_name": ctx.get("agent_name"),
            "agent_id": ctx.get("agent_id")}


def clear_llm_context() -> None:
    """Reset the context. Job handlers MUST call this in their ``finally`` — request/worker
    threads are pooled and reused, and a stale run_id would mis-attribute later calls."""
    _llm_ctx.set({})


def get_llm_context() -> dict:
    return dict(_llm_ctx.get())


# ── Transient retries are ONE call in the ledger (BOSSAPP-1010) ───────────────────────────────
#
# The transient-retry wrapper (agent/transient._with_transient_retry) repeats the SAME logical
# call on a provider hiccup. Each attempt used to land its own llm_calls row — two failed twins
# beside the success — so one answer looked like three calls. Inside a retry scope, a FAILED
# attempt's row is deferred: retried, it is folded into the final row's `retries`; not retried
# (a non-transient error, attempts exhausted), it is written on the way out, with the fold
# count it accumulated. Outside a scope nothing changes: a failure records at once, as the
# BOSSAPP-998 conformance suite pins.

_retry_scope: ContextVar[Optional[dict]] = ContextVar("llm_retry_scope", default=None)


@contextmanager
def transient_retry_scope():
    """One logical call that may retry inside. Opened by the transient-retry wrapper around its
    whole attempt loop; the seam defers failed rows into it, `fold_retry()` folds one on each
    retry, and whatever failure is still pending when the scope closes is written exactly once."""
    scope: dict = {"retries": 0, "pending": None}
    token = _retry_scope.set(scope)
    try:
        yield scope
    finally:
        _retry_scope.reset(token)
        pending = scope.get("pending")
        if pending is not None:
            # The last attempt failed and nobody retried it: ITS row, once, carrying how many
            # folded attempts came before it. Outside the scope now, so it writes immediately.
            record_llm_call(**pending)


def fold_retry() -> bool:
    """The attempt that just failed is about to be RETRIED: fold it into the final row instead
    of recording it. True when a scope absorbed it; False means no scope is open and the
    failure was already recorded the ordinary way (the caller changes nothing)."""
    scope = _retry_scope.get()
    if scope is None:
        return False
    scope["retries"] = int(scope.get("retries") or 0) + 1
    scope["pending"] = None
    return True


# Which AGENT a pipeline stage belongs to — derived at write time so per-agent spend
# ($/qa vs $/implementer vs $/reviewer) is a plain GROUP BY, no joins (BOSS-280 P1).
STAGE_ROLES = {
    "standards": "chief_architect", "spec": "chief_architect", "plan": "chief_architect",
    "review_plan": "chief_architect", "design": "chief_architect",
    "write_tests": "qa",
    "implement": "software_engineer", "repair": "software_engineer",
    "review_code": "adversarial_reviewer", "consistency_check": "adversarial_reviewer",
    "security_check": "security_reviewer",
    # The merged panel session (BOSSAPP-1000): one session sits every review seat; its row is
    # its own stage so review spend is never overstated the way the arbiter's was.
    "review_panel": "adversarial_reviewer",
    "check_tests": "qa", "run_all_tests": "qa",
    # The test arbiter rules on failing tests before an implementer chases them; it used to be
    # ledgered as review_code, which overstated review spend and hid the arbiter's entirely.
    "test_arbitration": "adversarial_reviewer",
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
    # An act that carries no role (a build signed by its root act — a stage outside the
    # pipeline's phase map, like test_arbitration) still writes the stage's known role.
    return found["act_id"], (canonical_role(found.get("role_key")) or named
                             or role_for_stage(stage))


def _ledger_columns(c, ctx: dict, *, act_id: Optional[str], row_id: str) -> dict:
    """The row's run-ledger columns, or {} — always {} while BOSS_LEDGER_EVENTS is off and no
    kernel run is recording, without touching the database. In a savepoint: a database that
    predates the ledger loses the columns, never the call's row."""
    from ..kernel import ledger

    if not (ledger.enabled() or ctx.get("ledger_seq") is not None):
        return {}
    try:
        with c.begin_nested():
            return ledger.llm_call_fields(c, ctx, act_id=act_id, row_id=row_id)
    except Exception as exc:  # noqa: BLE001 — the ledger copy never costs the spend row
        log.warning("run ledger: llm_calls ledger columns not written (non-fatal): %s", exc)
        return {}


def engine_call_is_included(engine_name: Optional[str], usage: Optional[dict]) -> bool:
    """Whether a call on `engine_name` ran on included capacity (a subscription or a local
    model). The adapter's own `billing` stamp wins; otherwise the engine decides, by the same
    list the router reserves nothing for (model_routing.UNMETERED)."""
    from .included_spend import call_is_included

    return call_is_included(engine_name, usage)


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
    billing: Optional[str] = None,
    connection=None,
    capacity: bool = True,
    _folded_retries: int = 0,
) -> Optional[str]:
    """Insert one llm_calls row from the call result + the ambient context. Never raises.
    `route_id` / `project_id` name the model router's decision and project when the caller
    holds them (a routed chat call), over whatever the ambient context says.

    THIS IS THE ONE RECORDING SEAM (BOSSAPP-998): every model invocation — a build stage, a
    chat turn, a panel seat, a decision, a proof, a gateway-settled call — lands in llm_calls
    through here, exactly once. A caller inside a transaction passes `connection`; the row is
    then written on it inside a savepoint (a failed write never poisons the caller's
    transaction). `capacity=False` skips the provider-capacity observation for a caller that
    observes capacity its own way (the model gateway's entitlement probe).

    Returns the row id when the call was recorded, else None; the id is also published to the
    ambient context as `last_llm_call_id`, so a settlement boundary (setup_service's
    governed_proof) can tell whether the call it paid for already recorded itself."""
    try:
        from ..db import begin
        from .openai_adapter import price_for_model

        scope = _retry_scope.get()
        if scope is not None and not ok:
            # Inside a transient-retry scope a FAILED attempt may be about to be retried as the
            # same logical call (BOSSAPP-1010). Defer its row: fold_retry() absorbs it on a
            # retry; the scope's exit writes it exactly once when nothing retried it.
            scope["pending"] = dict(
                engine_name=engine_name, usage=usage, cost_usd_micros=cost_usd_micros,
                latency_ms=latency_ms, turns=turns, ok=ok, error=error, stage=stage,
                route_id=route_id, project_id=project_id, billing=billing,
                # Deferred to the scope's exit: the caller's connection/transaction may be gone
                # by then, so the deferred row writes on its own short transaction.
                connection=None, capacity=capacity,
                # What this last attempt inherits when it is the one that lands: the
                # attempts already folded before it.
                _folded_retries=int(scope.get("retries") or 0))
            return None

        ctx = dict(_llm_ctx.get())
        if project_id and not ctx.get("project_id"):
            ctx["project_id"] = project_id
        u = dict(usage or {})
        folded = _folded_retries or (int(scope.get("retries") or 0) if scope else 0)
        if folded:
            u["retries"] = int(u.get("retries") or 0) + folded
        # The credential the adapter ran on, for a row whose usage carries no stamp (a failed
        # call): the same answer its successful calls give (included_spend.call_is_included).
        if billing and not u.get("billing"):
            u["billing"] = billing
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
        # EVERY ROW HAS A CLASS (BOSSAPP-999): derived from the stage the boundary named, total
        # by construction — an unknown stage is 'other', never NULL, never a crash.
        from .call_classes import class_of

        call_class = class_of(stage or ctx.get("stage"))
        row_id = f"llmc_{uuid4().hex[:20]}"

        def _write(c) -> None:
            act_id, role = _signed_by(c, ctx, stage or ctx.get("stage"))
            # The act column is written only when there is an act, so a store that predates
            # the ledger (or a test's minimal table) records the call exactly as before.
            signed = (", act_id", ", :act") if act_id else ("", "")
            # The route that chose this call's model (model_router): by stage, else the last.
            routes = ctx.get("routes") if isinstance(ctx.get("routes"), dict) else {}
            route = route_id or routes.get(stage or ctx.get("stage") or "") \
                or ctx.get("route_id")
            if route:
                signed = (signed[0] + ", route_id", signed[1] + ", :route")
            # THE RUN LEDGER (BOSS_LEDGER_EVENTS, kernel/ledger.py): the run, its sequence, its
            # retry lineage and the policies that produced the call. Only the columns that
            # have a value are written, so a store that predates them records as before.
            ledger = _ledger_columns(c, ctx, act_id=act_id, row_id=row_id)
            for column, value in ledger.items():
                if value is not None:
                    cast = f"CAST(:l_{column} AS jsonb)" if column == "policy" else f":l_{column}"
                    signed = (signed[0] + f", {column}", signed[1] + f", {cast}")
            c.execute(
                text(f"""
                    INSERT INTO llm_calls (
                        id, project_id, code_task_run_id, stage, engine, model, role,
                        prompt_tokens, completion_tokens, total_tokens,
                        cache_read_tokens, cache_write_tokens, cost_usd_micros,
                        price_estimated, latency_ms, retries, turns, ok, error,
                        included_in_subscription, call_class{signed[0]})
                    VALUES (:id, :p, :run, :stage, :eng, :model, :role, :pt, :ct, :tt,
                            :crt, :cwt, :cost, :pe, :lat, :retries, :turns, :ok, :err,
                            :included, :call_class{signed[1]})
                """),
                {
                    "id": row_id,
                    **{f"l_{column}": value for column, value in ledger.items()},
                    "p": ctx.get("project_id"),
                    "run": ctx.get("run_id"),
                    "stage": stage or ctx.get("stage"),
                    "role": role,
                    "act": act_id,
                    "route": route,
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
                    "call_class": call_class,
                },
            )
            # BOSSAPP-863: a PAID call is where a scoped budget (org / project / agent /
            # workflow) is checked mid-stage; one past a hard-stop cap tells the run to stop at
            # its next turn (scoped_budgets.after_call; never raises, a no-op with no policy).
            if not included and int(cost_usd_micros or 0) > 0:
                from .run_secret_redaction import current_run_id
                from .scoped_budgets import after_call

                after_call(c, project_id=ctx.get("project_id"), role=role,
                           workflow_run_id=ctx.get("workflow_run_id"),
                           run_id=ctx.get("run_id") or current_run_id())
            # A PROVIDER'S CAPACITY IS A GOVERNED FACT, AND THIS IS WHERE IT IS OBSERVED.
            #
            # Every engine already reports here, which makes it the one place that can know
            # "codex is out of capacity until Saturday" without a copy per adapter. Until this
            # existed, that sentence lived only in a container log: a subscription hit its
            # weekly cap mid-run, the panel stalled, a build filled with red crosses, and the
            # operator — who could have switched models in ten seconds — never saw it at all.
            observed_project = ctx.get("project_id")
            if observed_project and capacity:
                from .provider_limits import record_call_outcome
                record_call_outcome(c, ctx.get("settings"), str(observed_project), engine_name,
                                    ok=bool(ok), error=error)
                # SUBSCRIPTION HEADROOM (model_router.headroom): the CLI's own window numbers
                # and its limit sentences, folded into the source's observed windows.
                # Pooled per ACCOUNT, written only when a window's state changes; a success
                # clears the rejected windows it proves wrong (model_router.accounts).
                from .model_router.policy import SUBSCRIPTION_ENGINES

                if str(engine_name) in SUBSCRIPTION_ENGINES:
                    from .model_router.store import observe

                    observe(c, ctx.get("settings"), str(observed_project), str(engine_name),
                            event=u.get("rate_limit") if isinstance(u.get("rate_limit"), dict)
                            else None, error=None if ok else error,
                            ok_model=(model or "") if ok else None)

        if connection is not None:
            # On the caller's transaction, inside a savepoint: a failed telemetry write rolls
            # back to here and never aborts the settlement it rides on. A settlement-side
            # write publishes no marker: `last_llm_call_id` means "the CALL recorded itself",
            # and the ambient context goes back exactly as the caller left it.
            with connection.begin_nested():
                _write(connection)
        else:
            with begin(ctx.get("db_engine")) as c:
                _write(c)
            set_llm_context(last_llm_call_id=row_id)
        # BOSS'S OWN TELEMETRY (BOSSAPP-867): the same call as an OTel GenAI span and metrics
        # counters. Both are off unless configured, and then this returns at once.
        from .self_telemetry import record_model_span

        record_model_span(engine_name=engine_name, model=model, usage=u, latency_ms=latency_ms,
                          ok=ok, error=error, stage=stage or ctx.get("stage"),
                          cost_usd_micros=int(cost_usd_micros or 0))
        return row_id
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the pipeline
        log.warning("llm_call telemetry write failed (non-fatal): %s", exc)
    return None


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
