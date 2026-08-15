"""NL observability ask is a plan only — never a live PromQL/Datadog executor.

Seam: boss_engine.services.sre_ask.plan_query
"""
from __future__ import annotations

from boss_engine.services.sre_ask import plan_query


def test_unknown_question_is_unknown_not_fake_promql():
    plan = plan_query("boss-dogfood", "why is the CEO sad about pasta")
    assert plan["status"] == "unknown"
    assert plan["reason"]
    assert plan["executed"] is False
    assert plan["public_ga"] is False
    assert plan.get("suggested_query") in (None, "")
    blob = str(plan).lower()
    assert "sum(rate(" not in blob
    assert "promql" not in blob or plan["status"] == "unknown"


def test_error_rate_maps_to_prometheus_plan_without_executing():
    plan = plan_query("boss-dogfood", "what is the HTTP error rate last hour")
    assert plan["status"] == "planned"
    assert plan["connector_kind"] == "prometheus"
    assert plan["suggested_query"]
    assert plan["window"]["duration"] == "1h"
    assert plan["executed"] is False
    assert plan["public_ga"] is False
    assert plan["project_id"] == "boss-dogfood"


def test_loki_logs_maps_to_loki_plan():
    plan = plan_query("p1", "show loki logs for checkout")
    assert plan["status"] == "planned"
    assert plan["connector_kind"] == "loki"
    assert plan["executed"] is False


def test_datadog_keyword_maps_to_datadog_plan_not_live():
    plan = plan_query("p1", "datadog latency for the api service last 15m")
    assert plan["status"] == "planned"
    assert plan["connector_kind"] == "datadog"
    assert plan["window"]["duration"] == "15m"
    assert plan["executed"] is False
