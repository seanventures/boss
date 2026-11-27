"""API + Postgres integration tests for M5 Model Routing (BOSSAPP-779, 780, 781, 782):
  - Shadow mode via PUT /v1/setup/routing + GET /v1/setup shadow & outcomes payloads
  - GET /v1/setup/routing/outcomes (per-stage, per-task-type outcome table)
  - GET /v1/govern/routing-savings & GET /v1/govern/spend routing_savings
  - GET /v1/govern/routing-savings/export (one-page HTML & Markdown reports)
  - Fixed-vs-Smart A/B experiment arm labels, merged count, cost per merged change, review rate
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, fakes
from boss_engine.services.openai_adapter import list_price_cost_usd_micros


@pytest.fixture(autouse=True)
def _clean_m5_api_state(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p "
                           "AND key = 'model_routing_mode'"), {"p": PROJECT})
            c.execute(text("DELETE FROM model_route_outcomes WHERE project_id = :p"), {"p": PROJECT})
            c.execute(text("DELETE FROM llm_calls WHERE project_id = :p AND id LIKE 'lc_api_m5_%'"),
                      {"p": PROJECT})
            c.execute(text("DELETE FROM model_routes WHERE project_id = :p AND id LIKE 'rt_api_m5_%'"),
                      {"p": PROJECT})
            c.execute(text("DELETE FROM run_scorecards WHERE project_id = :p AND unit_id LIKE 'crun_api_m5_%'"),
                      {"p": PROJECT})
    wipe()
    yield
    wipe()


def test_shadow_mode_and_outcomes_endpoint(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)

    saved = client.put("/v1/setup/routing", headers=AUTH, json={
        "project_id": PROJECT, "policy": {"mode": "shadow"}, "version": 0,
    })
    assert saved.status_code == 200, saved.text
    routing = saved.json()["routing"]
    assert routing["mode"] == "shadow"
    assert "shadow" in routing and "outcomes" in routing

    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO model_routes (
                id, project_id, unit_kind, unit_id, step, mode, tier, engine, model,
                shadow_tier, shadow_engine, shadow_model, shadow_reason, created_at
            ) VALUES (
                'rt_api_m5_sh1', :p, 'build_stage', 'crun_api_m5_1', 'standards',
                'shadow', 'balanced', 'claude_code', 'claude-sonnet-5',
                'fast', 'claude_code', 'claude-haiku-4-5', 'Shadow Fast', :now
            )
        """), {"p": PROJECT, "now": now})
        c.execute(text("""
            INSERT INTO model_route_outcomes (
                route_id, project_id, unit_id, task_type, stage, tier, engine, model,
                mode, tests_passed, stage_passed, list_cost_usd_micros, recorded_at
            ) VALUES (
                'rt_api_m5_sh1', :p, 'crun_api_m5_1', 'fix', 'standards', 'fast',
                'claude_code', 'claude-haiku-4-5', 'smart', true, true, 50000, :now
            )
        """), {"p": PROJECT, "now": now})

    got = client.get("/v1/setup/routing/outcomes", headers=AUTH,
                     params={"project_id": PROJECT, "days": 30})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["shadow"]["routes"] == 1
    assert body["shadow"]["shadow_tiers"]["fast"] == 1
    assert len(body["outcomes"]["rows"]) == 1
    assert body["outcomes"]["rows"][0]["stage"] == "standards"


