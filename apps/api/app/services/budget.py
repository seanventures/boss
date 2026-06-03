from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_blocking_decision, emit_event, expires_at, make_id, now_utc


class BudgetGovernor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def expire_stale_reservations(
        self, connection: Connection, project_id: Optional[str] = None
    ) -> list[str]:
        query_params: dict[str, Any] = {"now_utc": now_utc()}
        if project_id is None:
            query = text(
                """
                UPDATE budget_reservations
                SET status = 'expired'
                WHERE status = 'active'
                  AND expires_at <= :now_utc
                RETURNING id, project_id
                """
            )
        else:
            query = text(
                """
                UPDATE budget_reservations
                SET status = 'expired'
                WHERE status = 'active'
                  AND expires_at <= :now_utc
                  AND project_id = :project_id
                RETURNING id, project_id
                """
            )
            query_params["project_id"] = project_id

        rows = connection.execute(query, query_params).mappings().all()

        expired_ids: list[str] = []
        for row in rows:
            expired_ids.append(row["id"])
            emit_event(
                connection,
                row["project_id"],
                "budget.reservation_expired",
                "budget_governor",
                "budget_reservation",
                row["id"],
                {},
            )
        return expired_ids

    def status_for_project(self, connection: Connection, project_id: str) -> dict:
        self.expire_stale_reservations(connection, project_id)
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
                "available_usd_micros": 0,
                "committed_usd_micros": 0,
                "burn_approx_usd_micros": 0,
                "utilization_ratio": 0.0,
                "active_reservations_count": 0,
                "top_cost_drivers": [],
                "soft_cap_pct": 0.7,
                "auto_lite_mode_pct": 0.85,
                "freeze_provisioning_pct": 0.9,
            }

        reservation_snapshot = connection.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(amount_usd_micros), 0) AS total,
                    COUNT(*) AS active_count
                FROM budget_reservations
                WHERE project_id = :project_id AND status = 'active'
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        reserved = int(reservation_snapshot["total"])
        active_reservations_count = int(reservation_snapshot["active_count"])

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
        actual = int(actual)

        top_cost_drivers = [
            {
                "skill_or_model_id": row["skill_or_model_id"],
                "amount_usd_micros": int(row["amount_usd_micros"]),
            }
            for row in connection.execute(
                text(
                    """
                    SELECT
                        skill_or_model_id,
                        COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros)), 0) AS amount_usd_micros
                    FROM cost_receipts
                    WHERE project_id = :project_id
                    GROUP BY skill_or_model_id
                    ORDER BY amount_usd_micros DESC, skill_or_model_id ASC
                    LIMIT 5
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]

        cap = int(policy_row["daily_cap_usd_micros"])
        committed = reserved + actual
        available = max(cap - committed, 0) if cap else 0
        utilization = 0 if cap == 0 else committed / cap
        soft = float(policy_row["soft_cap_pct"])
        orange = float(policy_row["auto_lite_mode_pct"])
        red = float(policy_row["freeze_provisioning_pct"])

        budget_state = self._state_for_totals(
            cap,
            committed,
            soft,
            orange,
            red,
        )

        return {
            "budget_state": budget_state,
            "cap_usd_micros": cap,
            "reserved_usd_micros": reserved,
            "actual_usd_micros": actual,
            "available_usd_micros": available,
            "committed_usd_micros": committed,
            "burn_approx_usd_micros": committed,
            "utilization_ratio": round(utilization, 6),
            "active_reservations_count": active_reservations_count,
            "top_cost_drivers": top_cost_drivers,
            "soft_cap_pct": soft,
            "auto_lite_mode_pct": orange,
            "freeze_provisioning_pct": red,
        }

    def quote(
        self,
        connection: Connection,
        project_id: str,
        category: str,
        amount_usd_micros: int,
        reason: str,
    ) -> dict[str, Any]:
        snapshot = self.status_for_project(connection, project_id)
        total_after = (
            snapshot["reserved_usd_micros"] + snapshot["actual_usd_micros"] + amount_usd_micros
        )
        allowed = snapshot["cap_usd_micros"] == 0 or total_after <= snapshot["cap_usd_micros"]
        post_available = (
            max(snapshot["cap_usd_micros"] - total_after, 0)
            if snapshot["cap_usd_micros"]
            else 0
        )
        post_state = self._state_for_totals(
            snapshot["cap_usd_micros"],
            total_after,
            snapshot["soft_cap_pct"],
            snapshot["auto_lite_mode_pct"],
            snapshot["freeze_provisioning_pct"],
        )
        return {
            "allowed": allowed,
            "category": category,
            "reason": reason,
            "requested_usd_micros": amount_usd_micros,
            "budget_state": snapshot["budget_state"],
            "post_reservation_budget_state": post_state,
            "cap_usd_micros": snapshot["cap_usd_micros"],
            "available_usd_micros": snapshot["available_usd_micros"],
            "post_reservation_available_usd_micros": post_available,
        }

    def reserve(
        self,
        connection: Connection,
        project_id: str,
        category: str,
        amount_usd_micros: int,
        reason: str,
        expires_in_minutes: int,
        job_id: Optional[str] = None,
    ) -> dict:
        self.expire_stale_reservations(connection, project_id)
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
                    :id, NULL, :project_id, :job_id, :category, :amount_usd_micros,
                    'active', :reason, :expires_at
                )
                """
            ),
            {
                "id": reservation_id,
                "project_id": project_id,
                "job_id": job_id,
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
            {
                "category": category,
                "amount_usd_micros": amount_usd_micros,
                "job_id": job_id,
            },
        )
        fresh = self.status_for_project(connection, project_id)
        return {
            "status": "reserved",
            "reservation_id": reservation_id,
            "decision_id": None,
            "budget_state": fresh["budget_state"],
            "message": "Reservation created.",
        }

    def release(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        self.expire_stale_reservations(connection, project_id)
        row = connection.execute(
            text(
                """
                SELECT id, status, category, amount_usd_micros
                FROM budget_reservations
                WHERE id = :reservation_id AND project_id = :project_id
                """
            ),
            {"reservation_id": reservation_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            return {
                "status": "not_found",
                "reservation_id": reservation_id,
                "budget_state": self.status_for_project(connection, project_id)["budget_state"],
                "message": "Reservation not found.",
            }
        if row["status"] != "active":
            return {
                "status": row["status"],
                "reservation_id": reservation_id,
                "budget_state": self.status_for_project(connection, project_id)["budget_state"],
                "message": f"Reservation is already {row['status']}.",
            }

        connection.execute(
            text(
                """
                UPDATE budget_reservations
                SET status = 'released'
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_released",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {
                "reason": reason,
                "category": row["category"],
                "amount_usd_micros": int(row["amount_usd_micros"]),
            },
        )
        snapshot = self.status_for_project(connection, project_id)
        return {
            "status": "released",
            "reservation_id": reservation_id,
            "budget_state": snapshot["budget_state"],
            "message": reason,
        }

    def assert_active_reservation(
        self, connection: Connection, project_id: str, reservation_id: str
    ) -> Optional[dict]:
        self.expire_stale_reservations(connection, project_id)
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

    def _state_for_totals(
        self,
        cap_usd_micros: int,
        committed_usd_micros: int,
        soft_cap_pct: float,
        auto_lite_mode_pct: float,
        freeze_provisioning_pct: float,
    ) -> str:
        if cap_usd_micros == 0:
            return "unknown"
        utilization = committed_usd_micros / cap_usd_micros
        if utilization >= freeze_provisioning_pct:
            return "red"
        if utilization >= auto_lite_mode_pct:
            return "orange"
        if utilization >= soft_cap_pct:
            return "yellow"
        return "green"
