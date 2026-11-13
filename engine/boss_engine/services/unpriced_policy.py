"""BLOCK UNPRICED USAGE (BOSSAPP-869): an install whose finance rule is "no spend we cannot price".

WHAT BOSS DID, AND STILL DOES BY DEFAULT
----------------------------------------
A model call BOSS has no price for is charged at the DEAREST known rate and labelled: the
ledger row's ``llm_calls.price_estimated`` is true (llm_telemetry.record_llm_call), a connection
proof's basis is ``unpriced`` (connection_spend.cost_evidence), and the architecture step refuses
a paid reply with no usable cost evidence at all (architecture_spend.settled_text_cost). The bill
never runs short, but the money is a guess.

WHAT THIS ADDS
--------------
One project setting, ``budget_unpriced_policy``:

  estimate  (the default) — exactly the behaviour above; nothing here is read.
  block     — once this project's UNPRICED money today (UTC; price-estimated, non-included
              ``llm_calls`` rows) is past ``budget_unpriced_allowance_usd_micros`` (default 0),
              BudgetGovernor.reserve refuses new paid work and opens the existing
              "increase_budget_or_defer" decision, so a person prices the model
              (BOSS_MODEL_PRICES_JSON), raises the allowance, or lets the work wait for tomorrow.

$0 work (a subscription, a local model, a reservation of 0) is never held: it cannot be unpriced
money. While one such decision is still open, later refusals land on it instead of opening more
(create_blocking_decision merges a repeat of the same question).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

POLICY_KNOB = "budget_unpriced_policy"
ALLOWANCE_KNOB = "budget_unpriced_allowance_usd_micros"
CHOICES = ("estimate", "block")
DEFAULT_POLICY = "estimate"
REASON = "budget_unpriced"
REFUSAL = "unpriced_blocked"
QUESTION = ("Unpriced model use is past this project's allowance: price the model, allow more, "
            "or wait until tomorrow?")
#: The highest allowance a person may set: $1,000,000 a day.
MAX_ALLOWANCE_USD_MICROS = 1_000_000 * 1_000_000


def _own(connection: Any, project_id: str, key: str) -> Any:
    try:
        with connection.begin_nested():
            return connection.execute(
                text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
                {"p": project_id, "k": key}).scalar()
    except Exception:  # noqa: BLE001 — a missing table reads as the default, never an outage
        return None


def policy(connection: Any, settings: Any, project_id: str) -> str:
    """This project's choice, else the install's (BOSS_BUDGET_UNPRICED_POLICY), else estimate."""
    value = _own(connection, project_id, POLICY_KNOB)
    if value in (None, ""):
        value = getattr(settings, POLICY_KNOB, DEFAULT_POLICY)
    word = str(value or "").strip().strip('"').lower()
    return word if word in CHOICES else DEFAULT_POLICY


def allowance(connection: Any, settings: Any, project_id: str) -> int:
    value = _own(connection, project_id, ALLOWANCE_KNOB)
    if value in (None, ""):
        value = getattr(settings, ALLOWANCE_KNOB, 0)
    try:
        return max(0, min(int(str(value).strip().strip('"')), MAX_ALLOWANCE_USD_MICROS))
    except (TypeError, ValueError):
        return 0


def unpriced_today(connection: Any, project_id: str) -> int:
    """Money spent today (UTC) on calls BOSS had no price for — the ledger's own label."""
    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                "SELECT COALESCE(SUM(cost_usd_micros), 0) FROM llm_calls "
                "WHERE project_id = :p AND price_estimated "
                "AND NOT COALESCE(included_in_subscription, false) "
                "AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"),
                {"p": project_id}).scalar() or 0)
    except Exception:  # noqa: BLE001 — an unreadable ledger reads as nothing unpriced
        return 0


def view(connection: Any, settings: Any, project_id: str) -> dict[str, Any]:
    """What Govern › Limits shows: the choice, the allowance and today's unpriced money."""
    return {"value": policy(connection, settings, project_id), "choices": list(CHOICES),
            "default": DEFAULT_POLICY,
            "allowance_usd_micros": allowance(connection, settings, project_id),
            "unpriced_today_usd_micros": unpriced_today(connection, project_id)}


def refusal(connection: Any, settings: Any, project_id: str, *, category: str, reason: str,
            amount_usd_micros: int, budget_state: Any = None) -> Optional[dict[str, Any]]:
    """The refusal reserve() returns when the policy is block and today's unpriced money is past
    the allowance; None otherwise (always None under estimate, and for $0 work)."""
    if int(amount_usd_micros or 0) <= 0:
        return None
    if policy(connection, settings, project_id) != "block":
        return None
    spent = unpriced_today(connection, project_id)
    allowed = allowance(connection, settings, project_id)
    if spent <= allowed:
        return None
    from .core import create_blocking_decision, emit_event, record_policy_eval

    detail = {"requested_usd_micros": int(amount_usd_micros),
              "unpriced_today_usd_micros": spent, "allowance_usd_micros": allowed,
              "unpriced_policy": "block", "category": category}
    # ONE QUESTION, so create_blocking_decision's one-open-decision-per-cause merges repeats
    # onto the card already open instead of queueing one per refused reservation.
    decision_id = create_blocking_decision(
        connection, project_id, None, QUESTION,
        {"action": "increase_budget_or_defer",
         "recommended": "Pin the model's price in BOSS_MODEL_PRICES_JSON, raise the unpriced "
                        "allowance in Govern › Limits, or let the work wait until tomorrow "
                        "(00:00 UTC).",
         "why_a_person": "This project allows no spend BOSS cannot price beyond its allowance."},
        detail, REASON)
    emit_event(connection, project_id, "budget.unpriced_blocked", "budget_governor",
               "decision", decision_id, {**detail, "reason": reason})
    record_policy_eval(connection, project_id, "budget.reserve", "deny", action=category,
                       reason="unpriced usage past its allowance",
                       detail={**detail, "decision_id": decision_id})
    return {"status": "denied", "refusal": REFUSAL, "transient": False,
            "decision_id": decision_id, "budget_state": budget_state,
            "unpriced_today_usd_micros": spent, "allowance_usd_micros": allowed,
            "message": (f"Unpriced usage blocked: ${spent / 1e6:.2f} today was charged at an "
                        f"estimated rate, past this project's allowance of ${allowed / 1e6:.2f}. "
                        "Price the model or raise the allowance in Govern › Limits; it resets "
                        "at 00:00 UTC. Decision created.")}


__all__ = ["ALLOWANCE_KNOB", "CHOICES", "DEFAULT_POLICY", "POLICY_KNOB", "REASON", "REFUSAL",
           "allowance", "policy", "refusal", "unpriced_today", "view"]
