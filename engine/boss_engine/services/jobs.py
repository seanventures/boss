from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_runtime import AgentRuntimeService
from .budget import BudgetGovernor
from .comms import CommsService
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    make_id,
    now_utc,
    normalize_data_boundary,
)


class LeaseNotFoundError(RuntimeError):
    pass


def resolve_job_data_boundary(
    connection: Connection,
    task_id: Optional[str],
    payload: dict[str, Any],
    data_boundary: Optional[str],
) -> str:
    """Resolve a job boundary without constructing the budget-aware lease service."""
    if data_boundary:
        return normalize_data_boundary(data_boundary)
    payload_boundary = payload.get("data_boundary")
    if isinstance(payload_boundary, str) and payload_boundary.strip():
        return normalize_data_boundary(payload_boundary)
    if task_id:
        row = connection.execute(
            text("SELECT data_boundary FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).mappings().first()
        if row:
            return normalize_data_boundary(row["data_boundary"])
    return "internal"


def enqueue_job(
    connection: Connection,
    project_id: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    task_id: Optional[str] = None,
    priority: int = 100,
    max_attempts: int = 3,
    run_after_seconds: int = 0,
    data_boundary: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a queued job without importing budget, entitlement, or agent services.

    A non-null ``dedupe_key`` makes enqueue idempotent within a project.  Budget
    and reservation enforcement intentionally remains at lease time.
    """
    job_payload = payload or {}
    boundary = resolve_job_data_boundary(
        connection, task_id, job_payload, data_boundary
    )
    job_id = make_id("job")
    run_after = now_utc() + timedelta(seconds=run_after_seconds)
    row = connection.execute(
        text(
            """
            INSERT INTO jobs (
                id, project_id, task_id, kind, priority, status, payload,
                data_boundary, max_attempts, attempt, run_after, dedupe_key
            )
            VALUES (
                :id, :project_id, :task_id, :kind, :priority, 'queued',
                CAST(:payload AS JSONB), :data_boundary, :max_attempts, 0,
                :run_after, :dedupe_key
            )
            ON CONFLICT (project_id, dedupe_key)
              WHERE dedupe_key IS NOT NULL
            DO NOTHING
            RETURNING id, project_id, task_id, kind, status, data_boundary,
                      priority, max_attempts, attempt, run_after, dedupe_key
            """
        ),
        {
            "id": job_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": kind,
            "priority": priority,
            "payload": json.dumps(job_payload),
            "data_boundary": boundary,
            "max_attempts": max_attempts,
            "run_after": run_after,
            "dedupe_key": dedupe_key,
        },
    ).mappings().first()
    created = row is not None
    if row is None:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, kind, status, data_boundary,
                       priority, max_attempts, attempt, run_after, dedupe_key
                FROM jobs
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).mappings().first()
    if row is None:
        raise RuntimeError("Job enqueue did not create or find a deduplicated job.")

    if created:
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            row["id"],
            {
                "kind": kind,
                "task_id": task_id,
                "data_boundary": boundary,
                "dedupe_key": dedupe_key,
            },
        )
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "status": row["status"],
        "data_boundary": row["data_boundary"],
        "priority": int(row["priority"]),
        "max_attempts": int(row["max_attempts"]),
        "attempt": int(row["attempt"]),
        "run_after": row["run_after"].isoformat(),
        "dedupe_key": row["dedupe_key"],
        "created": created,
    }


class JobLeaseService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.comms = CommsService(settings)
        self.agent_runtime = AgentRuntimeService(settings)

    def list_jobs(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        task_id: Optional[str] = None,
        include_terminal: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        if offset < 0:
            raise ValueError("offset must be >= 0.")
        filters = ["j.project_id = :project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": limit,
            "offset": offset,
        }
        if status:
            filters.append("j.status = :status")
            params["status"] = status
        if kind:
            filters.append("j.kind = :kind")
            params["kind"] = kind
        if task_id:
            filters.append("j.task_id = :task_id")
            params["task_id"] = task_id
        if not include_terminal:
            filters.append(
                "j.status NOT IN ('succeeded', 'cancelled', 'deadlettered')"
            )
        where_clause = " AND ".join(filters)
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
                WHERE {where_clause}
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
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_job_row(row) for row in rows]

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
        data_boundary: Optional[str] = None,
    ) -> dict[str, Any]:
        job = enqueue_job(
            connection,
            project_id,
            kind,
            payload,
            task_id=task_id,
            priority=priority,
            max_attempts=max_attempts,
            run_after_seconds=run_after_seconds,
            data_boundary=data_boundary,
        )
        # Preserve the public return shape used before enqueue_job was
        # extracted.  Dispatcher-only metadata belongs on the free function.
        job.pop("dedupe_key")
        job.pop("created")
        return job

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
            SELECT id, project_id, task_id, kind, payload, data_boundary, attempt,
                   max_attempts, status
            FROM jobs
            WHERE kind IN :capabilities
              AND run_after <= :now_utc
              AND (
                status IN ('queued', 'failed_retryable')
                -- reclaim leased OR running jobs whose lease expired: a worker that
                -- crashed mid-job leaves it 'running', and it must be recoverable.
                OR (status IN ('leased', 'running') AND lease_expires_at <= :now_utc)
              )
            ORDER BY
              CASE status
                WHEN 'leased' THEN 0
                WHEN 'running' THEN 0
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
            budget_policy = self._budget_state_policy_for_job(
                connection,
                candidate["project_id"],
                candidate["task_id"],
                candidate["id"],
                candidate["kind"],
            )
            if budget_policy is not None:
                self._defer_for_budget_state(
                    connection,
                    candidate["project_id"],
                    candidate["task_id"],
                    candidate["id"],
                    candidate["kind"],
                    budget_policy,
                )
                continue
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
                        data_boundary,
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
            if row["kind"] == "agent.studio.execute":
                connection.execute(
                    text(
                        "UPDATE agent_studio_runs SET status='leased', "
                        "updated_at=:updated_at WHERE job_id=:job_id"
                    ),
                    {
                        "job_id": row["id"],
                        "updated_at": current_time,
                    },
                )
            leased_payload = self.agent_runtime.attach_to_payload(
                connection,
                row["project_id"],
                job_kind=row["kind"],
                payload=row["payload"] or {},
            )
            return {
                "id": row["id"],
                "project_id": row["project_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "payload": leased_payload,
                "data_boundary": row["data_boundary"],
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
        if row["kind"] == "agent.studio.execute":
            connection.execute(
                text(
                    "UPDATE agent_studio_runs SET status=:status, "
                    "updated_at=:updated_at WHERE job_id=:job_id"
                ),
                {
                    "status": next_status,
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
            self._mark_related_message_retrying(
                connection, row, retry_delay_seconds
            )
            self._mark_agent_system_failure(
                connection, row, error, terminal=False, actor=worker_id
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
        self._mark_related_message_deadlettered(connection, row, deadletter_id)
        self._mark_agent_system_failure(
            connection, row, error, terminal=True, actor=worker_id
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
                       data_boundary, lease_owner, lease_token_hash, lease_expires_at
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

    def require_active_lease(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        """Validate a worker lease before a side-effecting job endpoint runs."""
        return dict(
            self._load_leased_job_for_update(
                connection, worker_id, job_id, lease_token
            )
        )

    def _finalize_success(
        self,
        connection: Connection,
        job_row,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if job_row["kind"] in {"openai.plan", "agent.resume"}:
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
                "security_review_artifact_id": result.get(
                    "security_review_artifact_id"
                ),
                "release_go_no_go_artifact_id": result.get(
                    "release_go_no_go_artifact_id"
                ),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
                "validation_status": result.get("validation_status"),
                "validation_summary": result.get("validation_summary"),
                "validation_sources": result.get("validation_sources") or [],
                "local_validation_receipt_id": result.get(
                    "local_validation_receipt_id"
                ),
            }

        if job_row["kind"] == "docs.update":
            return {
                "receipt_id": result.get("receipt_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
                "feature_id": result.get("feature_id"),
                "doc_path": result.get("doc_path"),
            }

        if job_row["kind"] == "github.pull_request.merge":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "deployment_decision_id": result.get("deployment_decision_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
                "merged_sha": result.get("merged_sha"),
            }

        if job_row["kind"] == "github.deployment.create":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "environment": result.get("environment"),
                "deployment_id": result.get("deployment_id"),
                "deployment_status": result.get("deployment_status"),
                "deployment_url": result.get("deployment_url"),
                "merged_sha": result.get("merged_sha"),
                "rollout_id": result.get("rollout_id"),
                "rollout_artifact_id": result.get("rollout_artifact_id"),
                "rollout_status": result.get("rollout_status"),
                "rollout_stage": result.get("rollout_stage"),
            }

        if job_row["kind"] == "comms.outbox.deliver":
            return {
                "receipt_id": result.get("receipt_id"),
            }

        if job_row["kind"] == "a2a.dispatch":
            return {
                "receipt_id": result.get("receipt_id"),
                "message_id": result.get("id") or result.get("message_id"),
                "redis_stream_id": result.get("redis_stream_id"),
            }

        if job_row["kind"] == "infra.droplet.create":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "resource_id": result.get("resource_id"),
                "provider_ref": result.get("provider_ref"),
                "provider_resource_id": result.get("provider_resource_id"),
                "resource_name": result.get("resource_name"),
                "expires_at": result.get("expires_at"),
            }

        if job_row["kind"] == "infra.droplet.delete":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "resource_id": result.get("resource_id"),
                "provider_ref": result.get("provider_ref"),
            }

        if str(job_row["kind"]).startswith("agent."):
            payload = job_row["payload"] or {}
            agent_module = result.get("agent_module") or payload.get("agent_module") or {}
            role_key = (
                ((agent_module or {}).get("role_key"))
                or str(job_row["kind"]).split(".")[1]
            )
            artifact_id = create_artifact(
                connection,
                self.settings,
                job_row["project_id"],
                job_row["task_id"],
                "agent_brief",
                f"{role_key} brief for {job_row['kind']}",
                {
                    "job_kind": job_row["kind"],
                    "summary": result.get("summary"),
                    "findings": result.get("findings") or [],
                    "recommendations": result.get("recommendations") or [],
                    "required_decisions": result.get("required_decisions") or [],
                    "evidence_refs": result.get("evidence_refs") or [],
                    "strategy_id": result.get("strategy_id"),
                    "grounding_status": result.get("grounding_status"),
                    "work_product": result.get("work_product") or {},
                    "agent_module": agent_module,
                    "agent_runtime": result.get("agent_runtime")
                    or payload.get("agent_runtime")
                    or {},
                    "prompt_excerpt": result.get("prompt_excerpt"),
                },
                created_by_job_id=job_row["id"],
            )
            emit_event(
                connection,
                job_row["project_id"],
                "agent.brief_created",
                "worker",
                "artifact",
                artifact_id,
                {
                    "job_id": job_row["id"],
                    "job_kind": job_row["kind"],
                    "role_key": role_key,
                },
            )
            if job_row["kind"] == "agent.studio.execute":
                from .agent_studio import AgentStudioService

                AgentStudioService(self.settings).record_job_success(
                    connection,
                    project_id=job_row["project_id"],
                    job_id=job_row["id"],
                    result=result,
                    artifact_id=artifact_id,
                    actor="worker",
                )
            return {"artifact_id": artifact_id}

        if job_row["kind"] == "enterprise.marketplace.validate":
            payload = job_row["payload"] or {}
            module_id = result.get("module_id") or payload.get("module_id")
            artifact_id = create_artifact(
                connection,
                self.settings,
                job_row["project_id"],
                job_row["task_id"],
                "entitlement_gate_report",
                f"Marketplace entitlement validation for {module_id or 'module'}",
                {
                    "job_kind": job_row["kind"],
                    "summary": result.get("summary"),
                    "status": result.get("status"),
                    "module_id": module_id,
                    "required_entitlement": result.get("required_entitlement"),
                    "entitlement_status": result.get("entitlement_status"),
                    "findings": result.get("findings") or [],
                    "recommendations": result.get("recommendations") or [],
                    "required_decisions": result.get("required_decisions") or [],
                    "evidence_refs": result.get("evidence_refs") or [],
                    "work_product": result.get("work_product") or {},
                    "agent_module": result.get("agent_module")
                    or payload.get("agent_module")
                    or {},
                    "agent_runtime": result.get("agent_runtime")
                    or payload.get("agent_runtime")
                    or {},
                },
                created_by_job_id=job_row["id"],
            )
            emit_event(
                connection,
                job_row["project_id"],
                "enterprise.marketplace_validated",
                "worker",
                "artifact",
                artifact_id,
                {
                    "job_id": job_row["id"],
                    "module_id": module_id,
                    "status": result.get("status"),
                },
            )
            return {"artifact_id": artifact_id}

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
                "agent_module": result.get("agent_module") or payload.get("agent_module") or {},
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

    def _mark_agent_system_failure(
        self,
        connection: Connection,
        job_row,
        error: dict[str, Any],
        *,
        terminal: bool,
        actor: str,
    ) -> None:
        if job_row["kind"] == "agent.studio.execute":
            from .agent_studio import AgentStudioService

            AgentStudioService(self.settings).record_job_failure(
                connection,
                project_id=job_row["project_id"],
                job_id=job_row["id"],
                error=error,
                terminal=terminal,
                actor=actor,
            )
        elif job_row["kind"] == "a2a.dispatch":
            from .a2a import A2AService

            A2AService(self.settings).mark_dispatch_failure(
                connection,
                project_id=job_row["project_id"],
                job_id=job_row["id"],
                error=error,
                terminal=terminal,
            )

    def _task_title(self, connection: Connection, task_id: Optional[str]) -> Optional[str]:
        if not task_id:
            return None
        return connection.execute(
            text("SELECT title FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).scalar_one_or_none()

    def _serialize_job_row(self, row) -> dict[str, Any]:
        last_error = row["last_error"] or {}
        if not isinstance(last_error, dict):
            last_error = {}
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "task_title": row.get("task_title"),
            "kind": row["kind"],
            "status": row["status"],
            "data_boundary": row.get("data_boundary") or "internal",
            "priority": row["priority"],
            "max_attempts": row["max_attempts"],
            "attempt": row["attempt"],
            "run_after": row["run_after"].isoformat(),
            "lease_owner": row.get("lease_owner"),
            "lease_expires_at": (
                row["lease_expires_at"].isoformat()
                if row.get("lease_expires_at")
                else None
            ),
            "heartbeat_at": (
                row["heartbeat_at"].isoformat() if row.get("heartbeat_at") else None
            ),
            "last_error": last_error,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _requires_reservation(self, kind: str, payload: dict[str, Any]) -> bool:
        spendy_kinds = {
            "openai.plan",
            "github.pull_request",
            "software_factory.propose_pr",
            "comms.outbox.deliver",
            "infra.droplet.create",
        }
        return bool(payload.get("requires_reservation")) or kind in spendy_kinds

    def _reservation_id_from_payload(self, payload: dict[str, Any]) -> Optional[str]:
        reservation_id = payload.get("reservation_id") or payload.get(
            "budget_reservation_id"
        )
        return str(reservation_id) if reservation_id else None

    def _budget_state_policy_for_job(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        job_id: str,
        kind: str,
    ) -> Optional[dict[str, Any]]:
        snapshot = self.budget.status_for_project(connection, project_id)
        budget_state = snapshot["budget_state"]
        if budget_state not in {"orange", "red"}:
            return None

        task_priority = "p2"
        risk_tier = 0
        if task_id:
            task_row = connection.execute(
                text(
                    """
                    SELECT priority, risk_tier
                    FROM tasks
                    WHERE id = :task_id
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
            if task_row:
                task_priority = str(task_row["priority"] or "p2").lower()
                risk_tier = int(task_row["risk_tier"] or 0)

        if budget_state == "orange" and task_priority not in {"p0", "p1"}:
            return {
                "budget_state": budget_state,
                "task_priority": task_priority,
                "risk_tier": risk_tier,
                "defer_seconds": 300,
                "reason": "orange_defers_non_critical_work",
            }

        high_impact_effects = {
            "github.pull_request.merge",
            "github.deployment.create",
            "comms.outbox.deliver",
        }
        if budget_state == "red":
            if kind == "infra.droplet.create":
                return {
                    "budget_state": budget_state,
                    "task_priority": task_priority,
                    "risk_tier": risk_tier,
                    "defer_seconds": 900,
                    "reason": "red_freezes_infra_provisioning",
                }
            if task_priority != "p0":
                return {
                    "budget_state": budget_state,
                    "task_priority": task_priority,
                    "risk_tier": risk_tier,
                    "defer_seconds": 900,
                    "reason": "red_halts_non_p0_work",
                }
            if kind in high_impact_effects and risk_tier >= 2:
                return {
                    "budget_state": budget_state,
                    "task_priority": task_priority,
                    "risk_tier": risk_tier,
                    "defer_seconds": 900,
                    "reason": "red_freezes_tier2_effects",
                }
        return None

    def _defer_for_budget_state(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        job_id: str,
        kind: str,
        policy: dict[str, Any],
    ) -> None:
        deferred_until = now_utc() + timedelta(seconds=int(policy["defer_seconds"]))
        message = (
            f"Budget state {policy['budget_state']} deferred job {kind} "
            f"(priority {policy['task_priority']}, risk {policy['risk_tier']})."
        )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'queued',
                    run_after = :run_after,
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "run_after": deferred_until,
                "last_error": json.dumps(
                    {
                        "message": message,
                        "budget_state": policy["budget_state"],
                        "reason": policy["reason"],
                    }
                ),
                "updated_at": now_utc(),
            },
        )
        decision_id = self._create_or_reuse_budget_policy_decision(
            connection,
            project_id,
            task_id,
            job_id,
            kind,
            policy,
        )
        emit_event(
            connection,
            project_id,
            "job.deferred_budget_policy",
            "budget_governor",
            "job",
            job_id,
            {
                "decision_id": decision_id,
                "budget_state": policy["budget_state"],
                "deferred_until": deferred_until.isoformat(),
                "reason": policy["reason"],
            },
        )

    def _create_or_reuse_budget_policy_decision(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        job_id: str,
        kind: str,
        policy: dict[str, Any],
    ) -> str:
        existing_id = connection.execute(
            text(
                """
                SELECT id
                FROM decisions
                WHERE project_id = :project_id
                  AND task_id IS NOT DISTINCT FROM :task_id
                  AND status IN ('pending', 'delegated', 'approved')
                  AND recommendation->>'action' = 'review_budget_policy'
                  AND cost_summary->>'job_id' = :job_id
                  AND cost_summary->>'budget_state' = :budget_state
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "job_id": job_id,
                "budget_state": policy["budget_state"],
            },
        ).scalar_one_or_none()
        if existing_id:
            return str(existing_id)

        return create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Budget state {policy['budget_state']} deferred job: {kind}",
            {
                "action": "review_budget_policy",
                "job_id": job_id,
                "job_kind": kind,
                "budget_state": policy["budget_state"],
                "reason": policy["reason"],
            },
            {
                "job_id": job_id,
                "job_kind": kind,
                "budget_state": policy["budget_state"],
                "task_priority": policy["task_priority"],
                "risk_tier": policy["risk_tier"],
            },
            "budget_state_deferred_job",
        )

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
        if kind == "comms.outbox.deliver":
            payload = connection.execute(
                text("SELECT payload FROM jobs WHERE id = :job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none() or {}
            outbox_message_id = payload.get("outbox_message_id")
            if outbox_message_id:
                self.comms.mark_outbox_deadlettered(
                    connection,
                    project_id,
                    str(outbox_message_id),
                    decision_id,
                )

    def _mark_related_message_retrying(
        self, connection: Connection, row, retry_delay_seconds: int
    ) -> None:
        if row["kind"] == "webhook.outbound.deliver":
            delivery_id = (row["payload"] or {}).get("delivery_id")
            if delivery_id:
                connection.execute(
                    text(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'retrying',
                            next_attempt_at = :next_attempt_at,
                            updated_at = :updated_at
                        WHERE id = :delivery_id AND project_id = :project_id
                        """
                    ),
                    {
                        "delivery_id": str(delivery_id),
                        "project_id": row["project_id"],
                        "next_attempt_at": now_utc()
                        + timedelta(seconds=retry_delay_seconds),
                        "updated_at": now_utc(),
                    },
                )
            return
        if row["kind"] != "comms.outbox.deliver":
            return
        payload = row["payload"] or {}
        outbox_message_id = payload.get("outbox_message_id")
        if not outbox_message_id:
            return
        self.comms.mark_outbox_retrying(
            connection,
            row["project_id"],
            str(outbox_message_id),
            retry_delay_seconds,
        )

    def _mark_related_message_deadlettered(
        self, connection: Connection, row, deadletter_id: str
    ) -> None:
        if row["kind"] == "webhook.outbound.deliver":
            delivery_id = (row["payload"] or {}).get("delivery_id")
            if delivery_id:
                connection.execute(
                    text(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'deadlettered', next_attempt_at = NULL,
                            last_error = COALESCE(last_error, '{}'::jsonb)
                                || CAST(:deadletter AS JSONB),
                            updated_at = :updated_at
                        WHERE id = :delivery_id AND project_id = :project_id
                        """
                    ),
                    {
                        "delivery_id": str(delivery_id),
                        "project_id": row["project_id"],
                        "deadletter": json.dumps({"deadletter_id": deadletter_id}),
                        "updated_at": now_utc(),
                    },
                )
            return
        if row["kind"] != "comms.outbox.deliver":
            return
        payload = row["payload"] or {}
        outbox_message_id = payload.get("outbox_message_id")
        if not outbox_message_id:
            return
        self.comms.mark_outbox_deadlettered(
            connection,
            row["project_id"],
            str(outbox_message_id),
            deadletter_id,
        )
