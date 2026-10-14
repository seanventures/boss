"""WHAT WILL THIS RUN COST, AND DO THE CAPS LET IT? — the pre-check's `budget.run_cost`.

WHY THIS EXISTS
---------------
`budget.headroom` asks whether each step's RESERVATION fits the day's cap. A reservation is a
hold, not a forecast: on a subscription nothing is money and nobody noticed. On 2026-10-14 the
laptop moved from a Claude subscription to a metered API key with a $10/day cap and a
$0.10/minute burst cap. Every reservation still "fit"; a HelpDesk run spends far more than $10
in a day and far more than $0.10 in a minute, so it would have been throttled for hours and then
refused — none of which the pre-check said.

So this estimates the WHOLE run from history and compares it with what the caps leave:

  * HISTORY. `llm_calls` (one row per model invocation: stage, engine, model, tokens, latency)
    for the last runs of this workflow (calls on this project while each run was live), or,
    failing that, the last builds of this project (calls by `code_task_run_id`). Rows recorded
    on a subscription carry their tokens too ($0 then, but the tokens are what matter now).
  * PRICED AT WHAT ROUTING WILL USE NOW. Each call's stage goes to the route the worker would
    take today (`engine_startup.build_routes` for build/review stages — the StageRouter's own
    Fixed answer — and `engine_startup.chat_route` for everything else), and its tokens are
    priced with the shared price table (`openai_adapter.estimate_cost_usd_micros`) at that
    route's model. A route on a subscription or a local model is $0 marginal and says so.
  * AGAINST THE CAPS. The estimate against what is left today and this month (`will_fail`
    when it does not fit), and its spend rate — while the run is active, and at its peak with
    parallel sessions — against the burst cap (`unknown`: slow, with the minutes of waiting).

Pure where it can be (`estimate_run`, `judge`), so the tests drive the verdicts with fixtures.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

#: How many past runs (or builds) the estimate reads, and how far back.
HISTORY_RUNS = 5
HISTORY_DAYS = 60
#: Source kinds whose calls cost no money at the margin (included_spend.py).
FREE_KINDS = frozenset({"subscription", "local"})
#: A spend rate this many times the burst cap, or this many minutes of waiting, is "slow".
FAR_ABOVE = 2.0
SLOW_MINUTES = 5.0
#: The burst window (budget.BURST_WINDOW_SECONDS), in minutes.
WINDOW_MINUTES = 1.0
FIX_DAILY = "Raise the daily cap in Govern → Pace (person), or start after the day resets"
FIX_MONTHLY = "Raise the monthly cap in Govern → Pace (person)"
FIX_BURST = "Raise the burst cap in Govern → Pace (person), or accept the waiting"


def _money(micros: float) -> str:
    return f"${micros / 1_000_000:.2f}"


# ── History ───────────────────────────────────────────────────────────────────────────────────

_CALL_COLUMNS = ("stage, engine, model, prompt_tokens, completion_tokens, cache_read_tokens, "
                 "cache_write_tokens, cost_usd_micros, latency_ms, turns, created_at, "
                 "included_in_subscription")


def _rows(connection: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(sql), dict(params)).mappings().all()]


def history(connection: Any, project_id: str, workflow_key: str, *,
            runs: int = HISTORY_RUNS, days: int = HISTORY_DAYS) -> dict[str, Any]:
    """{basis, runs: [{id, status, calls: [...]}]}: the last runs of this workflow that recorded
    model calls (completed ones first), else the last builds of this project. Reads only."""
    past = _rows(connection, (
        "SELECT r.id, r.status, r.created_at, r.updated_at FROM workflow_runs r JOIN "
        "workflow_definitions d ON d.id = r.workflow_definition_id WHERE r.project_id = :p "
        "AND d.workflow_key = :k AND r.status IN ('completed','failed','deadlettered',"
        "'cancelled') AND r.created_at > now() - make_interval(days => :d) "
        "ORDER BY (r.status = 'completed') DESC, r.created_at DESC LIMIT :n"),
        {"p": project_id, "k": workflow_key, "d": days, "n": runs * 2})
    found = []
    for run in past:
        calls = _rows(connection, (
            f"SELECT {_CALL_COLUMNS} FROM llm_calls WHERE project_id = :p AND created_at >= :a "
            "AND created_at <= :b ORDER BY created_at"),
            {"p": project_id, "a": run["created_at"], "b": run["updated_at"]})
        if calls:
            found.append({"id": run["id"], "status": run["status"], "calls": calls})
        if len(found) >= runs:
            break
    if found:
        return {"basis": "workflow", "runs": found}
    builds = _rows(connection, (
        "SELECT id, status FROM code_task_runs WHERE project_id = :p AND status IN "
        "('completed','failed') AND created_at > now() - make_interval(days => :d) "
        "ORDER BY (status = 'completed') DESC, created_at DESC LIMIT :n"),
        {"p": project_id, "d": days, "n": runs * 2})
    for build in builds:
        calls = _rows(connection, (
            f"SELECT {_CALL_COLUMNS} FROM llm_calls WHERE code_task_run_id = :r "
            "ORDER BY created_at"), {"r": build["id"]})
        if calls:
            found.append({"id": build["id"], "status": build["status"], "calls": calls})
        if len(found) >= runs:
            break
    return {"basis": "builds" if found else "none", "runs": found}


# ── Routing now ───────────────────────────────────────────────────────────────────────────────

def _build_lane_stages() -> frozenset[str]:
    from .engine_startup import BUILD_STAGES, REVIEW_STAGES
    from .model_router.policy import BUILD_STEPS
    from .model_router.tiers import CODING_STEPS

    return frozenset(BUILD_STEPS) | CODING_STEPS | frozenset(BUILD_STAGES) \
        | frozenset(REVIEW_STAGES) | {"solo", "simple"}


def routes_now(connection: Any, settings: Any, project_id: str,
               stages: Sequence[str]) -> dict[str, dict[str, Any]]:
    """{stage: {engine, model, source, kind}} — the route each stage takes today, through the
    worker's own routing (engine_startup), and what its source costs (subscription | local |
    metered | env | …). A stage that is not a build stage takes the chat job's route."""
    from . import engine_startup, model_routing
    from .setup_service import connected_engines

    s = model_routing.routed_settings(connection, settings, project_id)
    try:
        connected = connected_engines(connection, s, project_id)
    except Exception:  # noqa: BLE001
        connected = {}

    def kind_of(engine: str) -> str:
        info = connected.get(engine) or {}
        kind = str(info.get("kind") or "")
        return model_routing._env_kind(engine, info) if kind == "env" else kind  # noqa: SLF001

    lane = _build_lane_stages()
    build = sorted({st for st in stages if st in lane})
    out: dict[str, dict[str, Any]] = {}
    if build:
        for r in engine_startup.build_routes(connection, settings, project_id, {}, build):
            out[r.stage] = {"engine": r.engine, "model": r.model, "source": r.source_id,
                            "kind": kind_of(r.engine)}
    chat = engine_startup.chat_route(connection, settings, project_id)
    as_chat = {"engine": chat.engine, "model": chat.model, "source": chat.source_id,
               "kind": kind_of(chat.engine)}
    for st in stages:
        out.setdefault(st, dict(as_chat))
    return out


