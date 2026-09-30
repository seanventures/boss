"""Deterministic SRE bench: known surge gets a real basis; missing stays unknown."""
from boss_engine.services.sre_bench import (
    MISSING_CASE_ID,
    SURGE_CASE_ID,
    run_sre_bench,
)


def test_error_surge_returns_calibrated_winner():
    report = run_sre_bench()
    assert report["public_ga"] is False
    assert report["deterministic"] is True
    surge = next(case for case in report["cases"] if case["id"] == SURGE_CASE_ID)
    winner = surge["winner"]
    assert winner["basis"] in {"memory", "change", "context"}
    assert 0.0 <= float(winner["confidence"]) <= 1.0
    assert winner["root_cause"] != "unknown"


def test_missing_signals_never_invent_high_confidence():
    report = run_sre_bench()
    missing = next(case for case in report["cases"] if case["id"] == MISSING_CASE_ID)
    winner = missing["winner"]
    assert winner["root_cause"] == "unknown"
    assert float(winner["confidence"]) < 0.5
    assert float(winner["confidence"]) != 0.99
