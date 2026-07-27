"""BOSS-280 P2 — automated cost insights: the "here are your top cost saves" analyzer.

The LangSmith-Engine idea, done the BOSS way: **deterministic** rules over the real spend ledger
(``llm_calls``) — no model call, so the cost analyzer itself costs nothing and its findings are
reproducible. Each insight names the problem, quantifies the money, and states the one action
that captures the saving. Surfaced at ``GET /v1/budget/insights`` and sized for the console.

Rules (each fires only when material):
* ``cache_leverage``   — low cache-hit ratio with real fresh-input volume → enable/stabilize
  prompt caching; saving ≈ what reads would have cost at 0.9× discount.
* ``failed_call_burn`` — dollars burned on calls that errored (ok=false), by worst stage.
* ``strong_on_cheap``  — strong-class models spending on stages the default profiles route
  cheap (standards/spec/plan) → route them low_cost.
* ``estimated_pricing``— a material share of spend priced by the DEFAULT fallback → pin real
  prices in BOSS_MODEL_PRICES_JSON (you can't manage what you're guessing).
* ``turn_cap_pressure``— calls that hit the turn cap → decompose or raise the cap knowingly.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .openai_adapter import CACHE_READ_MULT, price_for_model

# thresholds — deliberately conservative so insights stay high-signal
_MIN_SPEND_USD_MICROS = 100_000          # ignore projects with < $0.10 at stake
_CACHE_RATIO_FLOOR = 0.30
_ESTIMATED_SHARE_FLOOR = 0.20
_CHEAP_STAGES = ("standards", "spec", "plan", "review_plan")


def build_insights(connection: Connection, project_id: str) -> dict[str, Any]:
    insights: list[dict[str, Any]] = []

    totals = connection.execute(
        text("SELECT COALESCE(SUM(cost_usd_micros),0) AS spend, "
             "COALESCE(SUM(prompt_tokens),0) AS fresh, "
             "COALESCE(SUM(cache_read_tokens),0) AS cached, "
             "COALESCE(SUM(CASE WHEN price_estimated THEN cost_usd_micros ELSE 0 END),0) AS est "
             "FROM llm_calls WHERE project_id=:p"),
        {"p": project_id}).mappings().one()
    spend = int(totals["spend"])
    if spend < _MIN_SPEND_USD_MICROS:
        return {"project_id": project_id, "total_spend_usd_micros": spend, "insights": []}

    # 1. cache leverage
    fresh, cached = int(totals["fresh"]), int(totals["cached"])
    total_input = fresh + cached
    if total_input:
        ratio = cached / total_input
        if ratio < _CACHE_RATIO_FLOOR:
            # what the fresh input would have saved had it been cache reads
            inp_rate, _, _ = price_for_model("")           # conservative default input rate
            potential = int(fresh * inp_rate * (1 - CACHE_READ_MULT))
            insights.append({
                "kind": "cache_leverage",
                "severity": "high" if ratio < 0.1 else "medium",
                "finding": f"Cache hit ratio is {ratio:.0%} across "
                           f"{total_input:,} input tokens — most context is re-paid at full "
                           f"price on every call.",
                "action": "Enable prompt caching / keep prompt prefixes stable across stages "
                          "(system + repo context first, variable parts last).",
                "potential_saving_usd_micros": potential,
            })

    # 2. failed-call burn
    failed = connection.execute(
        text("SELECT COALESCE(SUM(cost_usd_micros),0) AS burn, count(*) AS calls, "
             "COALESCE(stage,'?') AS stage FROM llm_calls "
             "WHERE project_id=:p AND ok = false GROUP BY stage ORDER BY burn DESC"),
        {"p": project_id}).mappings().all()
    burn = sum(int(r["burn"]) for r in failed)
    if burn > 0 and burn * 20 >= spend:                    # ≥5% of all spend
        worst = failed[0]
        insights.append({
            "kind": "failed_call_burn",
            "severity": "high" if burn * 5 >= spend else "medium",
            "finding": f"${burn/1e6:.2f} ({burn/spend:.0%} of spend) burned on calls that "
                       f"errored; worst stage: {worst['stage']} ({worst['calls']} failures).",
            "action": "Fix the failing stage's environment/timeouts before scaling usage — "
                      "failed calls pay full price and return nothing.",
            "potential_saving_usd_micros": burn,
        })

    # 3. strong models on cheap stages
    strong_cheap = connection.execute(
        text("SELECT COALESCE(SUM(cost_usd_micros),0) AS s FROM llm_calls "
             "WHERE project_id=:p AND stage = ANY(:stages)"),
        {"p": project_id, "stages": list(_CHEAP_STAGES)}).scalar() or 0
    if int(strong_cheap) * 10 >= spend:                    # ≥10% on planning-class stages
        insights.append({
            "kind": "strong_on_cheap",
            "severity": "medium",
            "finding": f"${int(strong_cheap)/1e6:.2f} ({int(strong_cheap)/spend:.0%}) spent on "
                       f"planning-class stages ({', '.join(_CHEAP_STAGES)}).",
            "action": "Route these stages to the low_cost class (the lean/balanced profiles "
                      "already do) — reserve strong models for implement + verification.",
            "potential_saving_usd_micros": int(int(strong_cheap) * 0.8),
        })

    # 4. estimated pricing share
    est = int(totals["est"])
    if spend and est / spend >= _ESTIMATED_SHARE_FLOOR:
        insights.append({
            "kind": "estimated_pricing",
            "severity": "medium",
            "finding": f"{est/spend:.0%} of recorded spend is priced by the DEFAULT fallback "
                       f"(unknown models) — the bill is a guess.",
            "action": "Pin real rates via BOSS_MODEL_PRICES_JSON "
                      "(supports provider-scoped keys like \"azure:gpt-4o\").",
            "potential_saving_usd_micros": 0,
        })

    # 5. turn-cap pressure
    capped = connection.execute(
        text("SELECT count(*) FROM llm_calls WHERE project_id=:p AND ok = false "
             "AND error ILIKE '%max_turns%'"),
        {"p": project_id}).scalar() or 0
    if int(capped) > 0:
        insights.append({
            "kind": "turn_cap_pressure",
            "severity": "medium",
            "finding": f"{int(capped)} call(s) died at the turn cap — each one pays for every "
                       f"turn and delivers nothing.",
            "action": "Decompose the task (smaller slices finish inside the cap) or raise "
                      "BOSS_CODE_AGENT_MAX_TURNS deliberately.",
            "potential_saving_usd_micros": 0,
        })

    insights.sort(key=lambda i: (i["severity"] != "high", -int(i["potential_saving_usd_micros"])))
    return {"project_id": project_id, "total_spend_usd_micros": spend, "insights": insights}
