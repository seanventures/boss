"""BOSSAPP-869: block unpriced usage, off by default.

  * estimate (the default) changes nothing: reserve() admits work however much unpriced money
    the ledger holds — the dearest-rate estimate, labelled, as before;
  * block: once today's unpriced money (price_estimated, not included) is past the allowance,
    reserve() refuses paid work as `unpriced_blocked` and opens the existing
    increase_budget_or_defer decision — one card per cause, repeats land on it;
  * $0 work and included (subscription) rows never trip it; the allowance lifts it;
  * the knobs are project settings with the install's env as default, and Govern shows them.

Needs BOSS_TEST_DATABASE_URL for the reserve() tests.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import project_settings as ps
from boss_engine.services import unpriced_policy as up

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


def _settings(**kw):
    from boss_engine.config import get_settings

    base = get_settings()
    return base.model_copy(update=kw) if kw else base


def test_the_knobs_are_project_settings_and_default_to_estimate():
    assert ps.KNOBS["budget_unpriced_policy"]["choices"] == ["estimate", "block"]
    assert ps.coerce("budget_unpriced_policy", "BLOCK") == "block"
    with pytest.raises(ValueError):
        ps.coerce("budget_unpriced_policy", "maybe")
    assert ps.coerce("budget_unpriced_allowance_usd_micros", "250000") == 250000
    with pytest.raises(ValueError):
        ps.coerce("budget_unpriced_allowance_usd_micros", -1)
    s = _settings()
    assert s.budget_unpriced_policy == "estimate" and s.budget_unpriced_allowance_usd_micros == 0


def test_the_decision_it_opens_is_a_classified_refusal():
    from boss_engine.services.decision_actions import REFUSALS

    assert REFUSALS["increase_budget_or_defer"] == "budget"


@pytest.fixture()
def pg():
    if not PG_URL.startswith("postgres") or "test" not in PG_URL.rsplit("/", 1)[-1]:
        pytest.skip("needs BOSS_TEST_DATABASE_URL naming a disposable test database")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    pid = "p_869_" + uuid4().hex[:8]
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_869', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_869', 'org_869', 'w') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_869', 'u', :p)"), {"p": pid})
        c.execute(text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                       "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) VALUES "
                       "(:i, 'project', :p, 100000000, 1000000000, 0)"),
                  {"i": "bp_" + uuid4().hex[:12], "p": pid})
    yield SimpleNamespace(eng=eng, pid=pid)
    eng.dispose()


def _unpriced(c, p, cost, *, included=False, estimated=True):
    c.execute(text("INSERT INTO llm_calls (id, project_id, stage, engine, model, cost_usd_micros, "
                   "price_estimated, included_in_subscription) VALUES "
                   "(:i, :p, 'implement', 'native', 'mystery-model', :c, :e, :inc)"),
              {"i": "llmc_" + uuid4().hex[:12], "p": p, "c": cost, "e": estimated,
               "inc": included})


def _reserve(env, amount=50_000, settings=None):
    from boss_engine.services.budget import BudgetGovernor

    with env.eng.begin() as c:
        return BudgetGovernor(settings or _settings()).reserve(
            c, env.pid, "model", amount, "unpriced test", 30)


def _set(env, key, value):
    with env.eng.begin() as c:
        ps.set_one(c, env.pid, key, value, "ceo", scope="project")


def _decisions(env):
    with env.eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, recommendation, status FROM decisions WHERE project_id = :p "
            "AND recommendation->>'action' = 'increase_budget_or_defer'"), {"p": env.pid}).mappings()]


def test_estimate_is_unchanged_whatever_the_ledger_holds(pg):
    with pg.eng.begin() as c:
        _unpriced(c, pg.pid, 5_000_000)
    got = _reserve(pg)
    assert got["status"] == "reserved", got
    assert _decisions(pg) == []


def test_block_holds_paid_work_past_the_allowance_and_opens_one_decision(pg):
    _set(pg, up.POLICY_KNOB, "block")
    assert _reserve(pg)["status"] == "reserved"  # nothing unpriced yet
    with pg.eng.begin() as c:
        _unpriced(c, pg.pid, 300_000)
    refused = _reserve(pg)
    assert refused["status"] == "denied" and refused["refusal"] == "unpriced_blocked"
    assert refused["transient"] is False and refused["decision_id"]
    assert refused["unpriced_today_usd_micros"] == 300_000
    assert "Unpriced usage blocked" in refused["message"]
    again = _reserve(pg)
    assert again["decision_id"] == refused["decision_id"]  # one card per cause
    cards = _decisions(pg)
    assert len(cards) == 1 and cards[0]["status"] == "pending"
    # $0 work is never held: it cannot be unpriced money
    assert _reserve(pg, amount=0)["status"] == "reserved"
    with pg.eng.begin() as c:
        evals = c.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p "
            "AND event_type = 'budget.unpriced_blocked'"), {"p": pg.pid}).scalar()
    assert evals >= 1


def test_the_allowance_and_included_rows(pg):
    _set(pg, up.POLICY_KNOB, "block")
    _set(pg, up.ALLOWANCE_KNOB, 1_000_000)
    with pg.eng.begin() as c:
        _unpriced(c, pg.pid, 900_000)
        _unpriced(c, pg.pid, 9_000_000, included=True)     # a subscription: not money
        _unpriced(c, pg.pid, 9_000_000, estimated=False)   # priced: not unpriced
    assert _reserve(pg)["status"] == "reserved"
    with pg.eng.begin() as c:
        _unpriced(c, pg.pid, 200_000)
    assert _reserve(pg)["refusal"] == "unpriced_blocked"
    with pg.eng.begin() as c:
        assert up.view(c, _settings(), pg.pid) == {
            "value": "block", "choices": ["estimate", "block"], "default": "estimate",
            "allowance_usd_micros": 1_000_000, "unpriced_today_usd_micros": 1_100_000}


def test_the_install_default_is_read_when_the_project_has_none(pg):
    with pg.eng.begin() as c:
        _unpriced(c, pg.pid, 10)
    got = _reserve(pg, settings=_settings(budget_unpriced_policy="block"))
    assert got["refusal"] == "unpriced_blocked"


def test_govern_view_shows_it(pg):
    from boss_engine.services import govern_view

    with pg.eng.begin() as c:
        view = govern_view.build(c, _settings(), pg.pid)
    assert view["unpriced"]["value"] == "estimate"
    assert view["unpriced"]["unpriced_today_usd_micros"] == 0
