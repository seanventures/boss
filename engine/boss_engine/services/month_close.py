"""Close a calendar month, and refuse spend dated inside it.

A close is one row: who closed the month, and which month. Closing it again
returns that row and does not write another. Spend in a month that is not
closed is admitted by the budget governor — a reservation first, then one
receipt — the same order a spend uses today. Spend dated inside a closed
month is not reserved. The refusal is one receipt, and its dedupe key is the
spend's identity, so a repeat does not write a second receipt.

This does not call a bank or a model.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id
from .receipts import ReceiptService

_MONTH = re.compile(r"\A\d{4}-(0[1-9]|1[0-2])\Z")
_ACCEPTED = "month_close.spend"
_REFUSED = "month_close.spend_refused"
_CATEGORY = "month_close"


def close_month(connection: Any, project_id: str, month: str, closed_by: str) -> dict[str, Any]:
    """Record that `closed_by` closed `month` (YYYY-MM). The second call returns the first row."""
    project = _project(connection, project_id)
    period = _calendar_month(month)
    who = _token(closed_by, "closed_by")
    existing = _close_row(connection, project["project_id"], period)
    if existing:
        return existing
    connection.execute(
        text(
            """
            INSERT INTO month_closes (id, org_id, project_id, "month", closed_by)
            VALUES (:id, :org, :p, :month, :who)
            ON CONFLICT (project_id, "month") DO NOTHING
            """
        ),
        {
            "id": make_id("mcl"),
            "org": project["org_id"],
            "p": project["project_id"],
            "month": period,
            "who": who,
        },
    )
    row = _close_row(connection, project["project_id"], period)
    if row is None:
        raise RuntimeError("month close was not recorded")
    return row


def spend(
    connection: Any,
    project_id: str,
    *,
    spent_on: Any,
    amount_usd_micros: int,
    spend_key: str,
    settings: Any = None,
) -> dict[str, Any]:
    """Accept spend in an open month. Refuse spend dated in a closed one.

    The refusal receipt's dedupe key is ``month-close:<project>:<spend_key>``.
    A repeat of that spend returns the same receipt and does not reserve.
    """
    project = _project(connection, project_id)
    period = _calendar_month(spent_on)
    amount = _amount(amount_usd_micros)
    key = _token(spend_key, "spend_key")
    dedupe = f"month-close:{project['project_id']}:{key}"
    existing = ReceiptService().get_by_dedupe_key(connection, project["org_id"], dedupe)
    if existing:
        return _from_receipt(existing, month=period)
    closed = _close_row(connection, project["project_id"], period)
    if closed:
        return _refuse(connection, project, closed, dedupe, key, amount, period)
    return _accept(connection, project, dedupe, key, amount, period, spent_on, settings)


def _accept(
    connection: Any,
    project: dict[str, str],
    dedupe: str,
    spend_key: str,
    amount: int,
    period: str,
    spent_on: Any,
    settings: Any,
) -> dict[str, Any]:
    # Open month: the budget governor reserves first, then the receipt is written.
    # That is the spend path already used today. A closed month never gets here.
    from boss_engine.config import get_settings
    from boss_engine.services.budget import BudgetGovernor

    governor = BudgetGovernor(settings or get_settings())
    reserved = governor.reserve(
        connection,
        project["project_id"],
        _CATEGORY,
        amount,
        dedupe,
        60,
    )
    if reserved.get("status") != "reserved":
        return {
            "status": "denied",
            "month": period,
            "closed_by": None,
            "reservation_id": None,
            "receipt_id": None,
            "dedupe_key": dedupe,
            "refusal": reserved.get("refusal"),
        }
    reservation_id = str(reserved["reservation_id"])
    payload = {
        "reservation_id": reservation_id,
        "amount_usd_micros": amount,
        "month": period,
        "spent_on": _spent_label(spent_on),
        "spend_key": spend_key,
    }
    receipt, _created = ReceiptService().record_completed(
        connection,
        project["org_id"],
        project["workspace_id"],
        project["project_id"],
        dedupe,
        _ACCEPTED,
        {"month": period, "spend_key": spend_key, "reservation_id": reservation_id},
        payload,
        provider_ref=reservation_id,
    )
    return {
        "status": "accepted",
        "month": period,
        "closed_by": None,
        "reservation_id": reservation_id,
        "receipt_id": receipt["id"],
        "dedupe_key": dedupe,
    }


def _refuse(
    connection: Any,
    project: dict[str, str],
    closed: dict[str, Any],
    dedupe: str,
    spend_key: str,
    amount: int,
    period: str,
) -> dict[str, Any]:
    error = {
        "code": "month_closed",
        "month": period,
        "closed_by": closed["closed_by"],
        "spend_key": spend_key,
        "amount_usd_micros": amount,
    }
    service = ReceiptService()
    receipt, created = service.create_or_reuse(
        connection,
        project["org_id"],
        project["workspace_id"],
        project["project_id"],
        dedupe,
        _REFUSED,
        {"month": period, "spend_key": spend_key},
        error,
    )
    if created:
        service.mark_failed(connection, receipt["id"], error)
    return {
        "status": "refused",
        "month": period,
        "closed_by": closed["closed_by"],
        "reservation_id": None,
        "receipt_id": receipt["id"],
        "dedupe_key": dedupe,
    }


def _from_receipt(receipt: dict[str, Any], *, month: str) -> dict[str, Any]:
    refused = receipt.get("effect_kind") == _REFUSED
    result = _json_obj(receipt.get("result"))
    error = _json_obj(receipt.get("error"))
    return {
        "status": "refused" if refused else "accepted",
        "month": str(error.get("month") or result.get("month") or month),
        "closed_by": error.get("closed_by") if refused else None,
        "reservation_id": None if refused else result.get("reservation_id"),
        "receipt_id": receipt["id"],
        "dedupe_key": receipt["dedupe_key"],
    }


def _close_row(connection: Any, project_id: str, month: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT id, org_id, project_id, "month", closed_by, closed_at
            FROM month_closes
            WHERE project_id = :p AND "month" = :month
            """
        ),
        {"p": project_id, "month": month},
    ).mappings().first()
    if not row:
        return None
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "month": row["month"],
        "closed_by": row["closed_by"],
        "closed_at": row["closed_at"],
    }


def _project(connection: Any, project_id: str) -> dict[str, str]:
    token = _token(project_id, "project_id")
    row = connection.execute(
        text(
            """
            SELECT p.id AS project_id, p.workspace_id, w.org_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :p
            """
        ),
        {"p": token},
    ).mappings().first()
    if not row:
        raise LookupError(f"project {token} not found")
    return {
        "project_id": str(row["project_id"]),
        "workspace_id": str(row["workspace_id"]),
        "org_id": str(row["org_id"]),
    }


def _calendar_month(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        return f"{moment.year:04d}-{moment.month:02d}"
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}"
    token = str(value or "").strip()
    if not _MONTH.match(token):
        raise ValueError("month must be YYYY-MM")
    return token


def _token(value: Any, name: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError(f"{name} is required")
    return token


def _amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("amount_usd_micros must be a positive integer")
    return value


def _spent_label(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}
