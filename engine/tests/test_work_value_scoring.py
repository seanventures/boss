"""Pure unit tests for BOSS-353 value-density scoring (no database)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from boss_engine.services.work_value_scoring import (
    SCORE_WEIGHTS,
    compute_priority_score,
    compute_value_density,
    priority_band,
    rank_by_value_density,
    score_preview,
)


def test_score_weights_sum_to_one():
    total = sum(SCORE_WEIGHTS.values(), Decimal("0"))
    assert total == Decimal("1.00")


def test_compute_priority_score_is_confidence_weighted_and_bounded():
    high = compute_priority_score(
        strategic_value=100,
        customer_impact=100,
        urgency=100,
        cost_of_delay=100,
        risk_reduction=100,
        dependency_pressure=100,
        confidence=100,
        manual_adjustment=0,
    )
    low_conf = compute_priority_score(
        strategic_value=100,
        customer_impact=100,
        urgency=100,
        cost_of_delay=100,
        risk_reduction=100,
        dependency_pressure=100,
        confidence=0,
        manual_adjustment=0,
    )
    speculative = compute_priority_score(
        strategic_value=40,
        customer_impact=30,
        urgency=20,
        cost_of_delay=10,
        risk_reduction=10,
        dependency_pressure=10,
        confidence=40,
        manual_adjustment=0,
    )

    assert high == Decimal("100.00")
    assert low_conf == Decimal("65.00")
    assert high > speculative
    assert Decimal("0") <= speculative <= Decimal("100")
    assert priority_band(high) == "p0"
    assert priority_band(speculative) in {"p2", "p3", "p4"}


def test_manual_adjustment_clamps_and_shifts_score():
    base = compute_priority_score(
        strategic_value=50,
        customer_impact=50,
        urgency=50,
        cost_of_delay=50,
        risk_reduction=50,
        dependency_pressure=50,
        confidence=100,
        manual_adjustment=0,
    )
    boosted = compute_priority_score(
        strategic_value=50,
        customer_impact=50,
        urgency=50,
        cost_of_delay=50,
        risk_reduction=50,
        dependency_pressure=50,
        confidence=100,
        manual_adjustment=20,
    )
    capped = compute_priority_score(
        strategic_value=100,
        customer_impact=100,
        urgency=100,
        cost_of_delay=100,
        risk_reduction=100,
        dependency_pressure=100,
        confidence=100,
        manual_adjustment=50,
    )
    assert boosted == base + Decimal("20.00")
    assert capped == Decimal("100.00")


def test_value_density_is_priority_per_usd():
    density = compute_value_density(Decimal("80.00"), 2_000_000)
    assert density["priority_score"] == 80.0
    assert density["estimated_cost_usd_micros"] == 2_000_000
    assert density["cost_floor_applied"] is False
    assert density["value_density"] == pytest.approx(40.0)
    assert density["value_per_cost"] == density["value_density"]
    assert density["value_per_cost_micros"] == 40


def test_zero_cost_uses_one_micro_floor_for_finite_density():
    density = compute_value_density(50, 0)
    assert density["cost_floor_applied"] is True
    assert density["cost_divisor_usd_micros"] == 1
    assert density["value_density"] == pytest.approx(50_000_000.0)
    assert density["value_per_cost_micros"] == 50_000_000


def test_negative_cost_fails_closed():
    with pytest.raises(ValueError, match="estimated_cost_usd_micros"):
        compute_value_density(10, -1)


def test_rank_by_value_density_prefers_higher_value_per_dollar():
    ranked = rank_by_value_density(
        [
            {
                "id": "expensive-high",
                "priority_score": 90,
                "estimated_cost_usd_micros": 9_000_000,
            },
            {
                "id": "cheap-mid",
                "priority_score": 40,
                "estimated_cost_usd_micros": 1_000_000,
            },
            {
                "id": "free-low",
                "priority_score": 10,
                "estimated_cost_usd_micros": 0,
            },
        ]
    )
    assert [row["id"] for row in ranked] == [
        "free-low",
        "cheap-mid",
        "expensive-high",
    ]
    assert ranked[1]["value_density"] > ranked[2]["value_density"]


def test_score_preview_matches_helpers():
    preview = score_preview(
        strategic_value=90,
        customer_impact=80,
        urgency=70,
        cost_of_delay=60,
        risk_reduction=50,
        dependency_pressure=40,
        confidence=90,
        manual_adjustment=0,
        estimated_cost_usd_micros=5_000_000,
    )
    expected_score = compute_priority_score(
        strategic_value=90,
        customer_impact=80,
        urgency=70,
        cost_of_delay=60,
        risk_reduction=50,
        dependency_pressure=40,
        confidence=90,
        manual_adjustment=0,
    )
    expected_density = compute_value_density(expected_score, 5_000_000)
    assert preview["priority_score"] == float(expected_score)
    assert preview["priority_band"] == priority_band(expected_score)
    assert preview["value_density"] == expected_density["value_density"]
    assert preview["value_per_cost"] == expected_density["value_per_cost"]
    assert "weights" in preview
    assert "formula" in preview
