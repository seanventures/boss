"""VAG #109 — budget-variable repair depth + implementer-tier escalation.

VAG Phase 3 gave the pipeline a repair loop: on a failed gate, feed the concrete evidence back and
retry, up to a **fixed** `code_repair_max_retries`. A fixed count is wrong in both directions — it
burns budget on a doomed cheap model, and it caps a task that a little more depth would finish.

This makes repair a **budget-aware policy**:

  - **Budget-variable depth** — the number of attempts is what the *remaining* budget affords, not a
    constant. Ample budget → go deeper; tight budget → stop before overspending.
  - **Escalate the tier on poor $/verified** — if the cheap implementer has burned N attempts and
    verified *nothing*, that's an infinite $/verified. Switching to a stronger (pricier) model is
    counter-intuitively *cheaper per verified outcome*: one $0.20 call that passes beats ten $0.02
    calls that don't. So after `escalate_after` cheap failures, escalate — if the budget affords it.

Pure and model-free: it decides *what to do next*, the pipeline executes it.
"""
from __future__ import annotations

from typing import Any, Optional


def affordable_attempts(remaining_usd_micros: int, est_attempt_cost: int) -> int:
    """How many attempts the remaining budget can pay for at the estimated per-attempt cost."""
    if est_attempt_cost <= 0:
        return 0
    return max(0, remaining_usd_micros // est_attempt_cost)


def budget_variable_depth(
    remaining_usd_micros: int, est_attempt_cost: int, max_retries: int
) -> int:
    """Repair depth = min(what the budget affords, the hard retry cap). Replaces the fixed count."""
    return max(0, min(max_retries, affordable_attempts(remaining_usd_micros, est_attempt_cost)))


def dollars_per_verified(spent_usd_micros: int, verified_count: int) -> Optional[float]:
    """$/verified-outcome in micros. None when nothing has verified yet (an *infinite* cost — which
    is exactly the signal that the current tier isn't earning its spend)."""
    if verified_count <= 0:
        return None
    return round(spent_usd_micros / verified_count, 1)


def decide_repair(
    *, attempt: int, spent_usd_micros: int, budget_usd_micros: int,
    est_attempt_cost: int, max_retries: int, escalate_after: int,
    cheap_tier: str, strong_tier: str, strong_attempt_cost: Optional[int] = None,
    last_gate_passed: bool = False,
) -> dict[str, Any]:
    """Decide the next repair action after a gate result.

    ``attempt`` is the number of repair attempts ALREADY made (0 = about to make the first). Returns
    ``{action: 'stop'|'retry', tier?, escalated?, reason, remaining_after?}``.
    """
    remaining = max(0, budget_usd_micros - spent_usd_micros)
    if last_gate_passed:
        return {"action": "stop", "reason": "gate passed — verified"}
    if attempt >= max_retries:
        return {"action": "stop", "reason": f"repair depth exhausted ({max_retries} retries)"}

    # Escalate once the cheap tier has failed `escalate_after` times with nothing verified.
    escalated = attempt >= escalate_after and strong_tier and strong_tier != cheap_tier
    tier = strong_tier if escalated else cheap_tier
    cost = (strong_attempt_cost or est_attempt_cost) if escalated else est_attempt_cost

    if remaining < cost:
        return {"action": "stop", "tier": tier,
                "reason": f"budget can't afford the next attempt (remaining {remaining} < {cost})"}
    return {
        "action": "retry", "tier": tier, "escalated": bool(escalated),
        "reason": (f"escalate to strong tier '{strong_tier}' after {escalate_after} cheap failures "
                   f"(poor $/verified)") if escalated else f"retry on cheap tier '{cheap_tier}'",
        "remaining_after": remaining - cost,
    }


def plan_repair(
    *, remaining_usd_micros: int, est_attempt_cost: int, max_retries: int,
    escalate_after: int, cheap_tier: str, strong_tier: str,
) -> dict[str, Any]:
    """A human-readable plan up front: how deep the budget allows, and when the tier flips — useful
    for the console / run evidence before the loop runs."""
    depth = budget_variable_depth(remaining_usd_micros, est_attempt_cost, max_retries)
    return {
        "max_depth": depth,
        "affordable_attempts": affordable_attempts(remaining_usd_micros, est_attempt_cost),
        "retry_cap": max_retries,
        "escalate_after": escalate_after,
        "tiers": {"cheap": cheap_tier, "strong": strong_tier},
        "note": (f"up to {depth} repair attempt(s) on '{cheap_tier}', escalating to '{strong_tier}' "
                 f"after {escalate_after} failure(s)"),
    }
