from __future__ import annotations

from typing import Any, Optional

from boss_engine.services.budget import BudgetGovernor


class PostgresBudgetPort:
    """Governed budget adapter preserving the existing Postgres behavior."""

    def __init__(self, settings: Any) -> None:
        self.governor = BudgetGovernor(settings)

    def reserve(
        self,
        connection: Any,
        project_id: str,
        category: str,
        amount_usd_micros: int,
        reason: str,
        expires_in_minutes: int,
        job_id: Optional[str] = None,
        allocation_id: Optional[str] = None,
        risk_tier: Optional[int] = None,
    ) -> dict[str, Any]:
        return self.governor.reserve(
            connection,
            project_id,
            category,
            amount_usd_micros,
            reason,
            expires_in_minutes,
            job_id=job_id,
            allocation_id=allocation_id,
            risk_tier=risk_tier,
        )

    def assert_active_reservation(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
    ) -> Optional[dict[str, Any]]:
        return self.governor.assert_active_reservation(
            connection, project_id, reservation_id
        )

    def release(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.governor.release(
            connection, project_id, reservation_id, reason
        )

    def settle(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
        receipt_id: str,
        category: str,
        skill_or_model_id: str,
        estimated_usd_micros: int,
        actual_usd_micros: Optional[int],
    ) -> str:
        return self.governor.settle(
            connection,
            project_id,
            reservation_id,
            receipt_id,
            category,
            skill_or_model_id,
            estimated_usd_micros,
            actual_usd_micros,
        )
