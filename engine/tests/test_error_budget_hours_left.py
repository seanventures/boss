"""Hours of error budget left in the 24 hour window.

The allowance is 24 * (100 - target) / 100. An unused budget is not reported as that
allowance, and a missed objective is zero hours left.
"""
from boss_engine.services.project_experience.health import hours_left


def test_full_uptime_is_not_using_the_budget():
    # 99.5 allows 0.12 h. None of it is in use, so that allowance is not the answer.
    assert hours_left(100, 99.5) == {
        "hours_left": None,
        "budget_words": "The budget is not being used.",
    }


def test_a_missed_objective_has_no_hours_left():
    assert hours_left(99.0, 99.5) == {"hours_left": 0, "budget_words": "0 hours left."}


def test_half_the_budget_leaves_six_hundredths_of_an_hour():
    # allowed = 24 * 0.5 / 100 = 0.12; downtime 0.25 of 0.5 is 50%; left = 0.06.
    assert hours_left(99.75, 99.5) == {"hours_left": 0.06, "budget_words": "0.06 hours left."}


def test_unknown_uptime_or_target_says_nothing():
    assert hours_left(None, 99.5) == {"hours_left": None, "budget_words": None}
    assert hours_left(99.75, None) == {"hours_left": None, "budget_words": None}
