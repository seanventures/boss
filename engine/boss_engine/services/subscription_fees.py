"""BOSSAPP-864: subscription seat fees, and estimates reconciled to the invoice.

Two gaps made cost-per-PR wrong and kept estimates from ever converging on the bill:

* A subscription engine (Claude Code, Codex on a plan) is tracked as INCLUDED value — its calls are
  $0 of money (included_spend.py) — but the seat fee itself was never recorded anywhere, so a
  team on subscriptions looked free.
* Per-call money is an ESTIMATE (list price x tokens). The provider's invoice is the truth, and
  BOSS could import one only from DigitalOcean.

THE MODEL

* ``subscription_prices`` — a seat fee per provider/plan for a project: seats x monthly price,
  REVISIONED (setting a new price closes the open one at its start; history is kept).
* ``subscription_fee_days`` — the price amortised into one row per day (monthly fee x seats /
  days in that month), written by ``accrue`` up to a day; idempotent per (price, day).
* ``cost_adjustments`` — one row per imported invoice line: the invoice amount, BOSS's estimate
  for the same provider and period (money on llm_calls), the difference, and the reason. Unique
  per (project, provider, invoice, line): re-importing the same invoice changes nothing.
* ``import_invoice`` — a GENERIC importer: a CSV or JSON export from Anthropic, OpenAI or
  OpenRouter (or any provider whose lines have a period/date, an amount and optionally an invoice
  and line id). Columns are matched by name, tolerantly; a line it cannot read is reported, not
  guessed.
* ``report`` — what Spend shows for a period: ESTIMATED (money on llm_calls), RECONCILED (the
  estimate replaced by the invoice for the provider-periods an invoice covers), FEES (seat fees
  accrued), and TOTAL (reconciled + fees).
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id

PROVIDERS = ("anthropic", "openai", "openrouter")

#: Which llm_calls rows are a provider's money (engine or model names, case-insensitive).
_PROVIDER_SQL = {
    "anthropic": ("(l.engine ILIKE '%claude%' OR l.engine ILIKE '%anthropic%' "
                  "OR l.model ILIKE 'anthropic%' OR l.model ILIKE 'claude%')"),
    "openai": ("(l.engine ILIKE '%openai%' OR l.engine ILIKE '%codex%' "
               "OR l.model ILIKE 'openai%' OR l.model ILIKE 'gpt%' OR l.model ILIKE 'o1%' "
               "OR l.model ILIKE 'o3%' OR l.model ILIKE 'o4%')"),
    "openrouter": "(l.engine ILIKE '%openrouter%' OR l.model ILIKE 'openrouter%')",
}


class SubscriptionFeeError(ValueError):
    """Something a person can fix, said for them."""


def _micros(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    raw = str(value).strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        return int((Decimal(raw) * 1_000_000).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def _day(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    raw = str(value).strip()
    if raw.isdigit() and len(raw) >= 9:                       # epoch seconds in a CSV
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).date()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%Y-%m"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ── seat fees ──────────────────────────────────────────────────────────────────────────────

def set_price(connection: Any, project_id: str, *, provider: str, plan: str,
              monthly_usd_micros: int, seats: int = 1, effective_from: Optional[date] = None,
              actor: str) -> dict[str, Any]:
    """A new price for this provider/plan from `effective_from` (default today). The open price
    is closed the day before; history is kept."""
    provider = str(provider or "").strip().lower()
    plan = str(plan or "").strip()
    if not provider or not plan:
        raise SubscriptionFeeError("Name the provider and the plan.")
    if int(monthly_usd_micros) < 0 or int(seats) < 1:
        raise SubscriptionFeeError("A price is $0 or more, for one seat or more.")
    start = effective_from or datetime.now(timezone.utc).date()
    connection.execute(text(
        "UPDATE subscription_prices SET effective_to = :end WHERE project_id = :p AND provider = :pr "
        "AND plan = :pl AND effective_to IS NULL AND effective_from < :start"),
        {"p": project_id, "pr": provider, "pl": plan, "start": start, "end": start - timedelta(days=1)})
    connection.execute(text(
        "DELETE FROM subscription_fee_days WHERE price_id IN (SELECT id FROM subscription_prices "
        "WHERE project_id = :p AND provider = :pr AND plan = :pl) AND day >= :start"),
        {"p": project_id, "pr": provider, "pl": plan, "start": start})
    connection.execute(text(
        "UPDATE subscription_prices SET effective_to = effective_from WHERE project_id = :p "
        "AND provider = :pr AND plan = :pl AND effective_to IS NULL AND effective_from >= :start"),
        {"p": project_id, "pr": provider, "pl": plan, "start": start})
    price_id = make_id("subp")
    connection.execute(text(
        "INSERT INTO subscription_prices (id, project_id, provider, plan, seats, monthly_usd_micros, "
        " effective_from, created_by) VALUES (:id, :p, :pr, :pl, :s, :m, :start, :by)"),
        {"id": price_id, "p": project_id, "pr": provider, "pl": plan, "s": int(seats),
         "m": int(monthly_usd_micros), "start": start, "by": actor})
    return {"id": price_id, "provider": provider, "plan": plan, "seats": int(seats),
            "monthly_usd_micros": int(monthly_usd_micros), "effective_from": start.isoformat()}


def daily_fee(monthly_usd_micros: int, seats: int, day: date) -> int:
    days = calendar.monthrange(day.year, day.month)[1]
    return int(round(int(monthly_usd_micros) * int(seats) / days))


def accrue(connection: Any, project_id: str, *, through: Optional[date] = None) -> int:
    """Write the daily fee rows every price owes up to `through` (default today). Idempotent.
    Returns how many rows were added."""
    through = through or datetime.now(timezone.utc).date()
    prices = connection.execute(text(
        "SELECT id, seats, monthly_usd_micros, effective_from, effective_to FROM subscription_prices "
        "WHERE project_id = :p AND effective_from <= :t"), {"p": project_id, "t": through}).mappings().all()
    added = 0
    for price in prices:
        last = min(through, price["effective_to"] or through)
        day = price["effective_from"]
        rows = []
        while day <= last:
            rows.append({"id": make_id("subf"), "price": price["id"], "p": project_id, "d": day,
                         "a": daily_fee(price["monthly_usd_micros"], price["seats"], day)})
            day += timedelta(days=1)
        if rows:
            added += connection.execute(text(
                "INSERT INTO subscription_fee_days (id, price_id, project_id, day, amount_usd_micros) "
                "VALUES (:id, :price, :p, :d, :a) ON CONFLICT (price_id, day) DO NOTHING"), rows).rowcount or 0
    return added


# ── invoices ──────────────────────────────────────────────────────────────────────────────

_AMOUNT_KEYS = ("amount", "amount_usd", "cost", "cost_usd", "total", "total_usd", "usd",
                "line_total", "amount_value", "value")
_START_KEYS = ("period_start", "start", "start_date", "start_time", "date", "day", "invoice_date",
               "billing_period_start", "usage_date", "created_at")
_END_KEYS = ("period_end", "end", "end_date", "end_time", "billing_period_end")
_INVOICE_KEYS = ("invoice_id", "invoice", "invoice_number", "invoice_uuid", "statement_id")
_LINE_KEYS = ("line_id", "id", "line_item_id", "line", "item_id")
_DESC_KEYS = ("description", "line_item", "product", "model", "name", "item")


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def parse_invoice(content: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Lines (period, amount, invoice, line, description) from a CSV or JSON export, and the
    problems with the lines it could not read. Never guesses an amount or a date."""
    content = (content or "").strip()
    if not content:
        raise SubscriptionFeeError("The invoice file is empty.")
    raw_rows: list[dict[str, Any]]
    if content[:1] in "[{":
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise SubscriptionFeeError(f"Not valid JSON: {exc}") from exc
        if isinstance(data, dict):
            data = data.get("data") or data.get("lines") or data.get("line_items") \
                or data.get("items") or [data]
        raw_rows = [r for r in data if isinstance(r, dict)]
        # OpenAI's costs API shape: buckets with results[{amount:{value}}]
        flat: list[dict[str, Any]] = []
        for r in raw_rows:
            if isinstance(r.get("results"), list):
                for i, res in enumerate(r["results"]):
                    amt = res.get("amount") if isinstance(res, dict) else None
                    flat.append({"start_time": r.get("start_time"), "end_time": r.get("end_time"),
                                 "amount": (amt or {}).get("value") if isinstance(amt, dict) else amt,
                                 "line_id": f"{r.get('start_time')}:{res.get('line_item') or i}",
                                 "description": res.get("line_item") or res.get("project_id")})
            else:
                flat.append(r)
        raw_rows = flat
    else:
        raw_rows = list(csv.DictReader(io.StringIO(content)))
    lines, problems = [], []
    for n, raw in enumerate(raw_rows, start=1):
        row = {_norm(k): v for k, v in raw.items()}
        amount = _micros(_first(row, _AMOUNT_KEYS))
        start = _day(_first(row, _START_KEYS))
        if amount is None or start is None:
            problems.append(f"line {n}: needs a date and an amount")
            continue
        end = _day(_first(row, _END_KEYS)) or start
        lines.append({"period_start": start, "period_end": max(end, start),
                      "amount_usd_micros": amount,
                      "invoice_ref": str(_first(row, _INVOICE_KEYS) or f"{start:%Y-%m}"),
                      "line_ref": str(_first(row, _LINE_KEYS) or f"{n}"),
                      "description": str(_first(row, _DESC_KEYS) or "")[:300]})
    return lines, problems


