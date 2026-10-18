"""A natural-language observability question becomes a query PLAN written by a MODEL, checked by
BOSS, never executed (BOSSAPP-598, G8). The keyword map is a hint the model may ignore; with no
model the answer says so and there is no keyword fallback.

Seam: boss_engine.services.sre_ask.plan_query
"""
from __future__ import annotations

import json

from boss_engine.services.sre_ask import keyword_guess, plan_query


class Model:
    def __init__(self, answer: dict):
        self.answer, self.prompts = answer, []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return json.dumps(self.answer)


class NoModel:
    def create_text_response(self, prompt, system_prompt=None):
        raise RuntimeError("no model is connected")


def test_no_model_says_so_and_never_falls_back_to_keywords():
    plan = plan_query("boss-dogfood", "what is the HTTP error rate last hour", adapter=NoModel())
    assert plan["status"] == "no_model" and plan["suggested_query"] is None
    assert plan["executed"] is False and plan["public_ga"] is False
    assert plan["boss_guess"]["connector_kind"] == "prometheus", "the guess is kept, as a hint"


def test_the_model_writes_the_plan_and_boss_scopes_its_promql_to_the_project():
    model = Model({"status": "planned", "connector_kind": "prometheus", "capability": "metrics",
                   "query": 'sum(rate(http_requests_total{status=~"5..",project="other"}[5m]))',
                   "window": "1h", "reason": "error rate"})
    plan = plan_query("boss-dogfood", "how many requests fail?", adapter=model)
    assert plan["status"] == "planned" and plan["decided_by"] == "model"
    assert 'project="boss-dogfood"' in plan["suggested_query"]
    assert 'project="other"' not in plan["suggested_query"], "scoped on the parsed query"
    assert plan["window"]["duration"] == "1h" and plan["executed"] is False
    assert "BOSS'S KEYWORD GUESS (a hint)" in model.prompts[0]


def test_the_model_may_say_it_is_not_an_observability_question():
    plan = plan_query("p1", "why is the CEO sad about pasta", adapter=Model(
        {"status": "unknown", "reason": "not about the system's telemetry"}))
    assert plan["status"] == "unknown" and plan["reason"] and plan["suggested_query"] is None


def test_promql_boss_cannot_check_is_never_suggested():
    plan = plan_query("p1", "p99 of checkout", adapter=Model(
        {"status": "planned", "connector_kind": "prometheus", "query": "topk(3, x)",
         "window": "15m"}))
    assert plan["status"] == "unknown" and plan["suggested_query"] is None


def test_the_keyword_guess_is_only_a_guess():
    g = keyword_guess("p1", "datadog latency for the api service last 15m")
    assert g["connector_kind"] == "datadog" and g["window"]["duration"] == "15m"
