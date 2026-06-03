from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, now_utc


class DecisionNotFoundError(Exception):
    pass


class InvalidDecisionTransitionError(Exception):
    pass


class DecisionService:
    def expire_overdue(
        self, connection: Connection, project_id: Optional[str] = None
    ) -> int:
        params: dict[str, object] = {"now": now_utc()}
        project_filter = ""
        if project_id:
            params["project_id"] = project_id
            project_filter = "AND project_id = :project_id"

        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, default_action, deadline_at
                FROM decisions
                WHERE status = 'pending'
                  AND deadline_at IS NOT NULL
                  AND deadline_at <= :now
                  {project_filter}
                """
            ),
            params,
        ).mappings().all()

        expired = 0
        for row in rows:
            updated = connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'expired', updated_at = :updated_at
                    WHERE id = :decision_id AND status = 'pending'
                    """
                ),
                {
                    "decision_id": row["id"],
                    "updated_at": now_utc(),
                },
            )
            if updated.rowcount != 1:
                continue

            expired += 1
            emit_event(
                connection,
                row["project_id"],
                "decision.expired",
                "system",
                "decision",
                row["id"],
                {
                    "deadline_at": row["deadline_at"].isoformat(),
                    "default_action": row["default_action"] or "hold",
                    "safe_default_applied": row["default_action"] or "hold",
                },
            )
        return expired

    def get_row(
        self, connection: Connection, decision_id: str, *, for_update: bool = False
    ) -> dict:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, task_id, question, status, risk_tier, default_action,
                       recommendation, options, cost_summary, deadline_at,
                       approved_by, approved_at, created_at, updated_at
                FROM decisions
                WHERE id = :decision_id
                {lock_clause}
                """
            ),
            {"decision_id": decision_id},
        ).mappings().first()
        if not row:
            raise DecisionNotFoundError("Decision not found")
        return dict(row)

    def transition(
        self,
        connection: Connection,
        decision_id: str,
        *,
        action: str,
        actor: str,
        note: Optional[str],
    ) -> dict:
        if action not in {"approve", "reject"}:
            raise ValueError(f"Unsupported action: {action}")

        row = self.get_row(connection, decision_id, for_update=True)
        if row["status"] != "pending":
            raise InvalidDecisionTransitionError(
                f"Decision is already {row['status']} and cannot be {action}d."
            )

        deadline_at = row["deadline_at"]
        if deadline_at and deadline_at <= now_utc():
            self.expire_overdue(connection, row["project_id"])
            raise InvalidDecisionTransitionError(
                "Decision deadline has passed and the safe default was applied."
            )

        status = "approved" if action == "approve" else "rejected"
        approved_by = actor if action == "approve" else None
        approved_at = now_utc() if action == "approve" else None
        connection.execute(
            text(
                """
                UPDATE decisions
                SET status = :status,
                    approved_by = :approved_by,
                    approved_at = :approved_at,
                    updated_at = :updated_at
                WHERE id = :decision_id
                """
            ),
            {
                "decision_id": decision_id,
                "status": status,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "updated_at": now_utc(),
            },
        )
        emit_event(
            connection,
            row["project_id"],
            f"decision.{status}",
            actor,
            "decision",
            decision_id,
            {
                "from_status": "pending",
                "to_status": status,
                "note": note,
                "default_action": row["default_action"] or "hold",
            },
        )
        return self.get_row(connection, decision_id)