# ── The estimate (pure) ───────────────────────────────────────────────────────────────────────

def _tokens(call: Mapping[str, Any]) -> dict[str, int]:
    return {k: int(call.get(k) or 0) for k in ("prompt_tokens", "completion_tokens",
                                               "cache_read_tokens", "cache_write_tokens")}


def _priceable(model: str, then: str) -> str:
    """A model name the price table knows: a context suffix ("claude-opus-4-8[1m]") dropped,
    and an engine's tier alias ("haiku") read as the priced model it names — the one the call
    ran last time when it is that family, else the newest of that family in the table."""
    import re

    from .openai_adapter import PRICE_PER_MTOK

    name = re.sub(r"\[[^\]]*\]$", "", model.strip())
    if name.lower() in ("haiku", "sonnet", "opus"):
        if name.lower() in then.lower():
            return then
        return next((k for k in PRICE_PER_MTOK if k.startswith(f"claude-{name.lower()}")), name)
    return name


def price_call(call: Mapping[str, Any], route: Optional[Mapping[str, Any]]
               ) -> tuple[int, int, str]:
    """(money, list value, the model it was priced at) for one past call, run today on `route`.
    A route that names no model ("its default model") is priced at what the call ran last
    time — the engine's default then is the best evidence of its default now."""
    from .openai_adapter import estimate_cost_usd_micros, price_for_model

    route = route or {}
    then = _priceable(str(call.get("model") or ""), "")
    model = _priceable(str(route.get("model") or ""), then)
    tokens = _tokens(call)
    if any(tokens.values()):
        value = estimate_cost_usd_micros({"model": model or then, **tokens})
    else:
        # A call that recorded only its cost (no tokens): scaled by the two models' prices.
        old_in, _, _ = price_for_model(then)
        new_in, _, _ = price_for_model(model or then)
        value = int(int(call.get("cost_usd_micros") or 0) * (new_in / old_in if old_in else 1))
    free = str(route.get("kind") or "") in FREE_KINDS if route else bool(
        call.get("included_in_subscription"))
    return (0 if free else int(value)), int(value), (model or then or "unknown")


