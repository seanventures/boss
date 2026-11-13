"""BOSSAPP-864: seat fees amortised into daily rows; invoices reconcile the estimate.

A subscription's seat fee is recorded (revisioned, one row per day); a provider invoice (CSV or
JSON, Anthropic / OpenAI / OpenRouter) becomes one cost adjustment per line with the estimate it
replaces and why; Spend's report says estimated, reconciled, fees and total.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import subscription_fees as sf

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")
DOLLAR = 1_000_000


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def project(eng):
    pid = f"p_864_{uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_864', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_864', 'org_864', 'w') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_864', :p, :p)"),
                  {"p": pid})
    return pid


def _call(eng, project, when: datetime, usd: float, *, engine="anthropic", model="claude-sonnet-4",
          included=False):
    with eng.begin() as c:
        c.execute(text("INSERT INTO llm_calls (id, project_id, engine, model, cost_usd_micros, "
                       " included_in_subscription, created_at) VALUES (:i, :p, :e, :m, :c, :inc, :t)"),
                  {"i": f"llmc_{uuid4().hex[:16]}", "p": project, "e": engine, "m": model,
                   "c": int(usd * DOLLAR), "inc": included, "t": when})


def test_a_seat_fee_is_amortised_per_day_and_revisioned(eng, project):
    with eng.begin() as c:
        sf.set_price(c, project, provider="anthropic", plan="Claude Max", seats=2,
                     monthly_usd_micros=100 * DOLLAR, effective_from=date(2026, 9, 1), actor="ceo")
        sf.set_price(c, project, provider="anthropic", plan="Claude Max", seats=3,
                     monthly_usd_micros=100 * DOLLAR, effective_from=date(2026, 9, 16), actor="ceo")
        assert sf.accrue(c, project, through=date(2026, 9, 30)) == 30
        assert sf.accrue(c, project, through=date(2026, 9, 30)) == 0          # idempotent
        total = c.execute(text("SELECT SUM(amount_usd_micros) FROM subscription_fee_days "
                               "WHERE project_id = :p"), {"p": project}).scalar()
        prices = c.execute(text("SELECT seats, effective_to FROM subscription_prices WHERE project_id = :p "
                                "ORDER BY effective_from"), {"p": project}).all()
    # 15 days at 2 seats + 15 days at 3 seats of $100/month (30-day September)
    assert total == 15 * sf.daily_fee(100 * DOLLAR, 2, date(2026, 9, 1)) \
        + 15 * sf.daily_fee(100 * DOLLAR, 3, date(2026, 9, 1))
    assert abs(total - 250 * DOLLAR) < 100
    assert prices[0] == (2, date(2026, 9, 15)) and prices[1][1] is None


def test_an_invoice_csv_reconciles_the_estimate(eng, project):
    _call(eng, project, datetime(2026, 9, 10, tzinfo=timezone.utc), 30)
    _call(eng, project, datetime(2026, 9, 20, tzinfo=timezone.utc), 20)
    _call(eng, project, datetime(2026, 9, 21, tzinfo=timezone.utc), 99, included=True)  # plan: $0
    _call(eng, project, datetime(2026, 9, 22, tzinfo=timezone.utc), 5, engine="openai", model="gpt-5")
    csv_text = ("Invoice Number,Line ID,Description,Period Start,Period End,Amount (USD)\n"
                "INV-9,1,Claude API usage,2026-09-01,2026-09-30,\"$62.40\"\n"
                "INV-9,2,bad line,,2026-09-30,1.00\n")
    with eng.begin() as c:
        out = sf.import_invoice(c, project, provider="anthropic", content=csv_text, actor="ceo")
        again = sf.import_invoice(c, project, provider="anthropic", content=csv_text, actor="ceo")
        adj = c.execute(text("SELECT invoice_usd_micros, estimated_usd_micros, adjustment_usd_micros, reason "
                             "FROM cost_adjustments WHERE project_id = :p"), {"p": project}).mappings().one()
        report = sf.report(c, project, date(2026, 9, 1), date(2026, 9, 30))
    assert (out["lines"], out["added"], len(out["problems"])) == (1, 1, 1)
    assert again["added"] == 0 and again["skipped_existing"] == 1
    assert adj["estimated_usd_micros"] == 50 * DOLLAR
    assert adj["invoice_usd_micros"] == 62_400_000
    assert adj["adjustment_usd_micros"] == 12_400_000 and "above the estimate" in adj["reason"]
    assert report["estimated_usd_micros"] == 55 * DOLLAR
    assert report["reconciled_usd_micros"] == 62_400_000 + 5 * DOLLAR
    assert report["by_provider"]["anthropic"]["invoice_lines"] == 1
    assert report["total_usd_micros"] == report["reconciled_usd_micros"] + report["fees_usd_micros"]


def test_openai_and_openrouter_json_exports_are_read(eng, project):
    openai_costs = {"data": [{"start_time": 1788220800, "end_time": 1788307200,
                              "results": [{"amount": {"value": 1.25, "currency": "usd"},
                                           "line_item": "gpt-5, input"}]}]}
    router = [{"date": "2026-09-03", "cost": "0.75", "id": "gen-1", "model": "openrouter/auto"}]
    with eng.begin() as c:
        a = sf.import_invoice(c, project, provider="openai", content=json.dumps(openai_costs), actor="ceo")
        b = sf.import_invoice(c, project, provider="openrouter", content=json.dumps(router), actor="ceo")
        rows = c.execute(text("SELECT provider, invoice_usd_micros FROM cost_adjustments "
                              "WHERE project_id = :p ORDER BY provider"), {"p": project}).all()
    assert (a["added"], b["added"]) == (1, 1)
    assert rows == [("openai", 1_250_000), ("openrouter", 750_000)]


def test_bad_input_is_refused_in_words(eng, project):
    with eng.begin() as c:
        with pytest.raises(sf.SubscriptionFeeError):
            sf.import_invoice(c, project, provider="aws", content="a,b\n1,2", actor="ceo")
        with pytest.raises(sf.SubscriptionFeeError):
            sf.import_invoice(c, project, provider="openai", content="", actor="ceo")
        with pytest.raises(sf.SubscriptionFeeError):
            sf.set_price(c, project, provider="", plan="x", monthly_usd_micros=1, actor="ceo")


def test_no_prices_and_no_invoices_is_the_estimate(eng, project):
    _call(eng, project, datetime(2026, 9, 10, tzinfo=timezone.utc), 7)
    with eng.begin() as c:
        r = sf.report(c, project, date(2026, 9, 1), date(2026, 9, 30))
    assert r["estimated_usd_micros"] == r["reconciled_usd_micros"] == r["total_usd_micros"] == 7 * DOLLAR
    assert r["fees_usd_micros"] == 0
