from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id, now_utc
from .jobs import JobLeaseService


class DeadletterRemediationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)

    def create_incident_packet(
        self,
        connection: Connection,
        *,
        deadletter_id: str,
        actor: str,
        severity: str = "high",
        summary: Optional[str] = None,
    ) -> dict[str, Any]:
        deadletter = self._get_deadletter_for_update(connection, deadletter_id)
        if deadletter.get("incident_artifact_id") and deadletter.get("incident_task_id"):
            return self._serialize_deadletter(deadletter)

        project_id = deadletter["project_id"]
        source = self._fetch_source(connection, deadletter)
        packet = {
            "deadletter_id": deadletter["id"],
            "source_kind": deadletter["source_kind"],
            "source_id": deadletter["source_id"],
            "reason": deadletter["reason"],
            "severity": severity,
            "summary": summary or self._default_summary(deadletter),
            "payload": deadletter["payload"] or {},
            "source": source,
            "required_next_actions": self._next_actions(deadletter),
            "boss_invariants": [
                "No silent drops.",
                "Failed jobs/messages/effects must be retried, deadlettered, or escalated.",
                "Remediation must preserve budget, lease, receipt, and policy gates.",
            ],
        }
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            source.get("task_id") if isinstance(source, dict) else None,
            "deadletter_incident_packet",
            f"Incident packet for {deadletter['source_kind']} {deadletter['source_id']}",
            packet,
        )
        task_id = self._create_incident_task(
            connection,
            deadletter,
            actor,
            artifact_id,
            severity,
            packet["summary"],
        )
        connection.execute(
            text(
                """
                UPDATE deadletters
                SET status = 'incident_created',
                    incident_task_id = :incident_task_id,
                    incident_artifact_id = :incident_artifact_id,
                    updated_at = now()
                WHERE id = :deadletter_id
                """
            ),
            {
                "deadletter_id": deadletter_id,
                "incident_task_id": task_id,
                "incident_artifact_id": artifact_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "deadletter.incident_created",
            actor,
            "deadletter",
            deadletter_id,
            {
                "incident_task_id": task_id,
                "incident_artifact_id": artifact_id,
                "severity": severity,
            },
        )
        return self._get_deadletter(connection, deadletter_id)

    def requeue_job_deadletter(
        self,
        connection: Connection,
        *,
        deadletter_id: str,
        actor: str,
        reason: str = "operator_requested_requeue",
        run_after_seconds: int = 0,
    ) -> dict[str, Any]:
        deadletter = self._get_deadletter_for_update(connection, deadletter_id)
        if deadletter.get("requeued_job_id"):
            return self._serialize_deadletter(deadletter)
        if deadletter["source_kind"] != "job":
            raise ValueError("only job deadletters can be requeued")
        job = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, kind, priority, status, payload,
                       data_boundary, max_attempts
                FROM jobs
                WHERE id = :job_id AND project_id = :project_id
                FOR UPDATE
                """
            ),
            {"job_id": deadletter["source_id"], "project_id": deadletter["project_id"]},
        ).mappings().first()
        if not job:
            raise ValueError("source job not found")
        if job["status"] not in {"deadlettered", "failed_final"}:
            raise ValueError("source job is not in a requeueable terminal state")

        payload = dict(job["payload"] or {})
        payload["requeue"] = {
            "source_job_id": job["id"],
            "source_deadletter_id": deadletter_id,
            "requested_by": actor,
            "reason": reason,
        }
        new_job = self.jobs.create_job(
            connection,
            job["project_id"],
            job["kind"],
            payload=payload,
            task_id=job["task_id"],
            priority=job["priority"],
            max_attempts=job["max_attempts"],
            run_after_seconds=run_after_seconds,
            data_boundary=job["data_boundary"],
        )
        resolved_at = now_utc()
        connection.execute(
            text(
                """
                UPDATE deadletters
                SET status = 'requeued',
                    requeued_job_id = :requeued_job_id,
                    resolved_by = :resolved_by,
                    resolved_at = :resolved_at,
                    updated_at = now()
                WHERE id = :deadletter_id
                """
            ),
            {
                "deadletter_id": deadletter_id,
                "requeued_job_id": new_job["id"],
                "resolved_by": actor,
                "resolved_at": resolved_at,
            },
        )
        emit_event(
            connection,
            job["project_id"],
            "deadletter.requeued",
            actor,
            "deadletter",
            deadletter_id,
            {
                "source_job_id": job["id"],
                "requeued_job_id": new_job["id"],
                "reason": reason,
            },
        )
        return self._get_deadletter(connection, deadletter_id)

    def _create_incident_task(
        self,
        connection: Connection,
        deadletter: dict[str, Any],
        actor: str,
        artifact_id: str,
        severity: str,
        summary: str,
    ) -> str:
        task_id = make_id("tsk")
        dedupe_key = f"deadletter:{deadletter['id']}:incident"
        priority = "p0" if severity == "critical" else "p1"
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id,
                    parent_task_id, dependency_task_ids, due_at, priority, risk_tier,
                    inputs, required_artifacts, budget_allocation_id, data_boundary,
                    dedupe_key
                )
                VALUES (
                    :id, :project_id, :title, :description, 'open', 'coo',
                    NULL, '[]'::jsonb, NULL, :priority, 2,
                    CAST(:inputs AS JSONB), CAST(:required_artifacts AS JSONB),
                    NULL, 'internal', :dedupe_key
                )
                ON CONFLICT (project_id, dedupe_key) DO UPDATE
                SET updated_at = now()
                RETURNING id
                """
            ),
            {
                "id": task_id,
                "project_id": deadletter["project_id"],
                "title": f"Investigate deadletter: {deadletter['reason']}",
                "description": summary,
                "priority": priority,
                "inputs": json.dumps(
                    {
                        "deadletter_id": deadletter["id"],
                        "source_kind": deadletter["source_kind"],
                        "source_id": deadletter["source_id"],
                        "incident_artifact_id": artifact_id,
                    }
                ),
                "required_artifacts": json.dumps(["deadletter_incident_packet"]),
                "dedupe_key": dedupe_key,
            },
        )
        row = connection.execute(
            text(
                """
                SELECT id
                FROM tasks
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": deadletter["project_id"], "dedupe_key": dedupe_key},
        ).mappings().one()
        emit_event(
            connection,
            deadletter["project_id"],
            "task.created",
            actor,
            "task",
            row["id"],
            {
                "dedupe_key": dedupe_key,
                "deadletter_id": deadletter["id"],
                "incident_artifact_id": artifact_id,
                "source": "deadletter.incident",
            },
        )
        return row["id"]

    def _fetch_source(self, connection: Connection, deadletter: dict[str, Any]) -> dict[str, Any]:
        if deadletter["source_kind"] == "job":
            row = connection.execute(
                text(
                    """
                    SELECT id, task_id, kind, status, attempt, max_attempts,
                           data_boundary, payload, last_error
                    FROM jobs
                    WHERE id = :source_id AND project_id = :project_id
                    """
                ),
                {
                    "source_id": deadletter["source_id"],
                    "project_id": deadletter["project_id"],
                },
            ).mappings().first()
            return dict(row) if row else {}
        return {}

    def _next_actions(self, deadletter: dict[str, Any]) -> list[str]:
        if deadletter["source_kind"] == "job":
            return [
                "Review the incident packet and source job payload.",
                "Fix configuration, policy, or code that caused the terminal failure.",
                "Use job requeue only after the root cause is safe to retry.",
            ]
        return [
            "Review the incident packet and source payload.",
            "Create a remediation decision or follow-up task for the owning role.",
        ]

    def _default_summary(self, deadletter: dict[str, Any]) -> str:
        return (
            f"{deadletter['source_kind']} {deadletter['source_id']} deadlettered: "
            f"{deadletter['reason']}."
        )

    def _get_deadletter_for_update(
        self, connection: Connection, deadletter_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT *
                FROM deadletters
                WHERE id = :deadletter_id
                FOR UPDATE
                """
            ),
            {"deadletter_id": deadletter_id},
        ).mappings().first()
        if not row:
            raise ValueError("deadletter not found")
        return self._serialize_deadletter(row)

    def _get_deadletter(self, connection: Connection, deadletter_id: str) -> dict[str, Any]:
        row = connection.execute(
            text("SELECT * FROM deadletters WHERE id = :deadletter_id"),
            {"deadletter_id": deadletter_id},
        ).mappings().one()
        return self._serialize_deadletter(row)

    def _serialize_deadletter(self, row: Any) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "resolved_at"):
            if payload.get(key) and hasattr(payload[key], "isoformat"):
                payload[key] = payload[key].isoformat()
        return payload
