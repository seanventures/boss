"""Money, and the value a subscription already paid for — kept apart.

WHY THIS EXISTS
---------------
boss-1 (2026-10-03) runs on a Claude Code subscription and a Codex subscription: flat-rate,
no API key, no per-call money at all. Its $10 project budget still went from $2.97 to $9.08
"actual" in a day of test runs, turned red, and the job gate then held every non-P0 job —
three research runs sat queued while their page said no worker had picked them up.

The money was never spent. The Claude CLI reports `total_cost_usd` on every call — what the
call WOULD have cost at API list price, subscription or not — and `estimate_cost_usd_micros`
honours that figure; Codex reports tokens, which the price table turns into list price. Every
settlement path then booked that figure as `actual_usd_micros` on a cost receipt, and the money
caps (daily, monthly, allocation, burst) summed it. The top cost driver was the route's model
id, `anthropic:claude-sonnet`, on the `anthropic_claude_code` subscription source.

THE RULE
--------
Money caps govern MONEY. A call answered by a subscription, a licensed seat or a local model
settles at actual cost $0. What it would have cost at list price is still recorded — on the
cost receipt as `equivalent_usd_micros` with `included_in_subscription = true`, on `llm_calls`
the same way — and reported as "included in your subscription", never dropped. Subscription
CAPACITY (entitlements, usage windows, concurrent leases) is governed exactly as before; this is
only about which figure is money.

Two ways to know, both decided here so no caller decides for itself:

  * the SOURCE — an `entitlement_sources` row (the model gateway's lease names it):
    `source_is_included`;
  * the USAGE an adapter reports — the CLI and local adapters stamp `billing` on it:
    `usage_is_included`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

#: `usage["billing"]` values. An adapter that answers on flat-rate or local capacity says so;
#: anything else (or nothing) is metered money.
BILLING_SUBSCRIPTION = "subscription"
BILLING_LOCAL = "local"
BILLING_METERED = "metered"
INCLUDED_BILLING = frozenset({BILLING_SUBSCRIPTION, BILLING_LOCAL})

#: `entitlement_sources.entitlement_kind` values whose calls cost no per-call money.
#: `api_key_capacity` (a coding agent on an API key) and every usage-based kind are money.
INCLUDED_ENTITLEMENTS = frozenset({"subscription", "licensed_seat", "local_capacity"})

#: The SQL a money total sums over `cost_receipts`. A receipt included in a subscription is
#: $0 of money whatever its amount columns say — that also covers receipts written before
#: this rule existed and later classified (db/schema.sql backfill), whose amounts are kept.
MONEY_SQL = ("CASE WHEN COALESCE(included_in_subscription, false) THEN 0 "
             "ELSE COALESCE(actual_usd_micros, estimated_usd_micros) END")

#: The API-equivalent value of the included receipts, for "included in your subscription".
INCLUDED_SQL = ("CASE WHEN COALESCE(included_in_subscription, false) "
                "THEN COALESCE(equivalent_usd_micros, actual_usd_micros, estimated_usd_micros, 0) "
                "ELSE 0 END")


def source_is_included(source: Optional[Mapping[str, Any]]) -> bool:
    """Whether calls on this entitlement source are flat-rate or local (no per-call money)."""
    if not source:
        return False
    kind = str(source.get("entitlement_kind") or source.get("cost_mode") or "").strip().lower()
    return kind in INCLUDED_ENTITLEMENTS


def usage_is_included(usage: Optional[Mapping[str, Any]]) -> bool:
    """Whether the adapter that answered said the call ran on included capacity."""
    if not isinstance(usage, Mapping):
        return False
    return str(usage.get("billing") or "").strip().lower() in INCLUDED_BILLING


def split(amount_usd_micros: Optional[int], included: bool) -> tuple[Optional[int], Optional[int]]:
    """(money, equivalent) for a call worth `amount_usd_micros` at list price."""
    if not included:
        return amount_usd_micros, None
    return 0, int(amount_usd_micros or 0)


def source_by_id(connection: Any, project_id: str, source_id: Optional[str]) -> Optional[dict]:
    """The entitlement source a pinned call will run on, or None. Read-only; never raises."""
    if not source_id or connection is None:
        return None
    try:
        from sqlalchemy import text

        with connection.begin_nested():
            row = connection.execute(
                text("SELECT entitlement_kind, cost_mode, source_kind FROM entitlement_sources "
                     "WHERE project_id = :p AND source_id = :s"),
                {"p": project_id, "s": source_id},
            ).mappings().first()
    except Exception:  # noqa: BLE001 — unknown means "treat as money", the safe side
        return None
    return dict(row) if row else None


def reservation_estimate(connection: Any, project_id: str, source_id: Optional[str],
                         estimate_usd_micros: int) -> int:
    """What a call pinned to `source_id` reserves: $0 on included capacity, else the estimate.

    The reservation becomes a GATE rather than a hold (still refused once the cap is spent), the
    same as `model_routing.turn_estimate_usd_micros` does for a subscription engine. An unpinned
    call keeps its estimate: the router has not chosen yet, and over-reserving is the safe side.
    """
    if source_is_included(source_by_id(connection, project_id, source_id)):
        return 0
    return int(estimate_usd_micros)