def _when(value: Any) -> Optional[datetime]:
    return value if isinstance(value, datetime) else None


def _timing(calls: Sequence[Mapping[str, Any]]) -> tuple[float, float, int]:
    """(active minutes = model time summed, span minutes = first start to last end, the most
    calls that overlapped). A call's row is written when it ends; it started `latency` before."""
    spans = []
    for c in calls:
        end = _when(c.get("created_at"))
        lat = max(0, int(c.get("latency_ms") or 0))
        if end is not None:
            spans.append((end - timedelta(milliseconds=lat), end))
    active = sum(max(0, int(c.get("latency_ms") or 0)) for c in calls) / 60_000
    if not spans:
        return active, active, 1
    span = (max(e for _, e in spans) - min(s for s, _ in spans)).total_seconds() / 60
    events = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans],
                    key=lambda x: (x[0], x[1]))
    live = peak = 0
    for _, step in events:
        live += step
        peak = max(peak, live)
    return active, max(span, active / max(peak, 1)), max(peak, 1)


def estimate_run(run: Mapping[str, Any], routes: Mapping[str, Mapping[str, Any]]
                 ) -> dict[str, Any]:
    """One past run, priced as it would be today."""
    money = value = 0
    turn_costs: list[float] = []
    by_stage: dict[str, int] = {}
    models: dict[str, str] = {}
    for call in run.get("calls") or []:
        stage = str(call.get("stage") or "model_call")
        m, v, model = price_call(call, routes.get(stage))
        money += m
        value += v
        by_stage[stage] = by_stage.get(stage, 0) + m
        models[stage] = model
        if m:
            turn_costs.append(m / max(1, int(call.get("turns") or 0)))
    active, span, peak = _timing(run.get("calls") or [])
    return {"id": run.get("id"), "status": run.get("status"), "money": money, "value": value,
            "by_stage": by_stage, "models": models, "active_min": active, "span_min": span,
            "peak_parallel": peak,
            "typical_turn": statistics.median(turn_costs) if turn_costs else 0.0}


