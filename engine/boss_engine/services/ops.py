from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from . import decision_actions
from .core import create_artifact, emit_event, now_utc
from .health import HealthSnapshotService


class OpsQueueService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.health = HealthSnapshotService(settings)

    def inspect_queue(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 25,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        current = self.health.current_state(connection, project_id)
        jobs = self._fetch_jobs(connection, project_id, limit, include_terminal)
        deadletters = self._fetch_open_deadletters(connection, project_id, limit)
        pending_decisions = self._fetch_pending_decisions(connection, project_id, limit)
        job_status_counts = self._count_by_status(connection, "jobs", project_id)
        job_kind_counts = self._count_jobs_by_kind(connection, project_id)
        stale_lease_job_ids = [
            job["id"]
            for job in jobs
            if job["status"] in {"leased", "running"}
            and job.get("lease_expires_at")
            and job["lease_expires_at"] < now_utc()
        ]
        ready_job_ids = [
            job["id"]
            for job in jobs
            if job["status"] in {"queued", "failed_retryable"}
            and job.get("run_after")
            and job["run_after"] <= now_utc()
        ]
        blockers = self._derive_blockers(
            current,
            jobs=jobs,
            deadletters=deadletters,
            pending_decisions=pending_decisions,
            stale_lease_job_ids=stale_lease_job_ids,
            ready_job_ids=ready_job_ids,
        )
        recommended_actions = self._recommended_actions(blockers)
        return {
            "project_id": project_id,
            "generated_at": now_utc().isoformat(),
            "budget_state": current["budget_state"],
            "queue_state": current["queue_state"],
            "workload_state": current["workload_state"],
            "incident_state": current["incident_state"],
            "job_status_counts": job_status_counts,
            "job_kind_counts": job_kind_counts,
            "stale_lease_job_ids": stale_lease_job_ids,
            "ready_job_ids": ready_job_ids,
            "jobs": [self._serialize_temporal(job) for job in jobs],
            "deadletters": [self._serialize_temporal(row) for row in deadletters],
            "pending_decisions": [self._serialize_temporal(row) for row in pending_decisions],
            "blockers": blockers,
            "recommended_actions": recommended_actions,
        }

    def create_ops_digest(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        limit: int = 25,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        inspection = self.inspect_queue(
            connection,
            project_id,
            limit=limit,
            include_terminal=include_terminal,
        )
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "ops_digest",
            "COO queue and incident digest",
            {
                "source": "ops.queue.inspect",
                "inspection": inspection,
                "required_references": ["job", "deadletter", "decision", "event"],
                "boss_invariants": [
                    "No worker execution without a lease.",
                    "No silent drops.",
                    "Every failed job/message/effect is retried, deadlettered, or escalated.",
                ],
            },
        )
        emit_event(
            connection,
            project_id,
            "ops.digest_created",
            actor,
            "artifact",
            artifact_id,
            {
                "blocker_count": len(inspection["blockers"]),
                "job_count": len(inspection["jobs"]),
                "deadletter_count": len(inspection["deadletters"]),
                "pending_decision_count": len(inspection["pending_decisions"]),
            },
        )
        return {
            "status": "created",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "artifact_type": "ops_digest",
            "created_at": now_utc().isoformat(),
            "counts": {
                "jobs": len(inspection["jobs"]),
                "deadletters": len(inspection["deadletters"]),
                "pending_decisions": len(inspection["pending_decisions"]),
                "blockers": len(inspection["blockers"]),
            },
            "queue_state": inspection["queue_state"],
            "blockers": inspection["blockers"],
            "recommended_actions": inspection["recommended_actions"],
        }

    def _fetch_jobs(
        self,
        connection: Connection,
        project_id: str,
        limit: int,
        include_terminal: bool,
    ) -> list[dict[str, Any]]:
        terminal_filter = (
            ""
            if include_terminal
            else "AND j.status NOT IN ('succeeded', 'cancelled')"
        )
        rows = connection.execute(
            text(
                f"""
                SELECT j.id, j.project_id, j.task_id, t.title AS task_title,
                       j.kind, j.status, j.priority, j.attempt, j.max_attempts,
                       j.data_boundary, j.run_after, j.lease_owner,
                       j.lease_expires_at, j.heartbeat_at, j.last_error,
                       j.created_at, j.updated_at
                FROM jobs j
                LEFT JOIN tasks t ON t.id = j.task_id
                WHERE j.project_id = :project_id
                  {terminal_filter}
                ORDER BY
                  CASE j.status
                    WHEN 'deadlettered' THEN 0
                    WHEN 'failed_final' THEN 1
                    WHEN 'failed_retryable' THEN 2
                    WHEN 'leased' THEN 3
                    WHEN 'running' THEN 4
                    WHEN 'queued' THEN 5
                    ELSE 6
                  END,
                  j.priority ASC,
                  j.run_after ASC,
                  j.created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_open_deadletters(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, source_kind, source_id, reason, status,
                       incident_task_id, incident_artifact_id, requeued_job_id,
                       resolved_by, resolved_at, created_at, updated_at
                FROM deadletters
                WHERE project_id = :project_id
                  AND status IN ('open', 'incident_created')
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_pending_decisions(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        # The decisions waiting on the operator, by the Approvals inbox's rule. This was its own
        # pending-plus-delegated list, which disagreed with the inbox (review round 3).
        keys = ("id", "task_id", "question", "status", "risk_tier", "default_action",
                "deadline_at", "created_at", "updated_at")
        rows = decision_actions.waiting_on_operator(connection, project_id)[:limit]
        return [{k: row.get(k) for k in keys} for row in rows]

    def _count_by_status(
        self, connection: Connection, table_name: str, project_id: str
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

    def _count_jobs_by_kind(
        self, connection: Connection, project_id: str
    ) -> dict[str, int]:
        rows = connection.execute(
            text(
                """
                SELECT kind, COUNT(*) AS count
                FROM jobs
                WHERE project_id = :project_id
                  AND status NOT IN ('succeeded', 'cancelled')
                GROUP BY kind
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return {str(row["kind"]): int(row["count"]) for row in rows}

    def _derive_blockers(
        self,
        current: dict[str, Any],
        *,
        jobs: list[dict[str, Any]],
        deadletters: list[dict[str, Any]],
        pending_decisions: list[dict[str, Any]],
        stale_lease_job_ids: list[str],
        ready_job_ids: list[str],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if deadletters:
            blockers.append(
                {
                    "severity": "high",
                    "category": "deadletters",
                    "title": "Open deadletters require remediation",
                    "object_ids": [row["id"] for row in deadletters[:10]],
                    "next_action": "Create incident packets or requeue safe terminal job deadletters.",
                }
            )
        if stale_lease_job_ids:
            blockers.append(
                {
                    "severity": "high",
                    "category": "leases",
                    "title": "Expired leases need recovery",
                    "object_ids": stale_lease_job_ids[:10],
                    "next_action": "Let lease reclaim run or inspect worker health before retry.",
                }
            )
        retryable_job_ids = [
            row["id"] for row in jobs if row["status"] == "failed_retryable"
        ]
        if retryable_job_ids:
            blockers.append(
                {
                    "severity": "medium",
                    "category": "retry",
                    "title": "Retryable jobs are queued",
                    "object_ids": retryable_job_ids[:10],
                    "next_action": "Inspect repeated errors and worker capacity before retry pressure grows.",
                }
            )
        high_risk_decisions = [
            row["id"] for row in pending_decisions if int(row["risk_tier"]) >= 2
        ]
        if high_risk_decisions:
            blockers.append(
                {
                    "severity": "medium",
                    "category": "decisions",
                    "title": "High-risk decisions are blocking execution",
                    "object_ids": high_risk_decisions[:10],
                    "next_action": "Route decision cards to required approvers.",
                }
            )
        if current["budget_state"] in {"orange", "red"}:
            blockers.append(
                {
                    "severity": "high" if current["budget_state"] == "red" else "medium",
                    "category": "budget",
                    "title": "Budget state is constraining runtime behavior",
                    "object_ids": [],
                    "next_action": "Defer non-critical work or request an approved budget change.",
                }
            )
        if ready_job_ids and not blockers:
            blockers.append(
                {
                    "severity": "low",
                    "category": "queue",
                    "title": "Queue has ready work",
                    "object_ids": ready_job_ids[:10],
                    "next_action": "Ensure workers are polling with matching capabilities.",
                }
            )
        return blockers

    def _recommended_actions(self, blockers: list[dict[str, Any]]) -> list[str]:
        if not blockers:
            return ["No queue blockers detected. Continue normal monitoring cadence."]
        return [str(blocker["next_action"]) for blocker in blockers[:5]]

    def _serialize_temporal(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        for key, value in list(payload.items()):
            if hasattr(value, "isoformat"):
                payload[key] = value.isoformat()
        if payload.get("last_error") is None:
            payload["last_error"] = {}
        return payload
