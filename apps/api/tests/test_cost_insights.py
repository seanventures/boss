"""BOSS-280 P2 — provider-dimensioned pricing + the deterministic cost-insights analyzer."""
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from boss_engine.services.core import make_id
from boss_engine.services.cost_insights import build_insights
from boss_engine.services.openai_adapter import price_for_model

PID = "boss-dogfood"


# --- provider-dimensioned pricing --------------------------------------------
def test_provider_scoped_price_wins(monkeypatch):
    monkeypatch.setenv("BOSS_MODEL_PRICES_JSON",
                       '{"azure:gpt-4o": [5.0, 20.0], "gpt-4o": [2.5, 10.0]}')
    get_settings.cache_clear()
    try:
        inp, out, known = price_for_model("gpt-4o", provider="azure")
        assert (inp, out, known) == (5.0, 20.0, True)          # provider key wins
        inp2, out2, _ = price_for_model("gpt-4o")
        assert (inp2, out2) == (2.5, 10.0)                     # bare model unchanged
        _, _, known3 = price_for_model("totally-unknown-model")
        assert known3 is False                                 # fallback flagged
    finally:
        get_settings.cache_clear()


# --- insights -----------------------------------------------------------------
def _call(c, *, stage="implement", cost, ok=True, error=None, fresh=1000, cached=0,
          estimated=False):
    c.execute(
        text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
             "prompt_tokens, completion_tokens, total_tokens, cache_read_tokens, "
             "cost_usd_micros, price_estimated, ok, error) "
             "VALUES (:id, :p, :r, :st, 'e', 'm', :fr, 10, :tt, :cr, :c, :pe, :ok, :err)"),
        {"id": make_id("llm"), "p": PID, "r": make_id("crun"), "st": stage, "fr": fresh,
         "tt": fresh + cached + 10, "cr": cached, "c": cost, "pe": estimated, "ok": ok,
         "err": error})


def test_insights_quiet_below_materiality(engine):
    with begin(engine) as c:
        _call(c, cost=10)                                       # $0.00001 — noise
        out = build_insights(c, PID)
    assert out["insights"] == []


def test_insights_fire_and_rank(engine):
    with begin(engine) as c:
        # heavy fresh input, zero cache -> cache_leverage
        _call(c, cost=600_000, fresh=2_000_000, cached=0)
        # failed spend >= 5% -> failed_call_burn (and max_turns -> turn_cap_pressure)
        _call(c, cost=200_000, ok=False, error="error_max_turns at 41", stage="implement")
        # planning-class stage spend >= 10% -> strong_on_cheap
        _call(c, cost=300_000, stage="spec")
        # estimated share >= 20% -> estimated_pricing
        _call(c, cost=400_000, estimated=True)
        out = build_insights(c, PID)
    kinds = [i["kind"] for i in out["insights"]]
    assert set(kinds) >= {"cache_leverage", "failed_call_burn", "strong_on_cheap",
                          "estimated_pricing", "turn_cap_pressure"}
    # every insight quantifies + prescribes
    for i in out["insights"]:
        assert i["finding"] and i["action"]
        assert "potential_saving_usd_micros" in i
    # high-severity insights rank first
    sevs = [i["severity"] for i in out["insights"]]
    assert sevs.index("high") == 0 if "high" in sevs else True


def test_insights_http(client, engine):
    with begin(engine) as c:
        _call(c, cost=600_000, fresh=2_000_000, cached=0)
    r = client.get("/v1/budget/insights", params={"project_id": PID})
    assert r.status_code == 200
    assert any(i["kind"] == "cache_leverage" for i in r.json()["insights"])
