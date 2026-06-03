from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import emit_event, make_id


class HealthSnapshotService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)

    def current_state(self, connection: Connection, project_id: str) -> dict[str, Any]:
        budget_snapshot = self.budget.status_for_project(connection, project_id)

        task_counts = self._group_counts(connection, project_id, "tasks")
        decision_counts = self._group_counts(connection, project_id, "decisions")
        job_counts = self._group_counts(connection, project_id, "jobs")
        outbox_counts = self._group_counts(connection, project_id, "outbox_messages")

        deadletter_count = int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM deadletters WHERE project_id = :project_id"
                ),
                {"project_id": project_id},
            ).scalar_one()
        )
        high_risk_pending_decisions = int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM decisions
                    WHERE project_id = :project_id
                      AND status IN ('pending', 'delegated')
                      AND risk_tier >= 2
                    """
                ),
                {"project_id": project_id},
            ).scalar_one()
        )
        latest_event_at = connection.execute(
            text(
                "SELECT MAX(created_at) FROM events WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        ).scalar_one_or_none()

        queue_state = {
            "queued_jobs": int(task_counts.get("__unused__", 0)),  # placeholder overwritten below
        }
        queue_state["queued_jobs"] = int(
            job_counts.get("queued", 0) + job_counts.get("failed_retryable", 0)
        )
        queue_state["leased_jobs"] = int(
            job_counts.get("leased", 0) + job_counts.get("running", 0)
        )
        queue_state["retryable_jobs"] = int(job_counts.get("failed_retryable", 0))
        queue_state["succeeded_jobs"] = int(job_counts.get("succeeded", 0))

        workload_state = {
            "open_tasks": int(task_counts.get("open", 0)),
            "completed_tasks": int(task_counts.get("completed", 0)),
            "rolled_back_tasks": int(task_counts.get("rolled_back", 0)),
            "pending_decisions": int(
                decision_counts.get("pending", 0) + decision_counts.get("delegated", 0)
            ),
            "approved_decisions": int(decision_counts.get("approved", 0)),
            "high_risk_pending_decisions": high_risk_pending_decisions,
        }

        incident_state = {
            "deadletters": deadletter_count,
            "blocked_outbox_messages": int(outbox_counts.get("blocked", 0)),
            "deadlettered_outbox_messages": int(outbox_counts.get("deadlettered", 0)),
            "retrying_outbox_messages": int(outbox_counts.get("retrying", 0)),
            "latest_event_at": latest_event_at.isoformat() if latest_event_at else None,
        }

        top_risks = self._derive_top_risks(
            budget_state=budget_snapshot["budget_state"],
            deadletter_count=deadletter_count,
            high_risk_pending_decisions=high_risk_pending_decisions,
            retryable_jobs=queue_state["retryable_jobs"],
            blocked_outbox_messages=incident_state["blocked_outbox_messages"],
            deadlettered_outbox_messages=incident_state["deadlettered_outbox_messages"],
            credentials_configured=bool(
                self.settings.github_token and self.settings.openai_api_key
            ),
        )

        summary = {
            "budget_state": budget_snapshot["budget_state"],
            "open_tasks": workload_state["open_tasks"],
            "pending_decisions": workload_state["pending_decisions"],
            "deadletters": incident_state["deadletters"],
            "active_jobs": queue_state["leased_jobs"],
            "blocked_outbox_messages": incident_state["blocked_outbox_messages"],
            "latest_event_at": incident_state["latest_event_at"],
        }

        return {
            "project_id": project_id,
            "budget_state": budget_snapshot["budget_state"],
            "queue_state": queue_state,
            "workload_state": workload_state,
            "incident_state": incident_state,
            "top_risks": top_risks,
            "summary": summary,
        }

    def refresh_snapshot(
        self, connection: Connection, project_id: str, actor: str
    ) -> dict[str, Any]:
        current = self.current_state(connection, project_id)
        snapshot_id = make_id("hs")
        connection.execute(
            text(
                """
                INSERT INTO health_snapshots (
                    id, project_id, generated_by, budget_state, queue_state,
                    workload_state, incident_state, top_risks, summary
                )
                VALUES (
                    :id, :project_id, :generated_by, :budget_state,
                    CAST(:queue_state AS JSONB), CAST(:workload_state AS JSONB),
                    CAST(:incident_state AS JSONB), CAST(:top_risks AS JSONB),
                    CAST(:summary AS JSONB)
                )
                """
            ),
            {
                "id": snapshot_id,
                "project_id": project_id,
                "generated_by": actor,
                "budget_state": current["budget_state"],
                "queue_state": json.dumps(current["queue_state"]),
                "workload_state": json.dumps(current["workload_state"]),
                "incident_state": json.dumps(current["incident_state"]),
                "top_risks": json.dumps(current["top_risks"]),
                "summary": json.dumps(current["summary"]),
            },
        )
        emit_event(
            connection,
            project_id,
            "health.snapshot_created",
            actor,
            "health_snapshot",
            snapshot_id,
            {
                "budget_state": current["budget_state"],
                "top_risk_count": len(current["top_risks"]),
            },
        )
        return self.get_snapshot(connection, snapshot_id)

    def list_snapshots(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, generated_by, budget_state, queue_state,
                       workload_state, incident_state, top_risks, summary, created_at
                FROM health_snapshots
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def latest_snapshot(
        self, connection: Connection, project_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, generated_by, budget_state, queue_state,
                       workload_state, incident_state, top_risks, summary, created_at
                FROM health_snapshots
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        return dict(row) if row else None

    def get_snapshot(self, connection: Connection, snapshot_id: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, generated_by, budget_state, queue_state,
                       workload_state, incident_state, top_risks, summary, created_at
                FROM health_snapshots
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).mappings().one()
        return dict(row)

    def _group_counts(
        self, connection: Connection, project_id: str, table_name: str
    ) -> dict[str, int]:
        rows = connection.execute(
            text(
                f"""
                SELECT status, COUNT(*) AS count
                FROM {table_name}
                WHERE project_id = :project_id
                GROUP BY status
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _derive_top_risks(
        self,
        *,
        budget_state: str,
        deadletter_count: int,
        high_risk_pending_decisions: int,
        retryable_jobs: int,
        blocked_outbox_messages: int,
        deadlettered_outbox_messages: int,
        credentials_configured: bool,
    ) -> list[dict[str, Any]]:
        risks: list[dict[str, Any]] = []
        if deadletter_count > 0:
            risks.append(
                {
                    "severity": "high",
                    "category": "incident",
                    "title": "Deadletters require operator review",
                    "detail": f"{deadletter_count} deadlettered object(s) need intervention.",
                }
            )
        if deadlettered_outbox_messages > 0 or blocked_outbox_messages > 0:
            risks.append(
                {
                    "severity": "high" if deadlettered_outbox_messages > 0 else "medium",
                    "category": "communications",
                    "title": "Outbound communication is blocked",
                    "detail": (
                        f"{deadlettered_outbox_messages} deadlettered and "
                        f"{blocked_outbox_messages} blocked outbox message(s)."
                    ),
                }
            )
        if budget_state in {"yellow", "orange", "red"}:
            severity = {"yellow": "low", "orange": "medium", "red": "high"}[
                budget_state
            ]
            risks.append(
                {
                    "severity": severity,
                    "category": "budget",
                    "title": "Budget pressure is changing runtime behavior",
                    "detail": f"Budget state is {budget_state}.",
                }
            )
        if high_risk_pending_decisions > 0:
            risks.append(
                {
                    "severity": "medium",
                    "category": "approvals",
                    "title": "High-risk decisions are waiting on approval",
                    "detail": (
                        f"{high_risk_pending_decisions} pending or delegated "
                        "decision(s) have risk tier 2 or higher."
                    ),
                }
            )
        if retryable_jobs > 0:
            risks.append(
                {
                    "severity": "medium",
                    "category": "runtime",
                    "title": "Retryable jobs are accumulating",
                    "detail": f"{retryable_jobs} queued retryable job(s) need recovery.",
                }
            )
        if not credentials_configured:
            risks.append(
                {
                    "severity": "medium",
                    "category": "configuration",
                    "title": "Real provider credentials are incomplete",
                    "detail": "GitHub or OpenAI credentials are missing, so parts of the control plane will fail closed.",
                }
            )
        if not risks:
            risks.append(
                {
                    "severity": "low",
                    "category": "status",
                    "title": "No critical risks detected",
                    "detail": "Queues, decisions, comms, and budget are within current thresholds.",
                }
            )
        severity_order = {"high": 0, "medium": 1, "low": 2}
        return sorted(risks, key=lambda item: severity_order[item["severity"]])[:5]