def estimated(connection: Any, project_id: str, provider: str, start: date, end: date) -> int:
    """BOSS's own money estimate for this provider over [start, end] (days, inclusive)."""
    from .included_spend import LLM_MONEY_SQL

    money = LLM_MONEY_SQL.replace("included_in_subscription", "l.included_in_subscription") \
        .replace("cost_usd_micros", "l.cost_usd_micros")
    return int(connection.execute(text(
        f"SELECT COALESCE(SUM({money}), 0) FROM llm_calls l WHERE l.project_id = :p "
        f"AND {_PROVIDER_SQL[provider]} AND l.created_at >= :s AND l.created_at < :e"),
        {"p": project_id, "s": start, "e": end + timedelta(days=1)}).scalar() or 0)


def import_invoice(connection: Any, project_id: str, *, provider: str, content: str,
                   actor: str) -> dict[str, Any]:
    """Import a provider invoice / cost export: one cost_adjustments row per line (idempotent)."""
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise SubscriptionFeeError(f"provider is one of {', '.join(PROVIDERS)}")
    lines, problems = parse_invoice(content)
    added = 0
    for line in lines:
        est = estimated(connection, project_id, provider, line["period_start"], line["period_end"])
        diff = line["amount_usd_micros"] - est
        reason = ("invoice matches the estimate" if diff == 0 else
                  f"invoice {'above' if diff > 0 else 'below'} the estimate by "
                  f"${abs(diff) / 1e6:,.2f}" + (f" ({line['description']})" if line["description"] else ""))
        added += connection.execute(text(
            "INSERT INTO cost_adjustments (id, project_id, provider, period_start, period_end, "
            " invoice_ref, line_ref, description, invoice_usd_micros, estimated_usd_micros, "
            " adjustment_usd_micros, reason, created_by) VALUES (:id, :p, :pr, :s, :e, :inv, :line, "
            " :d, :amt, :est, :diff, :why, :by) "
            "ON CONFLICT (project_id, provider, invoice_ref, line_ref) DO NOTHING"),
            {"id": make_id("cadj"), "p": project_id, "pr": provider, "s": line["period_start"],
             "e": line["period_end"], "inv": line["invoice_ref"], "line": line["line_ref"],
             "d": line["description"] or None, "amt": line["amount_usd_micros"], "est": est,
             "diff": diff, "why": reason, "by": actor}).rowcount or 0
    return {"provider": provider, "lines": len(lines), "added": added,
            "skipped_existing": len(lines) - added, "problems": problems}


