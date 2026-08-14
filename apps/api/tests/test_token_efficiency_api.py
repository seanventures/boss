"""Token efficiency API — scorecard, policies, estimate, recommendations, consumption.

Read-only over llm_calls for analytics; policies write only to token_budget_policies.
No external model spend.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.core import make_id
from boss_engine.services.token_efficiency import (
    PROJECT_DEFAULT_STAGE,
    estimate_messages,
    estimate_tokens,
    suggest_model_routing,
)

PID = "boss-dogfood"


def _insert_call(
    c,
    *,
    stage: str = "implement",
    cost: int = 100_000,
    ok: bool = True,
    error: str | None = None,
    fresh: int = 1000,
    cached: int = 0,
    completion: int = 100,
    estimated: bool = False,
    model: str = "test-model",
):
    total = fresh + cached + completion
    c.execute(
        text(
            """
            INSERT INTO llm_calls (
                id, project_id, code_task_run_id, stage, engine, model, role,
                prompt_tokens, completion_tokens, total_tokens,
                cache_read_tokens, cache_write_tokens, cost_usd_micros,
                price_estimated, ok, error)
            VALUES (
                :id, :p, :r, :st, 'e', :m, 'software_engineer',
                :fr, :ct, :tt, :cr, 0, :c, :pe, :ok, :err)
            """
        ),
        {
            "id": make_id("llm"),
            "p": PID,
            "r": make_id("crun"),
            "st": stage,
            "m": model,
            "fr": fresh,
            "ct": completion,
            "tt": total,
            "cr": cached,
            "c": cost,
            "pe": estimated,
            "ok": ok,
            "err": error,
        },
    )


# ── Pure helpers (no DB) ──────────────────────────────────────────────────────


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("abcd") == 1  # 4 chars → 1 token
    assert estimate_tokens("a" * 8) == 2
    # ceil: 5 chars → 2 tokens
    assert estimate_tokens("abcde") == 2


def test_estimate_messages_and_compress_suggestions():
    long_body = "x" * 7000
    out = estimate_messages(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "system", "content": "Also be brief."},
            {"role": "user", "content": long_body},
            {"role": "assistant", "content": ""},
        ],
        max_context_tokens=100,
    )
    assert out["heuristic"] == "chars/4"
    assert out["message_count"] == 4
    assert out["total_estimated_tokens"] > 0
    assert out["over_budget"] is True
    kinds = {s["kind"] for s in out["compress_suggestions"]}
    assert "merge_system" in kinds
    assert "truncate_long_message" in kinds
    assert "drop_empty" in kinds


def test_suggest_model_routing_from_profiles():
    lean = suggest_model_routing(profile_name="lean")
    assert lean["profile"] == "lean"
    by_stage = {s["stage"]: s["preferred_class"] for s in lean["stages"]}
    assert by_stage["implement"] == "low_cost"  # lean default
    balanced = suggest_model_routing(profile_name="balanced")
    bmap = {s["stage"]: s["preferred_class"] for s in balanced["stages"]}
    assert bmap["review_code"] == "strong"
    assert bmap["implement"] == "low_cost"


# ── HTTP surface ──────────────────────────────────────────────────────────────


def test_estimate_http(client):
    r = client.post(
        "/v1/token-efficiency/estimate",
        json={
            "messages": [
                {"role": "system", "content": "stable prefix " * 20},
                {"role": "user", "content": "hello world"},
            ],
            "max_context_tokens": 10_000,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_estimated_tokens"] >= 1
    assert body["over_budget"] is False
    assert body["message_count"] == 2


def test_routing_http(client):
    r = client.get("/v1/token-efficiency/routing", params={"profile_name": "vag"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"] == "vag"
    assert any(s["stage"] == "implement" for s in body["stages"])


def test_policies_default_and_upsert(client, engine):
    listed = client.get("/v1/token-efficiency/policies", params={"project_id": PID})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["project_id"] == PID
    assert body["count"] >= 1
    stages = {p["stage"] for p in body["policies"]}
    assert PROJECT_DEFAULT_STAGE in stages

    put = client.put(
        "/v1/token-efficiency/policies",
        json={
            "project_id": PID,
            "policies": [
                {
                    "stage": "*",
                    "max_tokens_per_day": 1_000_000,
                    "max_cost_usd_micros_per_day": 10_000_000,
                    "alert_at_pct": 0.75,
                    "preferred_model_class": "low_cost",
                    "enabled": True,
                },
                {
                    "stage": "implement",
                    "max_total_tokens_per_call": 50_000,
                    "preferred_model_class": "low_cost",
                },
            ],
        },
    )
    assert put.status_code == 200, put.text
    stored = put.json()["policies"]
    by_stage = {p["stage"]: p for p in stored}
    assert by_stage["*"]["source"] == "stored"
    assert by_stage["*"]["max_tokens_per_day"] == 1_000_000
    assert by_stage["*"]["alert_at_pct"] == 0.75
    assert by_stage["implement"]["max_total_tokens_per_call"] == 50_000
    assert by_stage["implement"]["version"] >= 1

    # Idempotent second put bumps version
    put2 = client.put(
        "/v1/token-efficiency/policies",
        json={
            "project_id": PID,
            "policies": [
                {
                    "stage": "implement",
                    "max_total_tokens_per_call": 40_000,
                    "preferred_model_class": "low_cost",
                }
            ],
        },
    )
    assert put2.status_code == 200, put2.text
    impl = next(p for p in put2.json()["policies"] if p["stage"] == "implement")
    assert impl["max_total_tokens_per_call"] == 40_000
    assert impl["version"] >= 2


def test_policies_reject_bad_class(client):
    r = client.put(
        "/v1/token-efficiency/policies",
        json={
            "project_id": PID,
            "policies": [{"stage": "*", "preferred_model_class": "turbo"}],
        },
    )
    assert r.status_code == 422


def test_scorecard_and_consumption(client, engine):
    with begin(engine) as c:
        # heavy fresh, low cache → poor cache grade
        _insert_call(c, cost=500_000, fresh=50_000, cached=1_000, completion=500)
        _insert_call(
            c,
            cost=100_000,
            ok=False,
            error="timeout",
            stage="implement",
            fresh=5_000,
        )
        _insert_call(c, cost=50_000, stage="spec", fresh=2_000, cached=500)

    sc = client.get(
        "/v1/token-efficiency/scorecard",
        params={"project_id": PID},
    )
    assert sc.status_code == 200, sc.text
    body = sc.json()
    assert body["calls"] >= 3
    assert body["ok_calls"] >= 2
    assert body["failed_calls"] >= 1
    assert body["cache_ratio"] is not None
    assert body["failed_burn_usd_micros"] >= 100_000
    assert body["avg_tokens_per_call"] > 0
    assert "cost_per_ok_call_usd_micros" in body
    assert body["grades"]["cache_ratio"] in ("good", "fair", "poor", "n/a")
    assert any(s["stage"] == "implement" for s in body["by_stage"])

    cons = client.get(
        "/v1/token-efficiency/consumption",
        params={"project_id": PID, "window_hours": 24},
    )
    assert cons.status_code == 200, cons.text
    cbody = cons.json()
    assert cbody["window_hours"] == 24
    assert cbody["total_tokens"] >= body["total_tokens"] or cbody["calls"] >= 1
    assert "policy" in cbody
    assert "series" in cbody
    assert isinstance(cbody["alerts"], list)


def test_recommendations_extend_cost_insights(client, engine):
    with begin(engine) as c:
        # Material spend so cost_insights rules can fire + token rules
        _insert_call(c, cost=600_000, fresh=2_000_000, cached=0, stage="implement")
        _insert_call(
            c,
            cost=200_000,
            ok=False,
            error="error_max_turns at 41",
            stage="implement",
            fresh=10_000,
        )
        _insert_call(c, cost=300_000, stage="spec", fresh=20_000)

    r = client.post(
        "/v1/token-efficiency/recommendations",
        json={"project_id": PID, "profile_name": "balanced"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PID
    assert body["profile"] == "balanced"
    assert body["count"] == len(body["recommendations"])
    assert body["count"] >= 1
    kinds = {rec["kind"] for rec in body["recommendations"]}
    # At least one cost_insights or token_efficiency rule
    assert kinds & {
        "cache_leverage",
        "raise_cache_ratio",
        "failed_call_burn",
        "strong_on_cheap",
        "route_planning_low_cost",
        "high_avg_tokens",
        "turn_cap_pressure",
    }
    for rec in body["recommendations"]:
        assert rec["finding"] and rec["action"]
        assert rec["source"] in ("cost_insights", "token_efficiency")
    assert isinstance(body["model_routing"], list)


def test_scorecard_empty_project(client):
    other = f"proj_te_{uuid4().hex[:8]}"
    r = client.get(
        "/v1/token-efficiency/scorecard",
        params={"project_id": other},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["calls"] == 0
    assert body["cache_ratio"] is None
    assert body["grades"]["cache_ratio"] == "n/a"
