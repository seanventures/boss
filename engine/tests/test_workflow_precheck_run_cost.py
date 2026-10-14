"""`budget.run_cost` (services.run_cost_estimate): does the WHOLE run fit the caps?

2026-10-14: the laptop moved from a Claude subscription to a metered key with a $10/day cap and a
$0.10/minute burst cap. Every step's reservation still fit, so `budget.headroom` said ready, and
a HelpDesk run would have been throttled for hours and then refused. These tests drive the
estimate (past runs' recorded tokens, priced at today's routes) and its verdicts with fixtures —
no database, no model call."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from boss_engine.services import run_cost_estimate as rc
from boss_engine.services import workflow_precheck as wp

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
SONNET = "claude-sonnet-4-5"           # $3 / $15 per Mtok


def _call(stage: str, minute: int, *, prompt: int, completion: int, latency_min: float,
          model: str = SONNET, engine: str = "claude_code", included: bool = False,
          turns: int = 10) -> dict[str, Any]:
    return {"stage": stage, "engine": engine, "model": model, "prompt_tokens": prompt,
            "completion_tokens": completion, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd_micros": 0 if included else 1, "latency_ms": int(latency_min * 60_000),
            "turns": turns, "created_at": T0 + timedelta(minutes=minute),
            "included_in_subscription": included}


def _history(**kw: Any) -> dict[str, Any]:
    # implement: 2M in / 200k out = $6 + $3 = $9; review: 1M / 100k = $3 + $1.50 = $4.50.
    calls = [_call("implement", 30, prompt=2_000_000, completion=200_000, latency_min=25, **kw),
             _call("review_code", 45, prompt=1_000_000, completion=100_000, latency_min=12,
                   **kw)]
    return {"basis": "workflow", "runs": [{"id": "wfr_1", "status": "completed",
                                           "calls": calls}]}


def _route(kind: str = "env", model: str | None = SONNET) -> dict[str, Any]:
    return {"engine": "claude_code", "model": model, "source": "setup-anthropic", "kind": kind}


def _budget(day_cap: int, *, actual: int = 0, month_cap: int = 200_000_000) -> dict:
    return {"periods": {
        "day": {"cap_usd_micros": day_cap, "actual_usd_micros": actual,
                "reserved_usd_micros": 0, "resets_at_label": "resets at midnight"},
        "month": {"cap_usd_micros": month_cap, "actual_usd_micros": actual,
                  "reserved_usd_micros": 0}}}


class _Settings:
    plan_budget_usd_micros = 1_500_000
    code_task_budget_usd_micros = 2_000_000
    code_agent_max_concurrency = 1


def _ctx(memo: dict[str, Any], spec: dict | None = None) -> wp.Context:
    spec = spec or {"entry_step": "build", "steps": [
        {"id": "build", "kind": "capability", "next": ["review"],
         "binding": {"capability": "build", "inputs": {}}},
        {"id": "review", "kind": "capability", "next": [],
         "binding": {"capability": "review", "inputs": {}}}]}
    ctx = wp.Context(connection=None, settings=_Settings(), project_id="p1", actor="ceo",
                     spec=spec, run_input={}, checkpoint=wp.PRE_RUN, workflow_key="d1",
                     offline=True)
    ctx.memo.update(memo)
    return ctx


def _memo(history: dict, route: dict, budget: dict, burst: int = 100_000,
          lanes: int = 3) -> dict[str, Any]:
    return {"run_cost_history": history,
            "run_cost_routes": {"implement": route, "review_code": route},
            "budget": budget, "budget_pace": {"burst": burst, "lanes": lanes}}


def _check(memo: dict[str, Any]) -> dict[str, Any]:
    ctx = _ctx(memo)
    return wp.check_run_cost(ctx, ctx.spec["steps"][0])


# ── verdicts ──────────────────────────────────────────────────────────────────────────────────

def test_a_run_over_the_daily_cap_will_fail_and_names_the_fix():
    found = _check(_memo(_history(), _route(), _budget(10_000_000)))
    assert found["verdict"] == "will_fail"
    assert "the run needs ~$13.50" in found["evidence"]
    assert "$10.00 left today under a $10.00 cap" in found["evidence"]
    assert "Raise the daily cap in Govern → Pace (person)" in found["fix"]["action"]
    assert found["details"]["money"] == 13_500_000


def test_money_already_spent_today_counts():
    found = _check(_memo(_history(), _route(), _budget(20_000_000, actual=8_000_000)))
    assert found["verdict"] == "will_fail" and "$12.00 left today" in found["evidence"]


def test_a_spend_rate_far_above_the_burst_cap_is_a_slow_risk():
    found = _check(_memo(_history(), _route(), _budget(500_000_000, month_cap=0)))
    assert found["verdict"] == "unknown"
    # $13.50 at $0.10/min is 135 min of spend allowed; unthrottled it spans 40 min (10:05-10:45).
    assert "expect ~95 min of waiting for the per-minute budget" in found["evidence"]
    assert "against a $0.10/min burst cap" in found["evidence"]
    assert "Raise the burst cap in Govern → Pace (person)" in found["fix"]["action"]


def test_a_burst_cap_the_run_fits_under_is_ready():
    found = _check(_memo(_history(), _route(), _budget(500_000_000), burst=10_000_000))
    assert found["verdict"] == "ready", found["evidence"]


def test_a_turn_larger_than_the_burst_cap_is_admitted_but_slows_the_run():
    # budget._burst_gate admits a request whenever the trailing minute is under the cap,
    # whatever the request's size — so it is never refused for good, only slowed.
    found = _check(_memo(_history(turns=1), _route(), _budget(500_000_000), burst=1_000_000))
    assert found["verdict"] == "unknown"
    assert "a typical model turn" in found["evidence"] and "above the burst cap" in \
        found["evidence"]


def test_a_subscription_route_is_ready_at_zero():
    found = _check(_memo(_history(), _route(kind="subscription"), _budget(10_000_000)))
    assert found["verdict"] == "ready"
    assert "$0 marginal" in found["evidence"] and "~$13.50 at API list price" in \
        found["evidence"]


def test_no_history_is_unknown_and_says_what_it_assumed():
    found = _check(_memo({"basis": "none", "runs": []}, _route(), _budget(10_000_000)))
    assert found["verdict"] == "unknown"
    assert "not estimated" in found["evidence"] and "assuming it fits" in found["evidence"]


def test_unreadable_history_is_unknown():
    assert _check({})["verdict"] == "unknown"


# ── priced at TODAY's route, from tokens recorded on a subscription ───────────────────────────

def test_subscription_history_is_priced_at_the_metered_route_now():
    # Recorded on a subscription (no money), Opus with a 1M context; today the route is the
    # metered key on the Fast tier's alias.
    hist = _history(model="claude-opus-4-8[1m]", included=True)
    found = _check(_memo(hist, _route(model="haiku"), _budget(500_000_000), burst=10_000_000))
    # haiku-4-5 at $1 / $5: 3M in = $3, 300k out = $1.50.
    assert found["details"]["money"] == 4_500_000 and found["verdict"] == "ready"


def test_a_route_with_no_model_is_priced_at_what_it_ran_last_time():
    money, value, model = rc.price_call(_call("implement", 1, prompt=1_000_000, completion=0,
                                              latency_min=1), _route(model=None))
    assert (money, value, model) == (3_000_000, 3_000_000, SONNET)


# ── wiring ────────────────────────────────────────────────────────────────────────────────────

def test_it_shows_once_per_run_and_only_before_the_run():
    ctx = _ctx(_memo(_history(), _route(), _budget(10_000_000)))
    report = wp.run_checks(ctx, only_fast=True)
    found = [(s["step_id"], c) for s in report["steps"] for c in s["checks"]
             if c["id"] == "budget.run_cost"]
    assert len(found) == 1
    step, check = found[0]
    assert step == "build" and check["also_needed_by"] == ["review"]
    assert "build:budget.run_cost" in report["summary"]["blocking"]
    assert wp.CHECKS["budget.run_cost"].checkpoints == (wp.PRE_RUN,)
    assert wp.CHECKS["budget.run_cost"].fast and wp.CHECK_DOCS["budget.run_cost"]


def test_start_anyway_on_the_budget_accepts_the_run_cost_too():
    assert "budget.run_cost" in wp.READINESS_ACCEPTS["budget"]


@pytest.mark.parametrize("calls,expected", [
    # Two calls of 10 min that overlap fully: 20 active minutes in a 10-minute span, 2 at once.
    ([(10, 10), (10, 10)], (20.0, 10.0, 2)),
    ([(10, 10), (30, 10)], (20.0, 30.0, 1)),
])
def test_timing_reads_active_time_span_and_parallelism(calls, expected):
    rows = [_call("implement", end, prompt=1, completion=1, latency_min=lat)
            for end, lat in calls]
    active, span, peak = rc._timing(rows)  # noqa: SLF001
    assert (round(active, 3), round(span, 3), peak) == expected
