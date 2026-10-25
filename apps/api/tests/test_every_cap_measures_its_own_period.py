"""Every budget cap measures its own period: the daily cap today's money, the monthly cap this
month's.

`daily_cap_usd_micros` is a DAILY cap (budget_v0.yaml, the Budget screen, the intensity slider's
"$5 → $500 / day"), yet status_for_project compared it with every cost receipt the project ever
had — no time filter at all. An install that reached its cap stayed red for ever: waiting never
helped, red holds every non-P0 job, so every install eventually stopped working. The monthly cap
was shown and never enforced.

These drive the real BudgetGovernor, job gate, readiness and API against Postgres. Time is moved
by injecting `now` and by dating receipts and reservations, never by sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services.budget import BudgetGovernor, budget_period
from boss_engine.services.jobs import JobLeaseService
from boss_engine.services.model_failures import explain

PROJECT = "boss-dogfood"
TAG = "test.period"
#: A fixed moment, far from any real "today", so no test straddles a real midnight.
NOW = datetime(2031, 5, 20, 12, 0, tzinfo=timezone.utc)


def _receipt(c, amount: int, at: datetime | None = None) -> None:
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
        "estimated_usd_micros, actual_usd_micros, status, created_at) "
        "VALUES (:i, :p, 'model', :m, :a, :a, 'actual', COALESCE(:at, now()))"),
        {"i": f"cost_{uuid4().hex[:12]}", "p": PROJECT, "m": TAG, "a": amount, "at": at})


def _status(engine, now: datetime | None = None) -> dict:
    with engine.begin() as c:
        return BudgetGovernor(api_main.settings).status_for_project(c, PROJECT, now=now)


@pytest.fixture
def caps(engine):
    """Clean ledger; `set_caps(daily, monthly)` in dollars."""
    def clean():
        with engine.begin() as c:
            c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
            c.execute(text("UPDATE budget_reservations SET status = 'released' "
                           "WHERE project_id = :p AND status = 'active'"), {"p": PROJECT})

    def set_caps(daily: float, monthly: float) -> None:
        with engine.begin() as c:
            c.execute(text(
                "UPDATE budget_policies SET daily_cap_usd_micros = :d, "
                "monthly_cap_usd_micros = :m WHERE scope_type = 'project' AND scope_id = :p"),
                {"d": int(daily * 1e6), "m": int(monthly * 1e6), "p": PROJECT})

    clean()
    set_caps(10, 200)
    yield set_caps
    clean()


def test_yesterdays_spend_does_not_count_today(engine, caps):
    """$9.50 spent at 22:00 yesterday: red until midnight, green after it."""
    yesterday_evening = NOW.replace(hour=0) - timedelta(hours=2)
    with engine.begin() as c:
        _receipt(c, 9_500_000, yesterday_evening)

    before = _status(engine, yesterday_evening + timedelta(hours=1))
    assert before["budget_state"] == "red", before
    assert before["period"] == "day" and before["actual_usd_micros"] == 9_500_000

    after = _status(engine, NOW.replace(hour=0, minute=1))
    assert after["budget_state"] == "green", "the day turned over; yesterday is not today"
    day = after["periods"]["day"]
    assert day["actual_usd_micros"] == 0 and day["available_usd_micros"] == 10_000_000
    assert day["utilization_ratio"] == 0 and day["budget_state"] == "green"
    assert day["resets_at"] == "2031-05-21T00:00:00+00:00"
    assert day["resets_at_label"] == "00:00 UTC"
    # It is still this month's money: 4.75% of $200 is now the period nearest its cap, and
    # the top-level numbers say so rather than pretending the day is all there is.
    assert after["periods"]["month"]["actual_usd_micros"] == 9_500_000
    assert after["period"] == "month" and after["actual_usd_micros"] == 9_500_000
    assert after["budget_timezone"] == "UTC"


def test_all_time_spend_never_keeps_an_install_red(engine, caps):
    """The incident: months of receipts summing far past the daily cap, none of them today."""
    with engine.begin() as c:
        for days_ago in range(1, 40):
            _receipt(c, 3_000_000, NOW - timedelta(days=days_ago))
    status = _status(engine, NOW)
    assert status["periods"]["day"]["actual_usd_micros"] == 0
    assert status["budget_state"] != "red", status


def test_a_reservation_counts_in_the_period_it_was_made(engine, caps):
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        made = governor.reserve(c, PROJECT, "model", 4_000_000, "period test", 60)
        assert made["status"] == "reserved", made
        today = governor.status_for_project(c, PROJECT)
        assert today["reserved_usd_micros"] == 4_000_000
        assert today["active_reservations_count"] == 1
        assert today["committed_usd_micros"] == 4_000_000

        # The same active hold, had it been made before midnight, is yesterday's.
        c.execute(text("UPDATE budget_reservations SET created_at = :at WHERE id = :id"),
                  {"at": budget_period("day")["start"] - timedelta(minutes=5),
                   "id": made["reservation_id"]})
        moved = governor.status_for_project(c, PROJECT)
        assert moved["periods"]["day"]["reserved_usd_micros"] == 0
        assert moved["periods"]["day"]["active_reservations_count"] == 0


def test_the_monthly_cap_sums_the_month(engine, caps):
    caps(100, 20)
    with engine.begin() as c:
        _receipt(c, 8_000_000, datetime(2031, 5, 3, 9, tzinfo=timezone.utc))
        _receipt(c, 8_000_000, datetime(2031, 5, 10, 9, tzinfo=timezone.utc))
        _receipt(c, 3_000_000, datetime(2031, 5, 20, 9, tzinfo=timezone.utc))
        _receipt(c, 8_000_000, datetime(2031, 4, 28, 9, tzinfo=timezone.utc))  # last month
    status = _status(engine, NOW)
    month, day = status["periods"]["month"], status["periods"]["day"]
    assert month["actual_usd_micros"] == 19_000_000
    assert day["actual_usd_micros"] == 3_000_000
    assert month["budget_state"] == "red" and day["budget_state"] == "green"
    # The binding period speaks for the budget: this month's.
    assert status["period"] == "month" and status["budget_state"] == "red"
    assert status["cap_usd_micros"] == 20_000_000
    assert status["available_usd_micros"] == 1_000_000
    assert status["resets_at_label"] == "00:00 UTC on 1 June"
    assert status["cap_name"] == "monthly cap"
    # And on the first of next month it is clear.
    assert _status(engine, datetime(2031, 6, 1, 0, 1, tzinfo=timezone.utc))["budget_state"] \
        == "green"


def test_reserve_refuses_on_the_period_cap_and_says_when_it_resets(engine, caps):
    caps(100, 20)
    with engine.begin() as c:
        _receipt(c, 19_000_000)
        refused = BudgetGovernor(api_main.settings).reserve(
            c, PROJECT, "model", 2_000_000, "period test", 60)
    assert refused["status"] == "denied" and refused["refusal"] == "cap_exceeded", refused
    assert refused["period"] == "month"
    month = budget_period("month")
    assert refused["message"].startswith(
        "Budget cap exceeded: this month's budget is 95% used ($19 of $20). "
        f"It resets at 00:00 UTC on 1 {month['end']:%B}, or raise the monthly cap in Govern › Limits.")
    said = explain(refused["message"])
    assert said["kind"] == "budget_refused"
    assert said["headline"] == ("The budget refused this work. This month's budget is 95% used "
                                "($19 of $20).")
    assert said["action"].startswith(f"It resets at 00:00 UTC on 1 {month['end']:%B}")

    # A day's cap is today's money: what fits today is reserved.
    caps(10, 200)
    with engine.begin() as c:
        c.execute(text("UPDATE cost_receipts SET created_at = :at WHERE project_id = :p"),
                  {"at": budget_period("day")["start"] - timedelta(minutes=1), "p": PROJECT})
        made = BudgetGovernor(api_main.settings).reserve(
            c, PROJECT, "model", 2_000_000, "period test", 60)
    assert made["status"] == "reserved", made


def test_the_job_gate_holds_until_the_period_rolls_over(engine, caps):
    with engine.begin() as c:
        _receipt(c, 9_500_000)
    kind = f"test.period.{uuid4().hex[:8]}"
    leases = JobLeaseService(api_main.settings)
    day = budget_period("day")
    with engine.begin() as c:
        job = leases.create_job(c, PROJECT, kind, {"discovery_run_id": "none"})
        assert leases.claim_job(c, "worker-test", [kind], 60) is None, "red holds non-P0 work"
        row = c.execute(text("SELECT last_error, run_after FROM jobs WHERE id = :j"),
                        {"j": job["id"]}).mappings().one()
    message = row["last_error"]["message"]
    assert message.startswith(
        "Held — the budget is red (95% of today's $10 used), so only P0 work runs. "
        "It resets at 00:00 UTC, or raise the daily cap in Govern › Limits."), message
    assert row["run_after"] <= day["end"], "never deferred past the reset"
    said = explain(message)
    assert said["kind"] == "budget_hold"
    assert said["action"].startswith("It resets at 00:00 UTC, or raise the daily cap in Govern › Limits.")

    # Midnight passes: today's money becomes yesterday's, and the held job is due again.
    with engine.begin() as c:
        c.execute(text("UPDATE cost_receipts SET created_at = :at WHERE project_id = :p"),
                  {"at": day["start"] - timedelta(minutes=1), "p": PROJECT})
        c.execute(text("UPDATE jobs SET run_after = now() - interval '1 second' "
                       "WHERE id = :j"), {"j": job["id"]})
        claimed = leases.claim_job(c, "worker-test", [kind], 60)
    assert claimed is not None and claimed["id"] == job["id"], "the gate released it"


def test_the_status_api_names_the_period(client, engine, caps):
    with engine.begin() as c:
        _receipt(c, 2_000_000)
    body = client.get("/v1/budget/status",
                      params={"scope_type": "project", "scope_id": PROJECT}).json()
    assert body["period"] == "day"
    assert body["resets_at_label"] == "00:00 UTC"
    assert body["actual_usd_micros"] == 2_000_000
    assert set(body["periods"]) == {"day", "month"}
    assert body["periods"]["month"]["cap_usd_micros"] == 200_000_000
    assert body["periods"]["day"]["resets_at"] == budget_period("day")["end"].isoformat()
