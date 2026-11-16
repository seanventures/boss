"""Elapsed work and settled spend for one builder turn (BOSSAPP-1246).

Settled money is only ``cost_receipts.actual_usd_micros`` on receipts that name
this turn. No receipt means unknown, not $0. A subscription receipt contributes
$0 because the row says so, not because a list price was guessed.

More credit pauses on a check-in. Declining that check-in does not reserve.
Approving reserves budget first, then writes one receipt keyed by
``turn-meter:{project}:{turn}:{request}``. The same key does not reserve again.
A refusal is returned on the turn and, when it will not clear itself, dead-lettered.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id, now_utc
from .receipts import ReceiptService

EFFECT_KIND = "builder_turn_credit"


class TurnMeterError(ValueError):
    """The ask itself is invalid. Nothing was reserved."""


def open_turn(
    connection: Any,
    project_id: str,
    *,
    turn_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Open a builder turn, or return the one already open. Does not spend."""
    project = _project(connection, project_id)
    token = _token(turn_id, "turn_id") if turn_id else make_id("trn")
    clock = _clock(started_at or now)
    existing = _turn_row(connection, project_id, token)
    if existing:
        return _view(connection, existing, now=now)
    other = connection.execute(
        text("SELECT project_id FROM builder_turns WHERE id = :id"),
        {"id": token},
    ).first()
    if other:
        raise TurnMeterError("That turn belongs to another project.")
    connection.execute(
        text(
            """
            INSERT INTO builder_turns (
                id, org_id, workspace_id, project_id, status, started_at
            )
            VALUES (:id, :org, :ws, :p, 'working', :started)
            """
        ),
        {
            "id": token,
            "org": project["org_id"],
            "ws": project["workspace_id"],
            "p": project_id,
            "started": clock,
        },
    )
    return _view(connection, _turn_row(connection, project_id, token), now=now or clock)


