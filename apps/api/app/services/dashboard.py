"""Real-time dashboard aggregation (spec §F): one consolidated snapshot of everything an operator
watches — budget/queue/workload/incidents (from the health service) plus model-provider health,
the latest canary + eval run, spend run-rate, and the no-silent-drops ack backlog.

To replace blind fixed-interval refresh, the snapshot carries a monotonic `cursor` (derived from the
event log). Clients poll `GET /v1/dashboard` and re-render only when the cursor advances; the bounded
long-poll (`poll`) holds the request open until the cursor changes or the timeout elapses — the
data-layer foundation for a WebSocket feed without a fixed 7s refresh.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..db import begin
from .budget import BudgetGovernor
from .health import HealthSnapshotService


class DashboardService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.health = HealthSnapshotService(settings)
        self.budget = BudgetGovernor(settings)

    def cursor(self, connection: Connection, project_id: str) -> str:
        """Monotonic change token for the project — advances whenever a new event is recorded."""
        row = connection.execute(
            text(
                "SELECT count(*) AS n, COALESCE(MAX(created_at), now()) AS latest "
                "FROM events WHERE project_id = :p"
            ),
            {"p": project_id},
        ).mappings().one()
        latest = row["latest"]
        return f"{int(row['n'])}:{latest.timestamp():.0f}"

    def snapshot(self, connection: Connection, project_id: str) -> dict[str, Any]:
        health = self.health.current_state(connection, project_id)
        budget_snapshot = self.budget.status_for_project(connection, project_id)

        provider = connection.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE api_available) AS healthy
                FROM provider_health
                WHERE project_id = :p
                  AND last_checked_at >= now() - interval '1 day'
                """
            ),
            {"p": project_id},
        ).mappings().one()

        canary = connection.execute(
            text(
                "SELECT id, status, passed, failed, skipped, created_at FROM canary_runs "
                "WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id},
        ).mappings().first()
        evals = connection.execute(
            text(
                "SELECT id, status, passed, failed, created_at FROM eval_runs "
                "WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id},
        ).mappings().first()

        run_rate = int(
            connection.execute(
                text(
                    "SELECT COALESCE(SUM(amount_usd_micros), 0) FROM budget_reservations "
                    "WHERE project_id = :p AND created_at >= now() - interval '60 minutes'"
                ),
                {"p": project_id},
            ).scalar()
            or 0
        )
        ack_pending = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM ack_state WHERE project_id = :p AND terminal = false"
                ),
                {"p": project_id},
            ).scalar()
            or 0
        )

        return {
            "project_id": project_id,
            "cursor": self.cursor(connection, project_id),
            "budget_state": health["budget_state"],
            "queue_state": health["queue_state"],
            "workload_state": health["workload_state"],
            "incident_state": health["incident_state"],
            "top_risks": health["top_risks"],
            "provider_health": {
                "total": int(provider["total"]),
                "healthy": int(provider["healthy"]),
            },
            "canary": dict(canary) if canary else None,
            "eval": dict(evals) if evals else None,
            "spend": {
                "cap_usd_micros": budget_snapshot["cap_usd_micros"],
                "reserved_usd_micros": budget_snapshot["reserved_usd_micros"],
                "actual_usd_micros": budget_snapshot["actual_usd_micros"],
                "run_rate_1h_usd_micros": run_rate,
            },
            "ack_backlog": ack_pending,
        }

    def poll(
        self,
        engine,
        project_id: str,
        *,
        cursor: Optional[str],
        timeout_ms: int = 5000,
        interval_ms: int = 250,
    ) -> dict[str, Any]:
        """Bounded long-poll: return as soon as the cursor advances past `cursor`, else when the
        timeout elapses. `changed` says whether new activity arrived within the window."""
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        interval = max(0.02, interval_ms / 1000.0)
        while True:
            with begin(engine) as connection:
                snap = self.snapshot(connection, project_id)
            if cursor is None or snap["cursor"] != cursor:
                return {"changed": True, **snap}
            if time.monotonic() >= deadline:
                return {"changed": False, **snap}
            time.sleep(interval)
