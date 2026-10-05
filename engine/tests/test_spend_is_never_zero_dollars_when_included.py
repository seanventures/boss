"""Zero metered spend because the work ran on a subscription is never "$0" (spec §4.10).

The engine's half of the rule: a call that ran on a subscription never counts as spend (its
cost_usd_micros is an API-equivalent value, not money), and a group whose every call ran on a
subscription says `included`, so the console can read "Included in plan" instead of $0.00.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from ._obs_seed import llm_call, pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import obs_view


def test_included_calls_are_never_spend_and_say_included():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        now = c.execute(text("SELECT now()")).scalar()
        for i in range(5):
            llm_call(c, pid, now - timedelta(minutes=10 + i), model="claude-sonnet-4-5",
                     cost=250_000, included=True)
        llm_call(c, pid, now - timedelta(minutes=3), model="gpt-5-mini", cost=1_234)
        body = obs_view.models(c, get_settings(), pid)
        pulse = obs_view.pulse(c, get_settings(), pid)
    assert body["totals"]["spend_micros"] == 1_234
    assert body["totals"]["included_calls"] == 5
    sub = next(g for g in body["groups"] if g["key"] == "claude-sonnet-4-5")
    assert sub["spend_micros"] == 0 and sub["included"] is True
    metered = next(g for g in body["groups"] if g["key"] == "gpt-5-mini")
    assert metered["included"] is False and metered["spend_micros"] == 1_234
    assert pulse["spend_24h_micros"] == 1_234


def test_all_included_is_zero_spend_with_every_call_included():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        now = c.execute(text("SELECT now()")).scalar()
        llm_call(c, pid, now - timedelta(minutes=5), cost=900_000, included=True)
        body = obs_view.models(c, get_settings(), pid)
    t = body["totals"]
    assert t["spend_micros"] == 0 and t["included_calls"] == t["calls"] == 1
    assert body["groups"][0]["included"] is True