def read_meter(
    connection: Any,
    project_id: str,
    turn_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    row = _turn_row(connection, project_id, _token(turn_id, "turn_id"))
    if not row:
        raise LookupError("Builder turn not found.")
    return _view(connection, row, now=now)


def request_credit(
    connection: Any,
    project_id: str,
    turn_id: str,
    request_key: str,
    amount_usd_micros: int,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pause for a credit check-in. Does not reserve budget."""
    turn = _require_turn(connection, project_id, turn_id)
    key = _request_key(request_key)
    amount = _amount(amount_usd_micros)
    dedupe = _dedupe(project_id, turn["id"], key)
    existing = _checkin_by_dedupe(connection, project_id, dedupe)
    if existing:
        if existing["status"] == "pending":
            _mark(connection, turn, "paused_for_credit", error=None, now=now)
        return _view(connection, _require_turn(connection, project_id, turn["id"]), now=now)
    connection.execute(
        text(
            """
            INSERT INTO builder_turn_checkins (
                id, project_id, turn_id, request_key, dedupe_key, amount_usd_micros, status
            )
            VALUES (:id, :p, :t, :k, :d, :a, 'pending')
            """
        ),
        {
            "id": make_id("chk"),
            "p": project_id,
            "t": turn["id"],
            "k": key,
            "d": dedupe,
            "a": amount,
        },
    )
    _mark(connection, turn, "paused_for_credit", error=None, now=now)
    return _view(connection, _require_turn(connection, project_id, turn["id"]), now=now)


def answer_credit(
    connection: Any,
    project_id: str,
    turn_id: str,
    request_key: str,
    decision: str,
    *,
    now: Optional[datetime] = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Approve reserves once, then writes one receipt. Decline reserves nothing."""
    word = str(decision or "").strip().lower()
    if word not in ("approve", "decline"):
        raise TurnMeterError("decision must be approve or decline.")
    turn = _require_turn(connection, project_id, turn_id)
    dedupe = _dedupe(project_id, turn["id"], _request_key(request_key))
    row = _lock_checkin(connection, project_id, dedupe)
    if not row:
        raise LookupError("Credit check-in not found.")
    if row["status"] == "declined" or (row["status"] == "approved" and row["receipt_id"]):
        return _view(connection, _require_turn(connection, project_id, turn["id"]), now=now)
    clock = _clock(now)
    if word == "decline":
        connection.execute(
            text(
                """
                UPDATE builder_turn_checkins
                SET status = 'declined', error = NULL, answered_at = :now
                WHERE id = :id
                """
            ),
            {"id": row["id"], "now": clock},
        )
        connection.execute(
            text(
                """
                UPDATE builder_turns
                SET status = 'declined', last_error = NULL,
                    ended_at = COALESCE(ended_at, :now), updated_at = :now
                WHERE id = :id AND project_id = :p
                """
            ),
            {"id": turn["id"], "p": project_id, "now": clock},
        )
        return _view(connection, _require_turn(connection, project_id, turn["id"]), now=clock)

    if not row["reservation_id"]:
        reserved = _reserve(connection, project_id, dedupe, int(row["amount_usd_micros"]), settings)
        if reserved.get("status") != "reserved":
            return _refuse(connection, turn, row, reserved, now=clock)
        row = {**row, "reservation_id": reserved["reservation_id"]}
    receipt, created = ReceiptService().record_completed(
        connection,
        turn["org_id"],
        turn["workspace_id"],
        project_id,
        dedupe,
        EFFECT_KIND,
        {
            "turn_id": turn["id"],
            "request_key": row["request_key"],
            "reservation_id": row["reservation_id"],
        },
        {
            "turn_id": turn["id"],
            "request_key": row["request_key"],
            "reservation_id": row["reservation_id"],
            "reserved_usd_micros": int(row["amount_usd_micros"]),
            "settled": False,
        },
        provider_ref=row["reservation_id"],
    )
    if created:
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, reservation_id, receipt_id, category,
                    skill_or_model_id, estimated_usd_micros, actual_usd_micros, status
                )
                VALUES (
                    :id, :p, :res, :rcpt, 'builder_turn', 'builder.turn', :est, NULL,
                    'estimated'
                )
                """
            ),
            {
                "id": make_id("cr"),
                "p": project_id,
                "res": row["reservation_id"],
                "rcpt": receipt["id"],
                "est": int(row["amount_usd_micros"]),
            },
        )
    connection.execute(
        text(
            """
            UPDATE builder_turn_checkins
            SET status = 'approved', reservation_id = :res, receipt_id = :rcpt,
                error = NULL, answered_at = :now
            WHERE id = :id
            """
        ),
        {
            "id": row["id"],
            "res": row["reservation_id"],
            "rcpt": receipt["id"],
            "now": clock,
        },
    )
    _mark(connection, turn, "working", error=None, now=clock)
    return _view(connection, _require_turn(connection, project_id, turn["id"]), now=clock)


def _refuse(
    connection: Any,
    turn: dict[str, Any],
    row: dict[str, Any],
    reserved: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    message = str(reserved.get("message") or reserved.get("refusal") or "Credit was not reserved.")
    transient = bool(reserved.get("transient"))
    letter = None
    if not transient:
        letter = _deadletter(connection, turn["project_id"], turn["id"], message, reserved)
    connection.execute(
        text(
            """
            UPDATE builder_turn_checkins
            SET status = :status, error = :error, deadletter_id = :letter, answered_at = :now
            WHERE id = :id
            """
        ),
        {
            "id": row["id"],
            "status": "pending" if transient else "failed",
            "error": message,
            "letter": letter,
            "now": now,
        },
    )
    _mark(
        connection,
        turn,
        "paused_for_credit" if transient else "failed",
        error=message,
        now=now,
    )
    return _view(connection, _require_turn(connection, turn["project_id"], turn["id"]), now=now)


def _reserve(
    connection: Any,
    project_id: str,
    dedupe: str,
    amount_usd_micros: int,
    settings: Any,
) -> dict[str, Any]:
    from boss_engine.config import get_settings
    from boss_engine.services.budget import BudgetGovernor

    governor = BudgetGovernor(settings or get_settings())
    return governor.reserve(
        connection,
        project_id,
        "builder_turn",
        amount_usd_micros,
        dedupe,
        60,
    )


def _deadletter(
    connection: Any,
    project_id: str,
    turn_id: str,
    reason: str,
    payload: dict[str, Any],
) -> str:
    letter_id = make_id("dlq")
    connection.execute(
        text(
            """
            INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
            VALUES (:id, :p, 'builder_turn', :src, :reason, CAST(:payload AS JSONB))
            """
        ),
        {
            "id": letter_id,
            "p": project_id,
            "src": turn_id,
            "reason": reason[:500],
            "payload": json.dumps(payload, default=str),
        },
    )
    return letter_id


def _view(connection: Any, turn: dict[str, Any], *, now: Optional[datetime]) -> dict[str, Any]:
    clock = _clock(now)
    started = _aware(turn["started_at"])
    ended = _aware(turn["ended_at"]) if turn.get("ended_at") else None
    end = ended or clock
    elapsed_ms = max(0, int((end - started).total_seconds() * 1000))
    settled_known, settled = _settled(connection, turn["project_id"], turn["id"])
    checkin = _latest_checkin(connection, turn["project_id"], turn["id"])
    error = None
    if checkin and checkin.get("error"):
        error = checkin["error"]
    elif turn.get("last_error"):
        error = turn["last_error"]
    retry = bool(checkin and checkin.get("status") == "pending" and checkin.get("error"))
    return {
        "turn_id": turn["id"],
        "project_id": turn["project_id"],
        "org_id": turn["org_id"],
        "workspace_id": turn["workspace_id"],
        "status": turn["status"],
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat() if ended else None,
        "elapsed_ms": elapsed_ms,
        "settled_usd_micros": settled if settled_known else None,
        "settled_known": settled_known,
        "error": error,
        "retry": retry,
        "checkin": checkin,
    }


def _settled(connection: Any, project_id: str, turn_id: str) -> tuple[bool, int]:
    """Sum settled receipt rows only. Estimates and other turns stay out."""
    row = connection.execute(
        text(
            """
            SELECT COUNT(*) FILTER (WHERE cr.actual_usd_micros IS NOT NULL) AS n,
                   COALESCE(SUM(
                       CASE
                           WHEN cr.actual_usd_micros IS NULL THEN NULL
                           WHEN COALESCE(cr.included_in_subscription, false) THEN 0
                           ELSE cr.actual_usd_micros
                       END
                   ), 0) AS settled
            FROM cost_receipts cr
            JOIN receipts r ON r.id = cr.receipt_id AND r.project_id = cr.project_id
            WHERE r.project_id = :p
              AND (
                    r.effect_scope->>'turn_id' = :t
                    OR (r.result IS NOT NULL AND r.result->>'turn_id' = :t)
                  )
            """
        ),
        {"p": project_id, "t": turn_id},
    ).mappings().one()
    count = int(row["n"] or 0)
    if count == 0:
        return False, 0
    return True, int(row["settled"] or 0)


def _latest_checkin(connection: Any, project_id: str, turn_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT id, request_key, dedupe_key, amount_usd_micros, status,
                   reservation_id, receipt_id, error, deadletter_id
            FROM builder_turn_checkins
            WHERE project_id = :p AND turn_id = :t
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"p": project_id, "t": turn_id},
    ).mappings().first()
    if not row:
        return None
    return {
        "id": row["id"],
        "request_key": row["request_key"],
        "dedupe_key": row["dedupe_key"],
        "amount_usd_micros": int(row["amount_usd_micros"]),
        "status": row["status"],
        "reservation_id": row["reservation_id"],
        "receipt_id": row["receipt_id"],
        "error": row["error"],
        "deadletter_id": row["deadletter_id"],
    }


def _project(connection: Any, project_id: str) -> dict[str, str]:
    row = connection.execute(
        text(
            """
            SELECT p.id AS project_id, p.workspace_id, w.org_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :p
            """
        ),
        {"p": project_id},
    ).mappings().first()
    if not row:
        raise LookupError("Project not found.")
    return {"project_id": row["project_id"], "workspace_id": row["workspace_id"], "org_id": row["org_id"]}


def _turn_row(connection: Any, project_id: str, turn_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT id, org_id, workspace_id, project_id, status, started_at, ended_at, last_error
            FROM builder_turns
            WHERE id = :id AND project_id = :p
            """
        ),
        {"id": turn_id, "p": project_id},
    ).mappings().first()
    return dict(row) if row else None