# ── what Spend shows ────────────────────────────────────────────────────────────────────────

def report(connection: Any, project_id: str, start: date, end: date) -> dict[str, Any]:
    """Estimated, reconciled, fees and total for [start, end] (days, inclusive)."""
    accrue(connection, project_id, through=min(end, datetime.now(timezone.utc).date()))
    by_provider = {}
    total_est = 0
    for provider in PROVIDERS:
        est = estimated(connection, project_id, provider, start, end)
        adj = connection.execute(text(
            "SELECT COALESCE(SUM(invoice_usd_micros), 0) AS invoiced, "
            "       COALESCE(SUM(estimated_usd_micros), 0) AS covered, COUNT(*) AS n "
            "FROM cost_adjustments WHERE project_id = :p AND provider = :pr "
            "AND period_start >= :s AND period_end <= :e"),
            {"p": project_id, "pr": provider, "s": start, "e": end}).mappings().one()
        reconciled = est - int(adj["covered"] or 0) + int(adj["invoiced"] or 0)
        by_provider[provider] = {"estimated_usd_micros": est, "reconciled_usd_micros": reconciled,
                                 "invoice_lines": int(adj["n"] or 0)}
        total_est += est
    # Everything else (other engines) is estimate only.
    from .included_spend import LLM_MONEY_SQL

    money = LLM_MONEY_SQL.replace("included_in_subscription", "l.included_in_subscription") \
        .replace("cost_usd_micros", "l.cost_usd_micros")
    all_est = int(connection.execute(text(
        f"SELECT COALESCE(SUM({money}), 0) FROM llm_calls l WHERE l.project_id = :p "
        "AND l.created_at >= :s AND l.created_at < :e"),
        {"p": project_id, "s": start, "e": end + timedelta(days=1)}).scalar() or 0)
    other = all_est - total_est
    fees = int(connection.execute(text(
        "SELECT COALESCE(SUM(amount_usd_micros), 0) FROM subscription_fee_days "
        "WHERE project_id = :p AND day >= :s AND day <= :e"),
        {"p": project_id, "s": start, "e": end}).scalar() or 0)
    reconciled = other + sum(v["reconciled_usd_micros"] for v in by_provider.values())
    return {"project_id": project_id, "start": start.isoformat(), "end": end.isoformat(),
            "estimated_usd_micros": all_est, "reconciled_usd_micros": reconciled,
            "fees_usd_micros": fees, "total_usd_micros": reconciled + fees,
            "by_provider": by_provider}
