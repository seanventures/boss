from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    make_id,
    now_utc,
)


class LeaseNotFoundError(RuntimeError):
    pass


class JobLeaseService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)

    def create_job(
        self,
        connection: Connection,
        project_id: str,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        task_id: Optional[str] = None,
        priority: int = 100,
        max_attempts: int = 3,
        run_after_seconds: int = 0,
    ) -> dict[str, Any]:
        job_id = make_id("job")
        run_after = now_utc() + timedelta(seconds=run_after_seconds)
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, kind, priority, status, payload,
                    max_attempts, attempt, run_after
                )
                VALUES (
                    :id, :project_id, :task_id, :kind, :priority, 'queued', CAST(:payload AS JSONB),
                    :max_attempts, 0, :run_after
                )
                """
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "task_id": task_id,
                "kind": kind,
                "priority": priority,
                "payload": json.dumps(payload or {}),
                "max_attempts": max_attempts,
                "run_after": run_after,
            },
        )
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            job_id,
            {"kind": kind, "task_id": task_id},
        )
        return {
            "id": job_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": kind,
            "status": "queued",
            "priority": priority,
            "max_attempts": max_attempts,
            "attempt": 0,
            "run_after": run_after.isoformat(),
        }

    def claim_job(
        self,
        connection: Connection,
        worker_id: str,
        capabilities: list[str],
        lease_ttl_seconds: int,
    ) -> Optional[dict[str, Any]]:
        if not capabilities:
            return None

        lease_token = f"lease_{uuid4().hex}"
        lease_token_hash = self._hash_token(lease_token)
        current_time = now_utc()
        lease_expires_at = current_time + timedelta(seconds=lease_ttl_seconds)

        candidate_statement = text(
            """
            SELECT id, project_id, task_id, kind, payload, attempt, max_attempts, status
            FROM jobs
            WHERE kind IN :capabilities
              AND run_after <= :now_utc
              AND (
                status IN ('queued', 'failed_retryable')
                OR (status = 'leased' AND lease_expires_at <= :now_utc)
              )
            ORDER BY
              CASE status
                WHEN 'leased' THEN 0
                WHEN 'failed_retryable' THEN 1
                ELSE 2
              END,
              priority ASC,
              created_at ASC
            LIMIT 10
            FOR UPDATE SKIP LOCKED
            """
        ).bindparams(bindparam("capabilities", expanding=True))
        candidates = connection.execute(
            candidate_statement,
            {
                "capabilities": capabilities,
                "now_utc": current_time,
            },
        ).mappings().all()
        if not candidates:
            return None

        for candidate in candidates:
            payload = candidate["payload"] or {}
            if self._requires_reservation(candidate["kind"], payload):
                reservation_id = self._reservation_id_from_payload(payload)
                reservation = (
                    self.budget.assert_active_reservation(
                        connection, candidate["project_id"], reservation_id
                    )
                    if reservation_id
                    else None
                )
                if not reservation:
                    self._cancel_budget_blocked_job(
                        connection,
                        candidate["project_id"],
                        candidate["id"],
                        candidate["task_id"],
                        candidate["kind"],
                        reservation_id,
                    )
                    continue

            row = connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'leased',
                        lease_owner = :worker_id,
                        lease_token_hash = :lease_token_hash,
                        lease_expires_at = :lease_expires_at,
                        heartbeat_at = :now_utc,
                        attempt = jobs.attempt + 1,
                        updated_at = :now_utc
                    WHERE id = :job_id
                    RETURNING
                        id,
                        project_id,
                        task_id,
                        kind,
                        payload,
                        attempt,
                        max_attempts,
                        lease_expires_at
                    """
                ),
                {
                    "job_id": candidate["id"],
                    "worker_id": worker_id,
                    "lease_token_hash": lease_token_hash,
                    "lease_expires_at": lease_expires_at,
                    "now_utc": current_time,
                },
            ).mappings().one()

            event_type = (
                "job.lease_reclaimed"
                if candidate["status"] == "leased"
                else "job.leased"
            )
            emit_event(
                connection,
                row["project_id"],
                event_type,
                worker_id,
                "job",
                row["id"],
                {"attempt": row["attempt"], "kind": row["kind"]},
            )
            return {
                "id": row["id"],
                "project_id": row["project_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "payload": row["payload"] or {},
                "attempt": row["attempt"],
                "max_attempts": row["max_attempts"],
                "lease_token": lease_token,
                "lease_expires_at": row["lease_expires_at"].isoformat(),
            }

        return None

    def heartbeat(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> dict[str, Any]:
        current_time = now_utc()
        lease_expires_at = current_time + timedelta(seconds=lease_ttl_seconds)
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        if row["lease_expires_at"] <= current_time:
            raise LeaseNotFoundError("Lease has expired.")

        next_status = "running" if row["status"] == "leased" else row["status"]
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = :status,
                    heartbeat_at = :heartbeat_at,
                    lease_expires_at = :lease_expires_at,
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "status": next_status,
                "heartbeat_at": current_time,
                "lease_expires_at": lease_expires_at,
                "updated_at": current_time,
                "job_id": job_id,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "job.heartbeat",
            worker_id,
            "job",
            job_id,
            {"status": next_status},
        )
        return {
            "job_id": job_id,
            "status": next_status,
            "lease_expires_at": lease_expires_at.isoformat(),
        }

    def complete(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        project_id = row["project_id"]
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    lease_owner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id, "updated_at": now_utc()},
        )

        extras = self._finalize_success(
            connection,
            row,
            result or {},
        )
        emit_event(
            connection,
            project_id,
            "job.succeeded",
            worker_id,
            "job",
            job_id,
            {"kind": row["kind"], **extras},
        )
        return {"job_id": job_id, "status": "succeeded", **extras}

    def fail(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        error: dict[str, Any],
        retryable: bool,
        retry_delay_seconds: int,
    ) -> dict[str, Any]:
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        project_id = row["project_id"]
        current_time = now_utc()

        if retryable and int(row["attempt"]) < int(row["max_attempts"]):
            next_run_after = current_time + timedelta(seconds=retry_delay_seconds)
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'failed_retryable',
                        lease_owner = NULL,
                        lease_token_hash = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL,
                        last_error = CAST(:last_error AS JSONB),
                        run_after = :run_after,
                        updated_at = :updated_at
                    WHERE id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "last_error": json.dumps(error),
                    "run_after": next_run_after,
                    "updated_at": current_time,
                },
            )
            emit_event(
                connection,
                project_id,
                "job.failed_retryable",
                worker_id,
                "job",
                job_id,
                {
                    "attempt": row["attempt"],
                    "max_attempts": row["max_attempts"],
                    "run_after": next_run_after.isoformat(),
                },
            )
            return {
                "job_id": job_id,
                "status": "failed_retryable",
                "run_after": next_run_after.isoformat(),
                "deadletter_id": None,
            }

        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'failed_final',
                    lease_owner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "last_error": json.dumps(error),
                "updated_at": current_time,
            },
        )
        emit_event(
            connection,
            project_id,
            "job.failed_final",
            worker_id,
            "job",
            job_id,
            {"attempt": row["attempt"], "max_attempts": row["max_attempts"]},
        )
        deadletter_id = make_id("dlq")
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (
                    :id, :project_id, 'job', :source_id, :reason, CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "id": deadletter_id,
                "project_id": project_id,
                "source_id": job_id,
                "reason": error.get("message", "job failed"),
                "payload": json.dumps(error),
            },
        )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'deadlettered',
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id, "updated_at": current_time},
        )
        emit_event(
            connection,
            project_id,
            "job.deadlettered",
            worker_id,
            "job",
            job_id,
            {"deadletter_id": deadletter_id},
        )
        return {
            "job_id": job_id,
            "status": "deadlettered",
            "run_after": None,
            "deadletter_id": deadletter_id,
        }

    def _load_leased_job_for_update(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
    ):
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, kind, payload, status, attempt, max_attempts,
                       lease_owner, lease_token_hash, lease_expires_at
                FROM jobs
                WHERE id = :job_id
                FOR UPDATE
                """
            ),
            {"job_id": job_id},
        ).mappings().first()
        if not row:
            raise LeaseNotFoundError("Job not found.")
        if row["lease_owner"] != worker_id:
            raise LeaseNotFoundError("Job lease is owned by another worker.")
        if row["lease_token_hash"] != self._hash_token(lease_token):
            raise LeaseNotFoundError("Lease token is invalid.")
        if row["status"] not in ("leased", "running"):
            raise LeaseNotFoundError("Job is not actively leased.")
        if row["lease_expires_at"] <= now_utc():
            raise LeaseNotFoundError("Lease has expired.")
        return row

    def _finalize_success(
        self,
        connection: Connection,
        job_row,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if job_row["kind"] == "openai.plan":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
            }

        if job_row["kind"] in {"software_factory.propose_pr", "github.pull_request"}:
            return {
                "artifact_id": result.get("artifact_id"),
                "diff_artifact_id": result.get("diff_artifact_id"),
                "test_report_artifact_id": result.get("test_report_artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
            }

        if job_row["kind"] not in {"plan.command", "plan.generate"}:
            return {}

        payload = job_row["payload"] or {}
        task_title = self._task_title(connection, job_row["task_id"])
        artifact_id = create_artifact(
            connection,
            self.settings,
            job_row["project_id"],
            job_row["task_id"],
            "plan_draft",
            f"Plan draft for {task_title or payload.get('command', 'command')}",
            {
                "command": payload.get("command"),
                "constraints": payload.get("constraints", {}),
                "summary": result.get("summary"),
                "objectives": result.get("objectives", []),
                "steps": result.get("steps", []),
                "risks": result.get("risks", []),
                "done_when": result.get("done_when", []),
            },
            created_by_job_id=job_row["id"],
        )
        decision_id = create_blocking_decision(
            connection,
            job_row["project_id"],
            job_row["task_id"],
            f"Review plan draft for {task_title or 'queued task'}",
            {
                "action": "review_plan_draft",
                "artifact_id": artifact_id,
            },
            {},
            "plan_ready_for_review",
        )
        emit_event(
            connection,
            job_row["project_id"],
            "plan.created",
            "worker",
            "artifact",
            artifact_id,
            {"job_id": job_row["id"], "decision_id": decision_id},
        )
        return {"artifact_id": artifact_id, "decision_id": decision_id}

    def _task_title(self, connection: Connection, task_id: Optional[str]) -> Optional[str]:
        if not task_id:
            return None
        return connection.execute(
            text("SELECT title FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).scalar_one_or_none()

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _requires_reservation(self, kind: str, payload: dict[str, Any]) -> bool:
        spendy_kinds = {
            "openai.plan",
            "github.pull_request",
            "software_factory.propose_pr",
        }
        return bool(payload.get("requires_reservation")) or kind in spendy_kinds

    def _reservation_id_from_payload(self, payload: dict[str, Any]) -> Optional[str]:
        reservation_id = payload.get("reservation_id") or payload.get(
            "budget_reservation_id"
        )
        return str(reservation_id) if reservation_id else None

    def _cancel_budget_blocked_job(
        self,
        connection: Connection,
        project_id: str,
        job_id: str,
        task_id: Optional[str],
        kind: str,
        reservation_id: Optional[str],
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "last_error": json.dumps(
                    {
                        "message": "Missing active reservation for spend-gated job.",
                        "reservation_id": reservation_id,
                    }
                ),
                "updated_at": now_utc(),
            },
        )
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Budget blocked spend-gated job: {kind}",
            {"action": "create_budget_reservation"},
            {"reservation_id": reservation_id},
            "budget_missing_for_job",
        )
        emit_event(
            connection,
            project_id,
            "job.cancelled_budget_blocked",
            "budget_governor",
            "job",
            job_id,
            {"decision_id": decision_id, "reservation_id": reservation_id},
        )
