"""A budget-cap refusal names what was asked (brec_bd2a474ed4eb4537a373): a $10 self-fix in a
project with a $10 daily cap was refused "today's budget is 0% used ($0.000012 of $10)" —
true of the period, and no reason at all. The sentence now says what the reservation needed
against what was left, or that one reservation is bigger than the whole cap."""
from __future__ import annotations

from boss_engine.services.model_failures import cap_refusal_sentence

TODAY = {"whose": "today's", "cap_usd_micros": 10_000_000, "committed_usd_micros": 12,
         "utilization_ratio": 0.0000012}


def test_a_reservation_bigger_than_what_is_left_says_both():
    said = cap_refusal_sentence(TODAY, 10_000_000)
    assert said == ("this needs $10 reserved and today's budget has $9.99 left "
                    "($0.000012 of $10 used)")
    assert "0% used" not in said


def test_a_reservation_bigger_than_the_whole_cap_says_so():
    assert cap_refusal_sentence(TODAY, 12_000_000) == (
        "this needs $12 reserved, more than today's whole budget of $10")


def test_a_used_up_period_keeps_its_words():
    spent = {**TODAY, "committed_usd_micros": 10_200_000, "utilization_ratio": 1.02}
    assert cap_refusal_sentence(spent, 2_000_000) == "today's budget is used ($10.20 of $10)"


def test_the_reserve_refusal_uses_it():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "boss_engine/services/budget.py").read_text()
    assert "cap_refusal_sentence(refusal, amount_usd_micros)" in src
