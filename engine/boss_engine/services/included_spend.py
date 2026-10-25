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


def call_is_included(engine: Optional[str], usage: Optional[Mapping[str, Any]] = None) -> bool:
    """THE ONE DECISION: did this model call run on included capacity (a subscription or a local
    model), so its cost is an estimate and not money?

    Every recording site (llm_telemetry.record_llm_call, the model gateway, the router's turn
    settlement) asks this, so one call cannot be stamped two ways. The adapter's `billing`
    stamp — the credential it actually ran on — wins. Without one, the engine's class decides
    (a subscription CLI or a local model; model_routing.UNMETERED). An adapter that knows its
    credential stamps it on EVERY row it records, failed calls included: a failed Claude call on
    an API key carried no stamp, fell to the engine rule, and read as a subscription call in the
    middle of a metered run (brec_85de2541)."""
    if isinstance(usage, Mapping) and usage.get("billing"):
        return usage_is_included(usage)
    from .model_routing import UNMETERED

    name = str(engine or "").strip().lower()
    if name.startswith("guest:"):
        name = name.split(":", 1)[1]
    return name in UNMETERED


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


def routed_reservation_estimate(connection: Any, settings: Any, project_id: str,
                                estimate_usd_micros: int) -> int:
    """What model work on this project's ROUTED model reserves: its marginal cost.

    Each PRD meeting reserved $6.00 for up to two hours, and active reservations count toward
    the daily cap (budget.period_spend) — so on a $10 cap two meetings, or a meeting and a build,
    turned the budget red and held work, while every call they made ran on a Claude subscription
    and settled at $0 (`included_in_subscription`). Sized at the marginal cost, included capacity
    reserves $0: the reservation is a GATE (still refused once the cap's real money is spent),
    exactly as `reservation_estimate` and `model_routing.turn_estimate_usd_micros` already do.
    A metered provider keeps the full estimate, so the caps stay meaningful where money is.

    Decided by the same rule the meeting's calls SETTLE by (model_routing.call_is_included), so a
    reservation can never call free what the receipt will call money. Unknown is money.
    """
    from .model_routing import call_is_included

    nested = None
    try:
        if hasattr(connection, "begin_nested"):
            nested = connection.begin_nested()
        included = call_is_included(connection, settings, project_id, None)
        if nested is not None:
            nested.commit()  # raises if a lookup inside failed: rolled back below
    except Exception:  # noqa: BLE001 — a lookup failure reserves the full estimate
        included = False
        try:
            if nested is not None and nested.is_active:
                nested.rollback()
        except Exception:  # noqa: BLE001
            pass
    return 0 if included else int(estimate_usd_micros)


# ── MONEY SPENT, ONE DEFINITION FOR GOVERN AND THE COST TOOLS ─────────────────────────────────
#
# Two ledgers each hold part of the truth. `cost_receipts` is what every cap counts (settled
# money per reservation). `llm_calls` is one row per model call — what get_costs and Spend by
# agent/model read. On Community the code-task pipeline's budget port is unmetered
# (defaults.NoopBudgetPort), so a build's calls are in `llm_calls` and never settle a receipt:
# Govern said $8.47 spent while the builds alone had spent $284 (brec_85de2541). `ledger_spend`
# is the one answer: the receipts' money plus every build's money no receipt settled
# (`not_under_cap`), with what subscriptions ran (an estimate at list price, never money) beside
# it. Govern's meter and get_costs both read it, so they cannot disagree.

#: Money on a llm_calls row: its cost, unless a subscription or local model answered (then $0).
LLM_MONEY_SQL = ("CASE WHEN COALESCE(included_in_subscription, false) THEN 0 "
                 "ELSE COALESCE(cost_usd_micros, 0) END")
#: What an included row would have cost at list price — an estimate, never money.
LLM_INCLUDED_SQL = ("CASE WHEN COALESCE(included_in_subscription, false) "
                    "THEN COALESCE(cost_usd_micros, 0) ELSE 0 END")


def ledger_spend(connection: Any, project_id: str, start: Any, end: Any = None) -> dict[str, int]:
    """Money spent over [start, end) and how it splits:

      money_usd_micros         = under_cap + not_under_cap (the figure to call "spent")
      under_cap_usd_micros     — settled in cost_receipts: what the caps count
      not_under_cap_usd_micros — build money in llm_calls that no receipt settled
      included_usd_micros      — subscription / local calls at list price (receipted ones, and
                                 builds' from llm_calls): an estimate, not money, in no total
      metered_calls / included_calls — llm_calls rows by credential kind (API key vs plan)

    Read-only; never raises (zeros when the tables are missing)."""
    from sqlalchemy import text

    params = {"p": project_id, "start": start, "end": end}
    window = "project_id = :p AND created_at >= :start" + (
        " AND created_at < :end" if end is not None else "")
    zero = {"money_usd_micros": 0, "under_cap_usd_micros": 0, "not_under_cap_usd_micros": 0,
            "included_usd_micros": 0, "metered_calls": 0, "included_calls": 0}
    try:
        with connection.begin_nested():
            calls = connection.execute(text(
                # A build's subscription calls settle no receipt in any edition; every other
                # included call is receipted (its estimate below, from cost_receipts).
                f"SELECT COALESCE(SUM({LLM_INCLUDED_SQL}) FILTER "
                "          (WHERE code_task_run_id IS NOT NULL), 0) AS included, "
                "        COUNT(*) FILTER (WHERE NOT COALESCE(included_in_subscription, false)) "
                "          AS metered_calls, "
                "        COUNT(*) FILTER (WHERE COALESCE(included_in_subscription, false)) "
                "          AS included_calls "
                f"FROM llm_calls WHERE {window}"), params).mappings().one()
            receipts = connection.execute(text(
                f"SELECT COALESCE(SUM({MONEY_SQL}), 0) AS money, "
                f"       COALESCE(SUM({INCLUDED_SQL}), 0) AS included "
                f"FROM cost_receipts WHERE {window}"), params).mappings().one()
            uncapped = connection.execute(text(
                f"WITH runs AS (SELECT code_task_run_id AS r, SUM({LLM_MONEY_SQL}) AS m "
                f"              FROM llm_calls WHERE {window} AND code_task_run_id IS NOT NULL "
                "               GROUP BY 1), "
                "     settled AS (SELECT substr(skill_or_model_id, 11) AS r, "
                "                        SUM(COALESCE(actual_usd_micros, estimated_usd_micros, 0))"
                "                          AS s "
                "                 FROM cost_receipts WHERE project_id = :p "
                "                  AND skill_or_model_id LIKE 'code_task:%' GROUP BY 1) "
                "SELECT COALESCE(SUM(GREATEST(runs.m - COALESCE(settled.s, 0), 0)), 0) "
                "FROM runs LEFT JOIN settled ON settled.r = runs.r"), params).scalar()
    except Exception:  # noqa: BLE001 — a figure beside the meter must not take the meter down
        return zero
    under, outside = int(receipts["money"] or 0), int(uncapped or 0)
    return {"money_usd_micros": under + outside, "under_cap_usd_micros": under,
            "not_under_cap_usd_micros": outside,
            "included_usd_micros": int(receipts["included"] or 0) + int(calls["included"] or 0),
            "metered_calls": int(calls["metered_calls"] or 0),
            "included_calls": int(calls["included_calls"] or 0)}