def test_routing_savings_api_spend_integration_and_one_page_export(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO model_routes (
                id, project_id, unit_kind, unit_id, step, mode, tier, engine, model, created_at
            ) VALUES (
                'rt_api_m5_s1', :p, 'build_stage', 'crun_api_m5_s1', 'implement',
                'smart', 'balanced', 'claude_code', 'claude-sonnet-5', :now
            )
        """), {"p": PROJECT, "now": now})
        micros, _ = list_price_cost_usd_micros({
            "model": "claude-sonnet-5", "prompt_tokens": 250_000, "completion_tokens": 50_000,
        })
        c.execute(text("""
            INSERT INTO llm_calls (
                id, project_id, code_task_run_id, stage, engine, model, route_id,
                prompt_tokens, completion_tokens, total_tokens, cost_usd_micros,
                included_in_subscription, ok, created_at
            ) VALUES (
                'lc_api_m5_s1', :p, 'crun_api_m5_s1', 'implement', 'claude_code',
                'claude-sonnet-5', 'rt_api_m5_s1', 250000, 50000, 300000, :cost,
                false, true, :now
            )
        """), {"p": PROJECT, "cost": micros, "now": now})
        c.execute(text("""
            INSERT INTO run_scorecards (
                unit_kind, unit_id, project_id, task_kind, terminal_state,
                review_approved, pr_merged, ended_at, computed_at
            ) VALUES (
                'build', 'crun_api_m5_s1', :p, 'fix', 'completed', true, true, :now, :now
            )
        """), {"p": PROJECT, "now": now})

    rep = client.get("/v1/govern/routing-savings", headers=AUTH,
                     params={"project_id": PROJECT, "days": 30})
    assert rep.status_code == 200, rep.text
    data = rep.json()
    assert data["total_calls"] >= 1
    assert data["metered"]["dollars_saved_usd"] > 0
    assert data["recomputation_check"]["within_5pct"] is True
    assert data["merged_changes"] >= 1

    # Also embedded on Govern › Spend
    spend = client.get("/v1/govern/spend", headers=AUTH,
                       params={"project_id": PROJECT, "window": "30d"})
    assert spend.status_code == 200, spend.text
    assert spend.json()["routing_savings"]["total_calls"] >= 1

    # One-page HTML export
    exp_html = client.get("/v1/govern/routing-savings/export", headers=AUTH,
                          params={"project_id": PROJECT, "days": 30, "format": "html"})
    assert exp_html.status_code == 200
    assert "text/html" in exp_html.headers["content-type"]
    assert "attachment;" in exp_html.headers["content-disposition"]
    assert "BOSS Model Routing Savings" in exp_html.text

    # One-page Markdown export
    exp_md = client.get("/v1/govern/routing-savings/export", headers=AUTH,
                        params={"project_id": PROJECT, "days": 30, "format": "markdown"})
    assert exp_md.status_code == 200
    assert "text/markdown" in exp_md.headers["content-type"]
    assert "# BOSS Model Routing Savings Report" in exp_md.text


def test_ab_experiment_reports_cost_per_merged_change_and_review_verdicts(engine):
    from boss_engine.services import experiments

    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        exp = experiments.create(
            c, project_id=PROJECT, actor="ceo", name="",
            target_kind="role", target_key="software_engineer",
            knob="routing", arm_a="fixed", arm_b="smart", target_units=10,
        )
        assert exp["arm_a_label"] == "Fixed"
        assert exp["arm_b_label"] == "Smart"
        eid = exp["id"]
        c.execute(text("""
            INSERT INTO experiment_assignments (experiment_id, unit_kind, unit_id, arm) VALUES
            (:e, 'build', 'crun_api_m5_a1', 'a'),
            (:e, 'build', 'crun_api_m5_b1', 'b')
        """), {"e": eid})
        c.execute(text("""
            INSERT INTO run_scorecards (
                unit_kind, unit_id, project_id, terminal_state, score, cost_usd,
                review_approved, pr_merged, ended_at, computed_at
            ) VALUES
            ('build', 'crun_api_m5_a1', :p, 'completed', 0.80, 4.00, true, true, :now, :now),
            ('build', 'crun_api_m5_b1', :p, 'completed', 0.88, 1.50, true, true, :now, :now)
        """), {"p": PROJECT, "now": now})
        view = experiments.get(c, PROJECT, eid)
        assert view["arms"]["a"]["merged_count"] == 1
        assert view["arms"]["a"]["cost_per_merged_usd"] == 4.0
        assert view["arms"]["a"]["review_approval_rate"] == 1.0
        assert view["arms"]["b"]["cost_per_merged_usd"] == 1.5
        c.execute(text("DELETE FROM experiment_assignments WHERE experiment_id = :e"), {"e": eid})
        c.execute(text("DELETE FROM experiments WHERE id = :e"), {"e": eid})