def estimate(found: Mapping[str, Any], routes: Mapping[str, Mapping[str, Any]]
             ) -> Optional[dict[str, Any]]:
    """The median past run priced today, with the range — or None with no history."""
    runs = [estimate_run(r, routes) for r in found.get("runs") or []]
    if not runs:
        return None
    runs.sort(key=lambda r: r["money"])
    mid = dict(runs[len(runs) // 2])
    mid.update({"basis": found.get("basis"), "n": len(runs), "low": runs[0]["money"],
                "high": runs[-1]["money"],
                "all_failed": all(r["status"] != "completed" for r in runs)})
    return mid


# ── The verdict (pure) ────────────────────────────────────────────────────────────────────────

_RANK = {"will_fail": 0, "unknown": 1, "fixable": 2, "ready": 3}


def judge(est: Optional[Mapping[str, Any]], budget: Optional[Mapping[str, Any]],
          burst_cap: int, max_concurrent: int,
          routes: Mapping[str, Mapping[str, Any]]) -> tuple[str, str, str]:
    """(verdict, evidence, action) for `budget.run_cost`."""
    if est is None:
        return ("unknown", "no past run of this workflow and no build of this project recorded "
                "model usage, so the run's cost is not estimated — assuming it fits the caps",
                "Run it once and watch Costs; the next pre-check estimates from that run.")
    money, value = int(est["money"]), int(est["value"])
    where = ("past runs of this workflow" if est.get("basis") == "workflow"
             else "past builds of this project")
    head = (f"the run needs ~{_money(money)} (median of {est['n']} {where}, "
            f"{_money(est['low'])}–{_money(est['high'])}), priced at today's routes: "
            + "; ".join(sorted({f"{r.get('engine') or 'none'} / {r.get('model') or 'default'}"
                                f" via {r.get('source') or 'the install environment'}"
                                f" ({r.get('kind') or 'unknown kind'})"
                                for r in routes.values()})))
    if est.get("all_failed"):
        head += "; every past run it reads failed, so a full run likely costs more"
    if money == 0:
        return ("ready", head + f" — $0 marginal: every call routes to a subscription or local "
                f"model (~{_money(value)} at API list price)", "")
    verdict, notes, actions = "ready", [], []
    periods = dict((budget or {}).get("periods") or {})
    for name, label, fix in (("day", "today", FIX_DAILY), ("month", "this month", FIX_MONTHLY)):
        p = periods.get(name) or {}
        cap = int(p.get("cap_usd_micros") or 0)
        if not cap:
            continue
        left = max(0, cap - int(p.get("actual_usd_micros") or 0)
                   - int(p.get("reserved_usd_micros") or 0))
        notes.append(f"{_money(left)} left {label} under a {_money(cap)} cap")
        if money > left:
            verdict = "will_fail"
            actions.append(fix + (f" ({p['resets_at_label']})" if name == "day"
                                  and p.get("resets_at_label") else "") + ".")
    if burst_cap:
        span = max(est["span_min"], 1e-6)
        active = max(est["active_min"], 1e-6)
        rate = money / span
        lanes = max(1, min(int(est["peak_parallel"]), max_concurrent or est["peak_parallel"]))
        peak = money / active * lanes
        throttled = money / burst_cap * WINDOW_MINUTES
        wait = max(0.0, throttled - est["span_min"])
        notes.append(f"spend rate ~{_money(rate)}/min while active (~{_money(peak)}/min at peak "
                     f"with {lanes} parallel session(s)) against a {_money(burst_cap)}/min "
                     "burst cap")
        turn = float(est.get("typical_turn") or 0)
        if turn > burst_cap:
            notes.append(f"a typical model turn (~{_money(turn)}) is above the burst cap: the "
                         "budget admits a request whenever the last minute's spend is under the "
                         "cap, whatever its size, so each such turn runs and the next waits up "
                         "to a minute")
        if rate > burst_cap * FAR_ABOVE or wait >= SLOW_MINUTES or turn > burst_cap:
            notes.append(f"expect ~{wait:.0f} min of waiting for the per-minute budget "
                         f"(~{throttled:.0f} min at the cap vs ~{est['span_min']:.0f} min "
                         "unthrottled)")
            if _RANK["unknown"] < _RANK[verdict]:
                verdict = "unknown"
            actions.append(FIX_BURST + ".")
    return verdict, head + "; " + "; ".join(notes), " ".join(dict.fromkeys(actions))


__all__ = ["estimate", "estimate_run", "history", "judge", "price_call", "routes_now"]