def _require_turn(connection: Any, project_id: str, turn_id: str) -> dict[str, Any]:
    row = _turn_row(connection, project_id, _token(turn_id, "turn_id"))
    if not row:
        raise LookupError("Builder turn not found.")
    return row


def _checkin_by_dedupe(connection: Any, project_id: str, dedupe: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT id, status, reservation_id, receipt_id
            FROM builder_turn_checkins
            WHERE project_id = :p AND dedupe_key = :d
            """
        ),
        {"p": project_id, "d": dedupe},
    ).mappings().first()
    return dict(row) if row else None


def _lock_checkin(connection: Any, project_id: str, dedupe: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT id, turn_id, request_key, dedupe_key, amount_usd_micros, status,
                   reservation_id, receipt_id, error, deadletter_id
            FROM builder_turn_checkins
            WHERE project_id = :p AND dedupe_key = :d
            FOR UPDATE
            """
        ),
        {"p": project_id, "d": dedupe},
    ).mappings().first()
    return dict(row) if row else None


def _mark(
    connection: Any,
    turn: dict[str, Any],
    status: str,
    *,
    error: Optional[str],
    now: Optional[datetime],
) -> None:
    connection.execute(
        text(
            """
            UPDATE builder_turns
            SET status = :status, last_error = :error, updated_at = :now
            WHERE id = :id AND project_id = :p
            """
        ),
        {
            "status": status,
            "error": error,
            "now": _clock(now),
            "id": turn["id"],
            "p": turn["project_id"],
        },
    )


def _dedupe(project_id: str, turn_id: str, request_key: str) -> str:
    return f"turn-meter:{project_id}:{turn_id}:{request_key}"


def _token(value: Optional[str], name: str) -> str:
    token = str(value or "").strip()
    if not token or len(token) > 200 or any(ch.isspace() for ch in token):
        raise TurnMeterError(f"{name} must be one token.")
    return token


def _request_key(value: str) -> str:
    key = _token(value, "request_key")
    if len(key) > 120:
        raise TurnMeterError("request_key must be one token.")
    return key


def _amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TurnMeterError("amount_usd_micros must be a positive integer.")
    return value


def _clock(now: Optional[datetime]) -> datetime:
    if now is None:
        return now_utc()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
