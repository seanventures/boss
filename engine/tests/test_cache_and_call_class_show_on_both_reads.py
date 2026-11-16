"""Observability and get_costs name cache tokens and the call class (BOSSAPP-1009).

Both read llm_calls. A build call and a chat call, each with cache read and cache write,
come back as the same cache totals and the same per-class breakdown. A stored
preferred_model_class changes the routing suggestion; a profile-only call does not.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import obs_view, token_efficiency
from boss_engine.tools.builtin import build_default_registry


def _call(c, pid, at, *, stage, call_class, model="m-a", tin=10, tout=4, cost=0,
          included=False, cache_read=0, cache_write=0):
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, stage, engine, model, role, "
        " prompt_tokens, completion_tokens, total_tokens, cache_read_tokens, "
        " cache_write_tokens, cost_usd_micros, price_estimated, latency_ms, retries, "
        " turns, ok, included_in_subscription, call_class, created_at) "
        "VALUES (:id, :p, :stage, 'claude_code', :model, 'software_engineer', "
        " :tin, :tout, :tt, :cr, :cw, :cost, false, 500, 0, 1, true, :inc, :cls, :at)"),
        {"id": f"llmc_{uuid4().hex}", "p": pid, "stage": stage, "model": model,
         "tin": tin, "tout": tout, "tt": tin + tout, "cr": cache_read, "cw": cache_write,
         "cost": cost, "inc": included, "cls": call_class, "at": at})


class _Ctx:
    def __init__(self, engine):
        self.engine = engine

    def begin(self):
        return self.engine.begin()


def _classes(rows):
    return {r["call_class"]: (r["calls"], r["cache_read_tokens"], r["cache_write_tokens"])
            for r in rows}


def test_models_and_get_costs_share_cache_and_call_class():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        now = c.execute(text("SELECT now()")).scalar()
        at = now - timedelta(minutes=5)
        _call(c, pid, at, stage="implement", call_class="build_stage",
              tin=10, tout=4, cost=2_000_000, cache_read=1000, cache_write=200)
        _call(c, pid, at, stage="chat", model="chat-model", call_class="chat",
              tin=8, tout=2, cost=30_000_000, included=True, cache_read=400, cache_write=50)
        body = obs_view.models(c, get_settings(), pid, win="24h")
        grouped = obs_view.models(c, get_settings(), pid, win="24h", group="call_class")
    costs = build_default_registry().invoke(
        "get_costs", _Ctx(eng), project_id=pid, actor="ceo", args={"days": 7})
    want = {"build_stage": (1, 1000, 200), "chat": (1, 400, 50)}
    totals = body["totals"]
    assert (totals["cache_read_tokens"], totals["cache_write_tokens"]) == (1400, 250)
    assert _classes(body["by_call_class"]) == want
    assert _classes(costs["by_call_class"]) == want
    assert (costs["cache_read_tokens"], costs["cache_write_tokens"]) == (1400, 250)
    assert _classes(
        [{"call_class": g["key"], "calls": g["calls"],
          "cache_read_tokens": g["cache_read_tokens"],
          "cache_write_tokens": g["cache_write_tokens"]} for g in grouped["groups"]]
    ) == want


def test_a_stored_preferred_class_changes_the_routing_suggestion():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        token_efficiency.upsert_policies(
            c, project_id=pid, actor="ceo",
            policies=[{"stage": "implement", "preferred_model_class": "strong"}])
        routed = token_efficiency.routing_for(c, pid, profile_name="lean")
    by = {s["stage"]: s for s in routed["stages"]}
    assert by["implement"]["preferred_class"] == "strong"
    assert "stored" in by["implement"]["rationale"]
    bare = {s["stage"]: s["preferred_class"]
            for s in token_efficiency.suggest_model_routing(profile_name="lean")["stages"]}
    assert bare["implement"] == "low_cost"
