"""BOSS-280 P1 — per-agent cost attribution + cache-aware pricing. Against boss_test."""
from apps.api.app.config import get_settings
from apps.api.app.db import begin, get_engine
from sqlalchemy import text

from boss_engine.services.llm_telemetry import (
    record_llm_call,
    role_for_stage,
    set_llm_context,
)
from boss_engine.services.openai_adapter import estimate_cost_usd_micros
from boss_engine.services.core import make_id
from boss_engine.services.reports import ReportService

PID = "boss-dogfood"


# --- role derivation ----------------------------------------------------------
def test_role_for_stage_map():
    assert role_for_stage("write_tests") == "qa"
    assert role_for_stage("implement") == "software_engineer"
    assert role_for_stage("review_code") == "reviewer"
    assert role_for_stage("security_check") == "security_reviewer"
    assert role_for_stage("design") == "architect"
    assert role_for_stage("mystery_stage") is None


def test_record_llm_call_writes_role_and_cache_columns(engine):
    run_id = make_id("crun")
    set_llm_context(db_engine=get_engine(get_settings()), project_id=PID, run_id=run_id)
    record_llm_call(
        engine_name="claude_code", stage="implement",
        usage={"model": "claude-sonnet-5", "prompt_tokens": 100, "completion_tokens": 50,
               "cache_read_tokens": 900, "cache_write_tokens": 40, "total_tokens": 1090},
        cost_usd_micros=1234)
    with begin(engine) as c:
        row = c.execute(
            text("SELECT role, cache_read_tokens, cache_write_tokens, prompt_tokens "
                 "FROM llm_calls WHERE code_task_run_id = :r"), {"r": run_id}).mappings().one()
    assert row["role"] == "software_engineer"
    assert row["cache_read_tokens"] == 900 and row["cache_write_tokens"] == 40
    assert row["prompt_tokens"] == 100          # fresh input only, cache NOT folded in


# --- cache-aware pricing ------------------------------------------------------
def test_estimate_prices_cache_tokens_at_cache_rates():
    base = {"model": "gpt-4o", "prompt_tokens": 1_000_000, "completion_tokens": 0}
    fresh = estimate_cost_usd_micros(base)
    # same million input tokens as cache READS should cost ~10% of fresh input
    cached = estimate_cost_usd_micros({**base, "prompt_tokens": 0,
                                       "cache_read_tokens": 1_000_000})
    assert cached == int(round(fresh * 0.1))
    # cache WRITES cost 1.25x input
    writes = estimate_cost_usd_micros({**base, "prompt_tokens": 0,
                                       "cache_write_tokens": 1_000_000})
    assert writes == int(round(fresh * 1.25))
    # self-reported cost still wins outright
    assert estimate_cost_usd_micros({**base, "cost_usd_micros": 777}) == 777


# --- $/agent reporting --------------------------------------------------------
def test_reports_llm_spend_by_role_and_cache(engine):
    run_id = make_id("crun")
    set_llm_context(db_engine=get_engine(get_settings()), project_id=PID, run_id=run_id)
    record_llm_call(engine_name="claude_code", stage="write_tests",
                    usage={"model": "m1", "prompt_tokens": 10, "completion_tokens": 5,
                           "cache_read_tokens": 90, "total_tokens": 105},
                    cost_usd_micros=500)
    record_llm_call(engine_name="claude_code", stage="implement",
                    usage={"model": "m1", "prompt_tokens": 20, "completion_tokens": 10,
                           "total_tokens": 30},
                    cost_usd_micros=2000)
    with begin(engine) as c:
        block = ReportService(get_settings())._llm_spend(c, PID)
    roles = {r["role"]: r for r in block["by_role"]}
    assert roles["software_engineer"]["spend_usd_micros"] >= 2000
    assert roles["qa"]["spend_usd_micros"] >= 500
    assert block["cache"]["cache_read_tokens"] >= 90
    assert block["cache"]["cache_hit_ratio"] is not None
    models = {m["model"]: m for m in block["by_model"]}
    assert models["m1"]["calls"] >= 2
