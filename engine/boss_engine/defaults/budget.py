from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Optional
from uuid import uuid4


class NoopBudgetPort:
    """Unmetered OSS budget boundary that still preserves reservation order."""

    def __init__(self) -> None:
        self._reservations: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def status_for_project(self, connection: Any, project_id: str) -> dict[str, Any]:
        """Unmetered: never under budget pressure, so the state is always green."""
        del connection
        return {"project_id": project_id, "budget_state": "green", "unmetered": True}

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
        del connection
        reservation_id = f"oss_res_{uuid4().hex[:20]}"
        reservation = {
            "id": reservation_id,
            "reservation_id": reservation_id,
            "project_id": project_id,
            "category": category,
            "amount_usd_micros": int(amount_usd_micros),
            "reason": reason,
            "status": "reserved",
            "budget_state": "unmetered",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
            ).isoformat(),
            "job_id": job_id,
            "allocation_id": allocation_id,
            "risk_tier": risk_tier,
            "unmetered": True,
        }
        with self._lock:
            self._reservations[reservation_id] = reservation
        return dict(reservation)

    def assert_active_reservation(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
    ) -> Optional[dict[str, Any]]:
        del connection
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if (
                reservation is None
                or reservation["project_id"] != project_id
                or reservation["status"] != "reserved"
            ):
                return None
            return dict(reservation)

    def release(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        del connection
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation["project_id"] != project_id:
                return {
                    "status": "not_found",
                    "reservation_id": reservation_id,
                    "budget_state": "unmetered",
                    "message": reason,
                }
            reservation["status"] = "released"
        return {
            "status": "released",
            "reservation_id": reservation_id,
            "budget_state": "unmetered",
            "message": reason,
        }

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
        *,
        included_in_subscription: bool = False,
        allow_expired: bool = False,
    ) -> str:
        del connection, category, skill_or_model_id, estimated_usd_micros, actual_usd_micros
        del included_in_subscription, allow_expired
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation["project_id"] != project_id:
                raise ValueError("active OSS reservation not found")
            reservation["status"] = "consumed"
            reservation["receipt_id"] = receipt_id
        return f"oss_cost_{uuid4().hex[:20]}"

    def extend(
        self,
        connection: Any,
        project_id: str,
        reservation_id: str,
        expires_in_minutes: int,
    ) -> bool:
        del connection
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if (reservation is None or reservation["project_id"] != project_id
                    or reservation["status"] != "reserved"):
                return False
            reservation["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
            ).isoformat()
        return True
