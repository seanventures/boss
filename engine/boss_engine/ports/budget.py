from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class BudgetPort(Protocol):
    """Budget boundary used by engine pipelines.

    The connection argument is intentionally opaque. Enterprise adapters use a
    SQL transaction while the OSS default ignores it and remains in-memory.

    Every method a holder of `ports.budget` calls must be declared here and implemented by
    every adapter: test_budget_port_contract.py checks the call sites against this list.
    """

    def status_for_project(self, connection: Any, project_id: str) -> dict[str, Any]:
        """The project's budget snapshot. Callers read at least `budget_state`
        (green/yellow/orange/red) to decide how much to spend."""
        ...

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
    ) -> dict[str, Any]: ...

    def assert_active_reservation(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
    ) -> Optional[dict[str, Any]]: ...

    def release(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
        reason: str,
    ) -> dict[str, Any]: ...

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
    ) -> str: ...
