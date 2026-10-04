"""A budget period's boundary and its words, defined once (budget.budget_period) and said the
same way by every hold and refusal (model_failures)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from boss_engine.services.budget import budget_period
from boss_engine.services.model_failures import (
    budget_hold_sentence,
    explain,
    held_headline,
)


@pytest.mark.parametrize("now,start,end,label", [
    (datetime(2031, 5, 20, 23, 59, tzinfo=timezone.utc), "2031-05-20T00:00:00+00:00",
     "2031-05-21T00:00:00+00:00", "00:00 UTC"),
    (datetime(2031, 12, 31, 23, 59, tzinfo=timezone.utc), "2031-12-31T00:00:00+00:00",
     "2032-01-01T00:00:00+00:00", "00:00 UTC"),
])
def test_a_day_is_a_utc_day(now, start, end, label):
    day = budget_period("day", now)
    assert (day["start"].isoformat(), day["end"].isoformat(), day["resets_at_label"]) == (
        start, end, label)
    assert day["cap_name"] == "daily cap" and day["whose"] == "today's"


def test_a_day_in_another_timezone_is_still_the_utc_day():
    from datetime import timedelta
    pacific = timezone(timedelta(hours=-7))
    # 20:00 on the 20th in UTC-7 is 03:00 on the 21st UTC.
    day = budget_period("day", datetime(2031, 5, 20, 20, tzinfo=pacific))
    assert day["start"].isoformat() == "2031-05-21T00:00:00+00:00"


@pytest.mark.parametrize("now,end,label", [
    (datetime(2031, 2, 28, 12, tzinfo=timezone.utc), "2031-03-01T00:00:00+00:00",
     "00:00 UTC on 1 March"),
    (datetime(2031, 12, 1, 0, 0, tzinfo=timezone.utc), "2032-01-01T00:00:00+00:00",
     "00:00 UTC on 1 January"),
])
def test_a_month_is_a_calendar_month(now, end, label):
    month = budget_period("month", now)
    assert month["start"] == now.replace(day=1, hour=0, minute=0)
    assert month["end"].isoformat() == end and month["resets_at_label"] == label
    assert month["cap_name"] == "monthly cap"


def test_a_hold_names_the_period_and_when_it_resets():
    period = {"whose": "today's", "resets_at_label": "00:00 UTC", "cap_name": "daily cap"}
    sentence = budget_hold_sentence("red", 0.95, 10_000_000, period)
    assert sentence == ("Held — the budget is red (95% of today's $10 used), so only P0 work "
                        "runs. It resets at 00:00 UTC, or raise the daily cap in Budget.")
    assert held_headline(sentence) == ("Held — the budget is red (95% of today's $10 used), "
                                       "so only P0 work runs")
    said = explain(f"{sentence} (job discovery.run, priority p2, risk 0; red_halts_non_p0_work)")
    assert said["kind"] == "budget_hold"
    assert said["headline"] == held_headline(sentence) + "."
    assert said["action"].startswith("It resets at 00:00 UTC, or raise the daily cap in Budget.")

    month = {"whose": "this month's", "resets_at_label": "00:00 UTC on 1 June",
             "cap_name": "monthly cap"}
    assert budget_hold_sentence("orange", 0.87, 200_000_000, month).endswith(
        "(87% of this month's $200 used), so only P0 and P1 work runs. It resets at "
        "00:00 UTC on 1 June, or raise the monthly cap in Budget.")
