"""Comprehensive unit and DB tests for M5 Model Routing:
  - BOSSAPP-779: strong lane frontier binding in Fixed mode, shadow mode with counterfactual
    logging & safe fallback, per-stage live budget state, Fixed-vs-Smart A/B metrics.
  - BOSSAPP-780: warm-fix-first & effort-bump lever before cold switch, cache-loss pricing,
    evidence-backed escalation handoff (failing tests + diff + review findings), in-run
    changes_requested / gate_failed signals, per-route escalation recovery metrics.
  - BOSSAPP-781: route + stage-outcome joining (replacing raw ok flag), empirical outcome priors
    fed to router & classifier/Jev, Fast-tier shadow on mechanical stages, projected savings.
  - BOSSAPP-782: CLI token-based list pricing (fixing Sonnet 5 1.8x inflation), gpt-6* frontier
    tier markers, Codex pricing, per-project savings report (metered vs subscription, ≤ 5%
    recomputation drift), one-page HTML & Markdown export.
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from ._router_fixtures import policy, snap
from boss_engine.config import Settings
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.services.code_agent import ClaudeCodeAgent, _current_effort
from boss_engine.services.codex_cli_adapter import CodexCliAdapter
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from boss_engine.services.model_router import store
from boss_engine.services.model_router.classifier import CLASSIFIER, Classifier, TaskSignal
from boss_engine.services.model_router.escalation import (
    EscalationLedger,
    build_escalation_handoff,
    estimate_cache_switch_cost,
    next_effort,
)
from boss_engine.services.model_router.outcomes import (
    OutcomePrior,
    format_priors_for_classifier,
    load_outcome_priors,
    outcome_table_report,
    recommend_from_priors,
    sync_unit_outcomes,
)
from boss_engine.services.model_router.policy import (
    default_strong_model,
    from_dict,
    model_tier,
)
from boss_engine.services.model_router.router import LegacyRoute, RouteRequest, Router
from boss_engine.services.model_router.savings import (
    escalation_recovery_summary,
    render_one_page_report,
    savings_report,
    shadow_summary,
)
from boss_engine.services.model_router.stages import StageRouter
from boss_engine.services.openai_adapter import list_price_cost_usd_micros, price_for_model
from boss_engine.services.pipeline_profiles import resolve_profile
from boss_engine.services.typed_decisions import Decision, DecisionBatch

FULL_STACK = {
    "title": "Build a full-stack task tracker with React and FastAPI",
    "instructions": "Build a new full-stack app for tracking tasks with a React frontend and FastAPI backend.",
}
OAUTH = {
    "title": "Add OAuth login with refresh tokens",
    "instructions": "Add OAuth login with refresh tokens to the existing app.",
}


def build(step: str, spec: dict, **kw) -> RouteRequest:
    return RouteRequest(kind="build_stage", step=step, project_id="p1",
                        text=spec.get("title", ""), spec=dict(spec), **kw)


@pytest.fixture(autouse=True)
def _fresh():
    CLASSIFIER.clear()
    store.invalidate()
    yield
    clear_llm_context()


# ── BOSSAPP-779: Safe Smart router switch-on ─────────────────────────────────────────────────

def test_779_strong_lane_bound_to_frontier_by_default_in_fixed_mode():
    assert default_strong_model("claude_code") == "opus"
    assert default_strong_model("codex") == "gpt-5.2-codex"
    assert default_strong_model("openrouter") == "anthropic/claude-opus-4.1"

    req = RouteRequest(
        kind="build_stage", step="review_code", project_id="p1",
        legacy=lambda: LegacyRoute("claude_code", None, "balanced profile puts review_code on strong",
                                   "strong"),
    )
    decision = Router().route(req, snap(mode="fixed"))
    assert decision.mode == "fixed"
    assert (decision.engine, decision.model, decision.tier) == ("claude_code", "opus", "frontier")
    assert "strong_lane_frontier" in decision.rules

    # An explicit pin is never overridden
    pinned_req = RouteRequest(
        kind="build_stage", step="review_code", project_id="p1",
        legacy=lambda: LegacyRoute("claude_code", "fable", "pinned in Setup", "strong"),
    )
    pinned = Router().route(pinned_req, snap(mode="fixed"))
    assert pinned.model == "fable"
    assert "strong_lane_frontier" not in pinned.rules


def test_779_shadow_mode_acts_fixed_and_logs_smart_counterfactual():
    req = RouteRequest(
        kind="build_stage", step="implement", project_id="p1", unit_id="run_sh1",
        text="Fix a typo in the existing header label.",
        spec={"title": "Fix typo", "instructions": "Fix a typo in the existing header label."},
        legacy=lambda: LegacyRoute("claude_code", "sonnet", "balanced profile low_cost", "low_cost"),
    )
    decision = Router().route(req, snap(mode="shadow"))
    # Acts strictly on Fixed decision
    assert decision.mode == "shadow"
    assert (decision.engine, decision.model, decision.tier) == ("claude_code", "sonnet", "balanced")
    # Counterfactual Smart decision logged without acting
    assert decision.shadow is not None
    assert decision.shadow.tier == "fast"
    assert decision.shadow.model == "haiku"
    assert "shadow" in decision.rules
    assert any("Shadow Smart would pick" in w for w in decision.why)


def test_779_shadow_mode_safe_fallback_when_smart_raises(monkeypatch):
    router = Router()
    monkeypatch.setattr(router, "_smart", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    req = RouteRequest(
        kind="build_stage", step="implement", project_id="p1",
        legacy=lambda: LegacyRoute("claude_code", "sonnet", "fixed", "low_cost"),
    )
    decision = router.route(req, snap(mode="shadow"))
    assert decision.mode == "shadow"
    assert (decision.engine, decision.model) == ("claude_code", "sonnet")
    assert decision.shadow is None
    assert "shadow_fallback" in decision.rules


def test_779_policy_accepts_shadow_mode():
    p = from_dict({"mode": "shadow"})
    assert p.mode == "shadow" and p.shadow and not p.smart


def test_779_budget_state_read_per_stage_not_once_per_run(monkeypatch):
    states = iter(["green", "orange"])

    class _FakeGov:
        def __init__(self, _settings):
            pass

        def status_for_project(self, _conn, _pid):
            return {"budget_state": next(states)}

    monkeypatch.setattr("boss_engine.services.budget.BudgetGovernor", _FakeGov)

    class _FakeConn:
        def begin_nested(self):
            return contextlib.nullcontext()

    fake_conn = _FakeConn()
    settings = SimpleNamespace(
        code_agent_strong_engine="", code_agent_low_cost_engine="",
        code_agent_strong_model="opus", code_agent_low_cost_model="sonnet",
        code_agent_model="", code_review_model="",
    )
    stages = StageRouter(
        transaction=lambda: contextlib.nullcontext(fake_conn),
        settings=settings, project_id="p1", run_id="r_b1",
        spec={"routing_mode": "fixed"}, title="Build app",
        profile=resolve_profile("strict"), build_engine="claude_code",
        review_engine="claude_code", budget_state="green",
    )
    stages._pins = {}

    captured = []
    monkeypatch.setattr(store, "route", lambda c, s, req, ledger=None: (
        captured.append(stages.budget_state),
        Router().route(req, snap(mode="fixed")),
    )[1])

    d1 = stages.route("implement")
    d2 = stages.route("implement")
    assert captured == ["green", "orange"]
    assert d1.model == "opus" and not d1.budget_lite
    assert d2.model == "sonnet" and d2.budget_lite


# ── BOSSAPP-780: Escalate with evidence, not cold ────────────────────────────────────────────

def test_780_cache_switch_pricing_prefers_warm_fix_first():
    before = estimate_cache_switch_cost(
        warm_context_tokens=120_000, current_model="claude-sonnet-5",
        target_model="claude-opus-4-6", warm_fix_attempted=False,
    )
    assert not before.worth_switching
    assert before.cache_loss_usd_micros > 0
    assert before.effort_bump_usd_micros < before.switch_cold_usd_micros

    after = estimate_cache_switch_cost(
        warm_context_tokens=120_000, current_model="claude-sonnet-5",
        target_model="claude-opus-4-6", warm_fix_attempted=True,
    )
    assert after.worth_switching


def test_780_effort_bump_before_cold_tier_switch():
    assert next_effort("low") == "medium"
    assert next_effort("medium") == "high"
    assert next_effort("high") is None

    ledger = EscalationLedger()
    # First failure -> bump effort on SAME model, preserve cache
    ledger.record("implement", "tests_failed", 1)
    req1 = RouteRequest(
        kind="build_stage", step="implement", project_id="p1",
        spec=dict(FULL_STACK), text=FULL_STACK["title"],
        warm_context_tokens=80_000, warm_fix_attempted=False, current_effort="low",
    )
    d1 = Router().route(req1, snap(), ledger=ledger)
    assert d1.tier == "balanced" and d1.model == "sonnet"
    assert d1.effort == "medium"
    assert d1.escalation_strategy == "effort_bump"
    assert d1.cache_loss_usd_micros == 0

    # Second failure STILL without warm fix attempted -> warm fix + effort bump before switch
    ledger.record("implement", "tests_failed", 1)
    req2 = RouteRequest(
        kind="build_stage", step="implement", project_id="p1",
        spec=dict(FULL_STACK), text=FULL_STACK["title"],
        warm_context_tokens=80_000, warm_fix_attempted=False, current_effort="medium",
    )
    d2 = Router().route(req2, snap(), ledger=ledger)
    assert d2.tier == "balanced" and d2.effort == "high"
    assert d2.escalation_strategy == "effort_bump"

    # Once warm fix has failed -> escalate to Frontier and price the cache switch loss
    req3 = RouteRequest(
        kind="build_stage", step="implement", project_id="p1",
        spec=dict(FULL_STACK), text=FULL_STACK["title"],
        warm_context_tokens=80_000, warm_fix_attempted=True, current_effort="high",
    )
    d3 = Router().route(req3, snap(), ledger=ledger)
    assert d3.tier == "frontier" and d3.model == "opus"
    assert d3.escalation_strategy == "tier_escalation"
    assert d3.cache_loss_usd_micros and d3.cache_loss_usd_micros > 0


def test_780_effort_override_honored_by_code_agent():
    set_llm_context(stage="implement", effort_writing="low")
    assert _current_effort() == "low"
    set_llm_context(effort_override="high")
    assert _current_effort() == "high"
    set_llm_context(effort_override=None)
    assert _current_effort() == "low"


def test_780_build_escalation_handoff_includes_tests_diff_and_review():
    handoff = build_escalation_handoff(
        failing_tests="FAILED tests/test_math.py::test_add - AssertionError: 3 != 4",
        diff="--- a/math.py\n+++ b/math.py\n-return a + b\n+return a - b",
        review_findings="FINDING [high] math.py:2 — subtraction instead of addition",
        prior_attempts=2, prior_model="claude-sonnet-5", signal="tests_failed",
    )
    assert "ESCALATION EVIDENCE HANDOFF" in handoff
    assert "FAILING TESTS" in handoff and "AssertionError: 3 != 4" in handoff
    assert "CURRENT IMPLEMENTATION DIFF" in handoff and "+return a - b" in handoff
    assert "REVIEW & GATE FINDINGS" in handoff and "subtraction instead of addition" in handoff


# ── BOSSAPP-781: Learn from outcomes ─────────────────────────────────────────────────────────

def test_781_outcome_priors_route_down_or_up_and_respect_floors_and_sensitivity():
    priors = {
        ("fix", "check_tests", "fast"): OutcomePrior(
            task_type="fix", stage="check_tests", tier="fast",
            samples=12, passed=11, tests_passed_count=11, tests_total_count=12,
            review_approved_count=10, review_total_count=12, merged_count=10, merged_total_count=12,
            avg_list_cost_usd=0.04, avg_tokens=20000,
        ),
        ("fix", "check_tests", "balanced"): OutcomePrior(
            task_type="fix", stage="check_tests", tier="balanced",
            samples=10, passed=9, tests_passed_count=9, tests_total_count=10,
            review_approved_count=9, review_total_count=10, merged_count=9, merged_total_count=10,
            avg_list_cost_usd=0.14, avg_tokens=20000,
        ),
        ("new_app", "implement", "balanced"): OutcomePrior(
            task_type="new_app", stage="implement", tier="balanced",
            samples=10, passed=4, tests_passed_count=4, tests_total_count=10,
            review_approved_count=4, review_total_count=10, merged_count=3, merged_total_count=10,
            avg_list_cost_usd=0.30, avg_tokens=50000,
        ),
    }

    # Route down when cheap tier holds pass rate (and no floor blocks it)
    down_rec = recommend_from_priors(priors, step="check_tests", task_type="fix",
                                     current_tier="balanced", sensitive=False, floor=None)
    assert down_rec is not None and down_rec.direction == "down" and down_rec.to_tier == "fast"

    # Floor blocks routing down
    blocked_floor = recommend_from_priors(priors, step="check_tests", task_type="fix",
                                          current_tier="balanced", sensitive=False, floor="balanced")
    assert blocked_floor is None

    # Sensitive work NEVER routes down
    blocked_sens = recommend_from_priors(priors, step="check_tests", task_type="fix",
                                         current_tier="balanced", sensitive=True, floor=None)
    assert blocked_sens is None

    # Route up when current tier underperforms (< 65%)
    up_rec = recommend_from_priors(priors, step="implement", task_type="new_app",
                                   current_tier="balanced", sensitive=False, floor=None)
    assert up_rec is not None and up_rec.direction == "up" and up_rec.to_tier == "frontier"

    # Router._smart applies the prior and formats prior hint into classifier context
    s = snap()
    s.outcome_priors = priors
    d_up = Router().route(
        RouteRequest(kind="build_stage", step="implement", project_id="p1",
                     task_kind="new_app", spec=dict(FULL_STACK), text=FULL_STACK["title"]),
        s,
    )
    assert d_up.tier == "frontier" and "outcome_prior:up" in d_up.rules

    # Jev receives empirical pass rates in its classifier context
    seen_ctx = []

    class _CapDecider:
        def decide(self, context, questions):
            seen_ctx.append(context)
            return DecisionBatch(
                decisions={"route_tier": Decision("balanced", 0.9, "ok")},
                backend="jev", model="jev", latency_ms=1,
            )

    hint = format_priors_for_classifier(priors, step="implement", task_type="new_app")
    assert "40% pass (4/10" in hint
    Classifier().classify(
        TaskSignal(kind="build_stage", step="implement", text="Build app",
                   task_kind="new_app", prior_hint=hint),
        policy().classifier, jev=_CapDecider(), jev_state="ready",
    )
    assert seen_ctx and "Historical stage outcomes" in seen_ctx[0]


def test_781_fast_stage_shadow_attached_on_mechanical_stages():
    # `plan` has a Balanced floor, so Smart acts Balanced and attaches a Fast shadow candidate
    d = Router().route(build("plan", FULL_STACK), snap())
    assert d.tier == "balanced"
    assert d.shadow is not None
    assert d.shadow.tier == "fast"
    assert d.shadow.model == "haiku"
    assert "fast_stage_shadow" in d.shadow.rules

    # Sensitive work never shadow-evaluates Fast
    d_sens = Router().route(build("plan", OAUTH), snap())
    assert d_sens.tier == "frontier"
    assert d_sens.shadow is None


# ── BOSSAPP-782: Cost truth & savings report ─────────────────────────────────────────────────

def test_782_cli_calls_priced_at_list_from_tokens_and_gpt6_frontier_markers():
    # gpt-6* markers classified as Frontier; mini/fast stay Fast
    assert model_tier("codex", "gpt-6") == "frontier"
    assert model_tier("codex", "gpt-6-sol") == "frontier"
    assert model_tier("codex", "gpt-6-codex") == "frontier"
    assert model_tier("codex", "gpt-6-mini") == "fast"
    assert model_tier("claude_code", "claude-fable-5-1") == "frontier"

    # Codex has a real known price in the price table
    inp, out, known = price_for_model("codex-cli")
    assert known and inp > 0 and out > 0
    inp6, out6, known6 = price_for_model("gpt-6-sol")
    assert known6 and (inp6, out6) == (3.00, 15.00)

    # Claude CLI reporting 1.8x list price for Sonnet 5 is corrected to exact list price from tokens
    # 100k input @ $2/Mtok ($0.20) + 10k output @ $10/Mtok ($0.10) = $0.30 = 300_000 micros
    # CLI claims $0.54 (1.8x inflation)
    data = {
        "usage": {"input_tokens": 100_000, "output_tokens": 10_000,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-sonnet-5": {}},
        "total_cost_usd": 0.54,
    }
    chat_usage = ClaudeCliAdapter(Settings())._usage(data)
    assert chat_usage["cost_usd_micros"] == 300_000
    assert chat_usage["cli_reported_cost_usd_micros"] == 540_000

    code_usage, code_cost = ClaudeCodeAgent._usage(data, Settings())
    assert code_cost == 300_000
    assert code_usage["cli_reported_cost_usd_micros"] == 540_000

    # Codex CLI chat adapter now prices its tokens at list price
    turn = (
        '{"type": "turn.completed", "usage": {"input_tokens": 20000, '
        '"cached_input_tokens": 10000, "output_tokens": 2000, "reasoning_output_tokens": 500}}\n'
    )
    codex_usage = CodexCliAdapter(Settings())._usage({"stdout": turn})
    expected_micros, known_c = list_price_cost_usd_micros(codex_usage)
    assert known_c and expected_micros > 0
    assert codex_usage["cost_usd_micros"] == expected_micros


# ── DB Integration across 779, 780, 781, 782 ────────────────────────────────────────────────

def test_m5_db_shadow_outcomes_escalation_recovery_and_savings_report():
    db_url = os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("BOSS_TEST_DATABASE_URL not set")
    db_engine = create_engine(db_url)
    pid = "proj_m5_routing_test"
    now = datetime.now(timezone.utc)
    with db_engine.begin() as c:
        # Clean up prior runs if any
        for tbl in ("model_route_outcomes", "llm_calls", "model_routes", "run_scorecards"):
            c.execute(text(f"DELETE FROM {tbl} WHERE project_id = :p"), {"p": pid})

        # 1. Insert shadow route + smart routes + escalated route
        c.execute(text("""
            INSERT INTO model_routes (
                id, project_id, unit_kind, unit_id, step, mode, tier, engine, model,
                shadow_tier, shadow_engine, shadow_model, shadow_reason,
                escalation, escalation_signal, escalation_strategy, created_at
            ) VALUES
            ('rt_m5_1', :p, 'build_stage', 'crun_m5_1', 'implement', 'smart', 'balanced',
             'claude_code', 'claude-sonnet-5', NULL, NULL, NULL, NULL, NULL, NULL, NULL, :now),
            ('rt_m5_2', :p, 'build_stage', 'crun_m5_1', 'review_code', 'smart', 'frontier',
             'codex', 'gpt-6-sol', NULL, NULL, NULL, NULL, NULL, NULL, NULL, :now),
            ('rt_m5_3', :p, 'build_stage', 'crun_m5_2', 'standards', 'shadow', 'balanced',
             'claude_code', 'claude-sonnet-5', 'fast', 'claude_code', 'claude-haiku-4-5',
             'Shadow Smart would pick Fast', NULL, NULL, NULL, :now),
            ('rt_m5_4', :p, 'build_stage', 'crun_m5_2', 'implement', 'smart', 'frontier',
             'claude_code', 'claude-opus-4-6', NULL, NULL, NULL, NULL,
             'tests failed ×2 → Frontier', 'tests_failed', 'tier_escalation', :now)
        """), {"p": pid, "now": now})

        # 2. Insert llm_calls at exact list price (one subscription, two metered, one codex review)
        def _ins_call(cid, rid, eng, mdl, pt, ct, cr, cw, inc):
            micros, _ = list_price_cost_usd_micros({
                "model": mdl, "prompt_tokens": pt, "completion_tokens": ct,
                "cache_read_tokens": cr, "cache_write_tokens": cw,
            })
            c.execute(text("""
                INSERT INTO llm_calls (
                    id, project_id, code_task_run_id, stage, engine, model, route_id,
                    prompt_tokens, completion_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd_micros,
                    included_in_subscription, ok, created_at
                ) VALUES (
                    :id, :p, :u, :st, :e, :m, :rid,
                    :pt, :ct, :tt, :cr, :cw, :cost, :inc, true, :now
                )
            """), {
                "id": cid, "p": pid, "u": "crun_m5_1", "st": "implement",
                "e": eng, "m": mdl, "rid": rid,
                "pt": pt, "ct": ct, "tt": pt + ct + cr + cw,
                "cr": cr, "cw": cw, "cost": micros, "inc": inc, "now": now,
            })

        _ins_call("lc_m5_1", "rt_m5_1", "claude_code", "claude-sonnet-5",
                  500_000, 100_000, 200_000, 0, True)   # subscription, balanced
        _ins_call("lc_m5_2", "rt_m5_2", "codex", "gpt-6-sol",
                  100_000, 20_000, 0, 0, True)          # subscription, frontier (gpt-6)
        _ins_call("lc_m5_3", "rt_m5_3", "openrouter", "deepseek/deepseek-chat",
                  400_000, 80_000, 0, 0, False)         # metered, fast/balanced
        _ins_call("lc_m5_4", "rt_m5_4", "claude_code", "claude-opus-4-6",
                  100_000, 30_000, 0, 0, False)         # metered, frontier escalation

        # 3. Scorecards: 1 merged PR with approved review on Frontier
        c.execute(text("""
            INSERT INTO run_scorecards (
                unit_kind, unit_id, project_id, task_kind, terminal_state,
                tests_passed, tests_total, review_approved, pr_merged,
                route_mode, route_tier, ended_at, computed_at
            ) VALUES
            ('build', 'crun_m5_1', :p, 'fix', 'completed', 5, 5, true, true, 'smart', 'frontier', :now, :now),
            ('build', 'crun_m5_2', :p, 'new_app', 'completed', 4, 4, true, false, 'smart', 'frontier', :now, :now)
        """), {"p": pid, "now": now})

        # 4. Sync outcomes & verify priors + recovery + shadow + savings report
        synced1 = sync_unit_outcomes(c, project_id=pid, unit_id="crun_m5_1")
        synced2 = sync_unit_outcomes(c, project_id=pid, unit_id="crun_m5_2")
        assert synced1 == 2 and synced2 == 2

        priors = load_outcome_priors(c, pid, days=30)
        assert ("fix", "implement", "balanced") in priors
        assert priors[("fix", "implement", "balanced")].pass_rate == 1.0

        # Shadow summary
        sh = shadow_summary(c, pid, days=14, now=now)
        assert sh["routes"] == 1
        assert sh["shadow_tiers"]["fast"] == 1
        assert sh["actual_tiers"]["balanced"] == 1
        assert sh["projected_saving_usd"] >= 0.0

        # Escalation recovery summary
        esc = escalation_recovery_summary(c, pid, days=30, now=now)
        assert esc["total"] == 1 and esc["rescued"] == 1
        assert esc["recovery_rate"] == 1.0
        assert esc["cost_per_rescued_build_usd"] > 0

        # Full savings report (BOSSAPP-782)
        rep = savings_report(c, pid, days=30, now=now)
        assert rep["total_calls"] == 4
        assert rep["routed_down_calls"] == 2
        assert rep["pct_calls_routed_down"] == 0.5
        assert rep["merged_changes"] == 1
        assert rep["cost_per_merged_change_usd"] is not None
        assert rep["metered"]["dollars_saved_usd"] > 0
        assert rep["subscription"]["capacity_saved_usd"] > 0
        assert rep["recomputation_check"]["within_5pct"] is True
        assert rep["recomputation_check"]["drift_pct"] <= 0.05
        # Review verdict on gpt-6-sol attributed to Frontier tier, not Balanced!
        rev_tiers = {r["tier"]: r for r in rep["review_verdicts_by_tier"]}
        assert rev_tiers["frontier"]["approved"] >= 1

        # One-page HTML & Markdown export
        html_doc = render_one_page_report(rep, project_name="Acme Core", format="html")
        assert "<!doctype html>" in html_doc.lower()
        assert "Acme Core" in html_doc
        assert "<script" not in html_doc.lower()  # self-contained, zero scripts
        md_doc = render_one_page_report(rep, project_name="Acme Core", format="markdown")
        assert "# BOSS Model Routing Savings Report — Acme Core" in md_doc
        assert "Metered dollars saved" in md_doc

        # Outcome table report
        ot = outcome_table_report(c, pid, days=30)
        assert len(ot["rows"]) >= 2

        # Cleanup
        for tbl in ("model_route_outcomes", "llm_calls", "model_routes", "run_scorecards"):
            c.execute(text(f"DELETE FROM {tbl} WHERE project_id = :p"), {"p": pid})
