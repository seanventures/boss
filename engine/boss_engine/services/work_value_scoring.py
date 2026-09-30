"""Deterministic value-density / priority scoring for work admission (BOSS-353).

Pure math only — no DB, no side effects, no receipts. Fail closed on invalid input.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Optional, Sequence

# Weighted portfolio dimensions (must sum to 1.00).
SCORE_WEIGHTS: dict[str, Decimal] = {
    "strategic_value": Decimal("0.25"),
    "customer_impact": Decimal("0.22"),
    "urgency": Decimal("0.18"),
    "cost_of_delay": Decimal("0.15"),
    "risk_reduction": Decimal("0.10"),
    "dependency_pressure": Decimal("0.10"),
}

# Confidence scales the weighted raw score into [0.65, 1.00] of raw.
CONFIDENCE_BASE = Decimal("0.65")
CONFIDENCE_SPAN = Decimal("0.35")

# Zero estimated cost uses a 1 micro-USD floor so density is finite and free
# work ranks above any positive-cost work with the same priority_score.
MIN_COST_USD_MICROS_FOR_DENSITY = 1

MICROS_PER_USD = Decimal("1000000")


def clamp_score_dimension(value: Any) -> int:
    """Bound a scoring dimension to the closed integer interval [0, 100]."""
    return max(0, min(100, int(value)))


def clamp_manual_adjustment(value: Any) -> int:
    """Bound a manual score adjustment to [-100, 100]."""
    return max(-100, min(100, int(value)))


def compute_priority_score(
    *,
    strategic_value: int,
    customer_impact: int,
    urgency: int,
    cost_of_delay: int,
    risk_reduction: int,
    dependency_pressure: int,
    confidence: int = 75,
    manual_adjustment: int = 0,
) -> Decimal:
    """Combine portfolio dimensions into an explainable priority score in [0, 100].

    Formula:
      raw = Σ (dimension_i * weight_i)
      confidence_factor = 0.65 + (confidence / 100) * 0.35
      priority_score = clamp(raw * confidence_factor + manual_adjustment, 0, 100)

    Dimensions are clamped to [0, 100] before weighting. Result is quantized
    to two decimal places (ROUND_HALF_UP).
    """
    inputs = {
        "strategic_value": clamp_score_dimension(strategic_value),
        "customer_impact": clamp_score_dimension(customer_impact),
        "urgency": clamp_score_dimension(urgency),
        "cost_of_delay": clamp_score_dimension(cost_of_delay),
        "risk_reduction": clamp_score_dimension(risk_reduction),
        "dependency_pressure": clamp_score_dimension(dependency_pressure),
    }
    confidence_clamped = clamp_score_dimension(confidence)
    manual = clamp_manual_adjustment(manual_adjustment)

    raw = sum(
        (Decimal(str(inputs[field])) * weight for field, weight in SCORE_WEIGHTS.items()),
        Decimal("0"),
    )
    confidence_factor = CONFIDENCE_BASE + (
        Decimal(str(confidence_clamped)) / Decimal("100") * CONFIDENCE_SPAN
    )
    adjusted = raw * confidence_factor + Decimal(str(manual))
    bounded = max(Decimal("0"), min(Decimal("100"), adjusted))
    return bounded.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_value_density(
    priority_score: Decimal | float | int,
    estimated_cost_usd_micros: int,
) -> dict[str, Any]:
    """Compute value-per-cost (value density) from priority score and estimated cost.

    Definitions:
      cost_usd = max(estimated_cost_usd_micros, 1) / 1_000_000
      value_density = priority_score / cost_usd
                    = priority_score * 1_000_000 / max(cost_micros, 1)
      value_per_cost = value_density  (API alias)
      value_per_cost_micros = round(value_density) as int ranking key
        (points-per-USD rounded to nearest integer; free work uses 1µ$ floor)

    Zero cost is not an error: free work gets maximum density for its score
    via the 1 micro-USD floor (cost_floor_applied=true).
    """
    if int(estimated_cost_usd_micros) < 0:
        raise ValueError("estimated_cost_usd_micros must be >= 0.")

    score = Decimal(str(priority_score)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    cost_micros = int(estimated_cost_usd_micros)
    cost_floor_applied = cost_micros == 0
    divisor = max(cost_micros, MIN_COST_USD_MICROS_FOR_DENSITY)

    # Points per USD, six decimal places.
    density = (score * MICROS_PER_USD / Decimal(str(divisor))).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    # Integer ranking key: points-per-USD rounded half-up (stable, comparable).
    value_per_cost_micros = int(
        density.to_integral_value(rounding=ROUND_HALF_UP)
    )

    return {
        "priority_score": float(score),
        "estimated_cost_usd_micros": cost_micros,
        "cost_floor_applied": cost_floor_applied,
        "cost_divisor_usd_micros": divisor,
        "value_density": float(density),
        "value_per_cost": float(density),
        "value_per_cost_micros": value_per_cost_micros,
    }


def score_preview(
    *,
    strategic_value: int = 50,
    customer_impact: int = 50,
    urgency: int = 50,
    cost_of_delay: int = 50,
    risk_reduction: int = 50,
    dependency_pressure: int = 50,
    confidence: int = 75,
    manual_adjustment: int = 0,
    estimated_cost_usd_micros: int = 0,
) -> dict[str, Any]:
    """Pure score preview: priority band + value density, no persistence."""
    dimensions = {
        "strategic_value": clamp_score_dimension(strategic_value),
        "customer_impact": clamp_score_dimension(customer_impact),
        "urgency": clamp_score_dimension(urgency),
        "cost_of_delay": clamp_score_dimension(cost_of_delay),
        "risk_reduction": clamp_score_dimension(risk_reduction),
        "dependency_pressure": clamp_score_dimension(dependency_pressure),
        "confidence": clamp_score_dimension(confidence),
        "manual_adjustment": clamp_manual_adjustment(manual_adjustment),
    }
    priority_score = compute_priority_score(**dimensions)
    density = compute_value_density(priority_score, estimated_cost_usd_micros)
    band = priority_band(priority_score)
    weights = {key: float(weight) for key, weight in SCORE_WEIGHTS.items()}
    return {
        **dimensions,
        **density,
        "priority_band": band,
        "weights": weights,
        "formula": {
            "priority_score": (
                "clamp(sum(dimension_i * weight_i) * "
                "(0.65 + confidence/100*0.35) + manual_adjustment, 0, 100)"
            ),
            "value_density": (
                "priority_score * 1_000_000 / max(estimated_cost_usd_micros, 1)"
            ),
        },
    }


def priority_band(score: Decimal | float | int) -> str:
    """Map a priority score to P0..P4."""
    value = float(score)
    if value >= 85:
        return "p0"
    if value >= 70:
        return "p1"
    if value >= 50:
        return "p2"
    if value >= 30:
        return "p3"
    return "p4"


def rank_by_value_density(
    candidates: Sequence[Mapping[str, Any]],
    *,
    score_key: str = "priority_score",
    cost_key: str = "estimated_cost_usd_micros",
    id_key: str = "id",
) -> list[dict[str, Any]]:
    """Order candidates by value density DESC, then priority, then lower cost, then id.

    Each candidate must provide a priority/effective score and an estimated cost
    in USD micros. Missing score falls back to ``effective_score``. Missing cost
    is treated as 0 (free → max density for score). Does not mutate inputs.
    """
    enriched: list[dict[str, Any]] = []
    for index, raw in enumerate(candidates):
        item = dict(raw)
        score_raw = item.get(score_key)
        if score_raw is None:
            score_raw = item.get("effective_score", 0)
        cost_raw = item.get(cost_key, 0)
        if cost_raw is None:
            cost_raw = 0
        density = compute_value_density(score_raw, int(cost_raw))
        item["priority_score"] = density["priority_score"]
        item["value_density"] = density["value_density"]
        item["value_per_cost"] = density["value_per_cost"]
        item["value_per_cost_micros"] = density["value_per_cost_micros"]
        item["estimated_cost_usd_micros"] = density["estimated_cost_usd_micros"]
        item["cost_floor_applied"] = density["cost_floor_applied"]
        item["_rank_index"] = index
        enriched.append(item)

    enriched.sort(
        key=lambda row: (
            -float(row["value_density"]),
            -float(row["priority_score"]),
            int(row["estimated_cost_usd_micros"]),
            str(row.get(id_key) or row.get("_rank_index")),
        )
    )
    for row in enriched:
        row.pop("_rank_index", None)
    return enriched


def annotate_work_item_scores(
    work_item: Mapping[str, Any],
    *,
    estimated_cost_usd_micros: Optional[int] = None,
) -> dict[str, Any]:
    """Add priority_score (and optional value density) onto a work-item dict."""
    data = dict(work_item)
    effective = data.get("effective_score")
    if effective is not None:
        data["priority_score"] = float(effective)
    elif data.get("priority_score") is not None:
        data["priority_score"] = float(data["priority_score"])
    if estimated_cost_usd_micros is not None:
        score = data.get("priority_score")
        if score is None and effective is not None:
            score = effective
        if score is not None:
            density = compute_value_density(score, int(estimated_cost_usd_micros))
            data.update(
                {
                    "value_density": density["value_density"],
                    "value_per_cost": density["value_per_cost"],
                    "value_per_cost_micros": density["value_per_cost_micros"],
                    "estimated_cost_usd_micros": density[
                        "estimated_cost_usd_micros"
                    ],
                    "cost_floor_applied": density["cost_floor_applied"],
                }
            )
    return data
