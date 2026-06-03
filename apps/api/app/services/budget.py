from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_blocking_decision, emit_event, expires_at, make_id


class BudgetGovernor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def status_for_project(self, connection: Connection, project_id: str) -> dict:
        policy_row = connection.execute(
            text(
                """
                SELECT daily_cap_usd_micros, soft_cap_pct, auto_lite_mode_pct, freeze_provisioning_pct
                FROM budget_policies
                WHERE scope_type = 'project' AND scope_id = :project_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()

        if not policy_row:
            return {
                "budget_state": "unknown",
                "cap_usd_micros": 0,
                "reserved_usd_micros": 0,
                "actual_usd_micros": 0,
            }

        reserved = connection.execute(
            text(
                """
                SELECT COALESCE(SUM(amount_usd_micros), 0) AS total
                FROM budget_reservations
                WHERE project_id = :project_id AND status = 'active'
                """
            ),
            {"project_id": project_id},
        ).scalar_one()

        actual = connection.execute(
            text(
                """
                SELECT COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros)), 0) AS total
                FROM cost_receipts
                WHERE project_id = :project_id
                """
            ),
            {"project_id": project_id},
        ).scalar_one()

        cap = int(policy_row["daily_cap_usd_micros"])
        utilization = 0 if cap == 0 else (reserved + actual) / cap
        soft = float(policy_row["soft_cap_pct"])
        orange = float(policy_row["auto_lite_mode_pct"])
        red = float(policy_row["freeze_provisioning_pct"])

        if utilization >= red:
            budget_state = "red"
        elif utilization >= orange:
            budget_state = "orange"
        elif utilization >= soft:
            budget_state = "yellow"
        else:
            budget_state = "green"

        return {
            "budget_state": budget_state,
            "cap_usd_micros": cap,
            "reserved_usd_micros": int(reserved),
            "actual_usd_micros": int(actual),
        }

    def reserve(
        self,
        connection: Connection,
        project_id: str,
        category: str,
        amount_usd_micros: int,
        reason: str,
        expires_in_minutes: int,
    ) -> dict:
        snapshot = self.status_for_project(connection, project_id)
        total_after = (
            snapshot["reserved_usd_micros"] + snapshot["actual_usd_micros"] + amount_usd_micros
        )

        if snapshot["cap_usd_micros"] and total_after > snapshot["cap_usd_micros"]:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Budget reservation denied",
                {"action": "increase_budget_or_defer"},
                {
                    "requested_usd_micros": amount_usd_micros,
                    "available_usd_micros": max(
                        snapshot["cap_usd_micros"]
                        - snapshot["reserved_usd_micros"]
                        - snapshot["actual_usd_micros"],
                        0,
                    ),
                },
                "budget_denied",
            )
            emit_event(
                connection,
                project_id,
                "budget.reservation_denied",
                "budget_governor",
                "decision",
                decision_id,
                {"requested_usd_micros": amount_usd_micros, "reason": reason},
            )
            return {
                "status": "denied",
                "decision_id": decision_id,
                "budget_state": snapshot["budget_state"],
                "message": "Budget cap exceeded. Decision created.",
            }

        reservation_id = make_id("res")
        connection.execute(
            text(
                """
                INSERT INTO budget_reservations (
                    id, allocation_id, project_id, job_id, category, amount_usd_micros,
                    status, reason, expires_at
                )
                VALUES (
                    :id, NULL, :project_id, NULL, :category, :amount_usd_micros,
                    'active', :reason, :expires_at
                )
                """
            ),
            {
                "id": reservation_id,
                "project_id": project_id,
                "category": category,
                "amount_usd_micros": amount_usd_micros,
                "reason": reason,
                "expires_at": expires_at(expires_in_minutes),
            },
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_created",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {"category": category, "amount_usd_micros": amount_usd_micros},
        )
        fresh = self.status_for_project(connection, project_id)
        return {
            "status": "reserved",
            "reservation_id": reservation_id,
            "decision_id": None,
            "budget_state": fresh["budget_state"],
            "message": "Reservation created.",
        }

    def assert_active_reservation(
        self, connection: Connection, project_id: str, reservation_id: str
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                """
                SELECT id, amount_usd_micros, status, expires_at
                FROM budget_reservations
                WHERE id = :reservation_id AND project_id = :project_id
                """
            ),
            {"reservation_id": reservation_id, "project_id": project_id},
        ).mappings().first()

        if not row or row["status"] != "active":
            return None
        return dict(row)

    def settle(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        receipt_id: str,
        category: str,
        skill_or_model_id: str,
        estimated_usd_micros: int,
        actual_usd_micros: Optional[int],
    ) -> None:
        cost_receipt_id = make_id("cost")
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, reservation_id, receipt_id, category, skill_or_model_id,
                    estimated_usd_micros, actual_usd_micros, status
                )
                VALUES (
                    :id, :project_id, :reservation_id, :receipt_id, :category, :skill_or_model_id,
                    :estimated_usd_micros, :actual_usd_micros, :status
                )
                """
            ),
            {
                "id": cost_receipt_id,
                "project_id": project_id,
                "reservation_id": reservation_id,
                "receipt_id": receipt_id,
                "category": category,
                "skill_or_model_id": skill_or_model_id,
                "estimated_usd_micros": estimated_usd_micros,
                "actual_usd_micros": actual_usd_micros,
                "status": "actual" if actual_usd_micros is not None else "estimated",
            },
        )
        connection.execute(
            text(
                """
                UPDATE budget_reservations
                SET status = 'consumed'
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_consumed",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {"receipt_id": receipt_id, "category": category},
        )
