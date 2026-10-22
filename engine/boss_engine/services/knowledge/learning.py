"""Outcome attribution, decay, and promotion candidates.

Weight is a Bayesian-smoothed usefulness with a 180-day half-life. Unused
low-weight facts are archived. They stay queryable and are not injected.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

PRIOR = 2.0
HALF_LIFE_DAYS = 180.0
ARCHIVE_AFTER_DAYS = 180
ARCHIVE_BELOW = 0.4
PROMOTION_SUCCESSES = 3


def usefulness(successes: int, failures: int) -> float:
    return (successes + 1.0) / (successes + failures + PRIOR)


def decay(weight: float, days_unused: float) -> float:
    if days_unused <= 0:
        return weight
    return weight * (0.5 ** (days_unused / HALF_LIFE_DAYS))


def days_between(later: datetime, earlier: datetime | None) -> float:
    if earlier is None:
        return 0.0
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    return max(0.0, (later - earlier).total_seconds() / 86400.0)


def should_archive(weight: float, days_unused: float) -> bool:
    return days_unused >= ARCHIVE_AFTER_DAYS and weight < ARCHIVE_BELOW


def promotion_candidate(*, trust_tier: str, successes: int, failures: int) -> bool:
    return trust_tier == "inferred" and successes >= PROMOTION_SUCCESSES and failures == 0


def apply_learning(stat: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    base = usefulness(int(stat.get("success_count") or 0), int(stat.get("failure_count") or 0))
    unused = days_between(now, stat.get("last_used_at"))
    weight = decay(base, unused)
    return {
        "weight": weight,
        "days_unused": unused,
        "archived": should_archive(weight, unused),
        "promotion_candidate": promotion_candidate(
            trust_tier=str(stat.get("trust_tier") or ""),
            successes=int(stat.get("success_count") or 0),
            failures=int(stat.get("failure_count") or 0),
        ),
    }


def score_is_finite(weight: float) -> bool:
    return math.isfinite(weight)
