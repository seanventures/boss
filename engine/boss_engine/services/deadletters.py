from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    make_id,
    now_utc,
)
from .jobs import JobLeaseService

# Operator-facing error taxonomy for open deadletters (factory / worker reliability).
# Kept coarse on purpose: DLQ triage buckets, not a full exception hierarchy.
DEADLETTER_ERROR_CLASSES = ("git", "sandbox", "model", "github", "other")

# Ordered rules: first match wins. Patterns are substring / regex against the combined
# reason + payload text (lowercased).
_CLASS_RULES: list[tuple[str, tuple[str, ...]]] = (
    (
        "git",
        (
            r"\bgit\b",
            r"no such file or directory: ['\"]?git",
            r"missing.?git",
            r"not a git repository",
            r"git.?clone",
            r"failed to clone",
            r"could not read from remote repository",
            r"permission denied \(publickey\)",
            r"index\.lock",
            r"unable to access ['\"]?https?://.*\.git",
        ),
    ),
    (
        "sandbox",
        (
            r"sandbox",
            r"micro.?vm",
            r"not allowlisted",
            r"allowlist",
            r"rootfs",
            r"sandbox.?host",
            r"sandbox.?task",
            r"exec.?rejected",
            r"container.?runtime",
            r"firecracker",
            r"substrate",
        ),
    ),
    (
        "model",
        (
            r"\btpm\b",
            r"tokens? per minute",
            r"rate.?limit",
            r"ratelimit",
            r"\b429\b",
            r"too many requests",
            r"usage.?limit",
            r"spend.?limit",
            r"credits?\b",
            r"quota",
            r"resource.?exhausted",
            r"overloaded",
            r"capacity",
            r"throttl",
            r"budget.?reserv",
            r"budget.?denied",
            r"budget.?exhaust",
            r"max.?turns",
            r"error_max_turns",
            r"model.?gateway",
            r"openai",
            r"anthropic",
            r"openrouter",
            r"groq",
        ),
    ),
    (
        "github",
        (
            r"github",
            r"pull.?request",
            r"\bpr\b",
            r"gh api",
            r"api\.github",
            r"installation.?token",
            r"github.?app",
            r"secondary rate",
            r"abuse.?detection",
            r"repository.?not.?found",
            r"bad credentials",
            r"requires authentication",
            r"resource not accessible by integration",
        ),
    ),
)

# Model capacity subclasses that must escalate to a Decision (not endless requeue/DLQ-only).
_MODEL_ESCALATION_SIGNS = (
    r"\btpm\b",
    r"tokens? per minute",
    r"rate.?limit",
    r"ratelimit",
    r"\b429\b",
    r"too many requests",
    r"usage.?limit",
    r"spend.?limit",
    r"credit",
    r"quota",
    r"resource.?exhausted",
    r"budget.?reserv",
    r"budget.?denied",
    r"budget.?exhaust",
)


def classify_deadletter_error(
    reason: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    job_kind: Optional[str] = None,
    last_error: Optional[str] = None,
) -> str:
    """Map deadletter text into one of DEADLETTER_ERROR_CLASSES.

    Pure / deterministic: used by the classify API and by unit tests without a DB.
    """
    chunks: list[str] = [str(reason or "")]
    if last_error:
        chunks.append(str(last_error))
    if job_kind:
        chunks.append(str(job_kind))
    if isinstance(payload, dict):
        for key in ("message", "error", "type", "reason", "detail", "summary"):
            val = payload.get(key)
            if val is not None:
                chunks.append(str(val))
        # Nested error dicts from worker fail payloads.
        err = payload.get("error")
        if isinstance(err, dict):
            for key in ("message", "type", "code"):
                if err.get(key) is not None:
                    chunks.append(str(err[key]))
    text_blob = " ".join(chunks).lower()
    for error_class, patterns in _CLASS_RULES:
        for pat in patterns:
            if re.search(pat, text_blob, flags=re.IGNORECASE):
                return error_class
    return "other"


def is_model_capacity_exhaustion(reason: str, payload: Optional[dict[str, Any]] = None) -> bool:
    """True when the failure is model TPM / rate limit / credits / budget exhaustion.

    These must surface as a Decision (or structured class) rather than silent retry forever.
    """
    if classify_deadletter_error(reason, payload) != "model":
        return False
    blob = f"{reason} {json.dumps(payload or {}, default=str)}".lower()
    return any(re.search(pat, blob, flags=re.IGNORECASE) for pat in _MODEL_ESCALATION_SIGNS)


class DeadletterRemediationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)

    def classify_open_deadletters(
        self,
        connection: Connection,
        *,
        project_id: str,
        limit: int = 200,
        escalate_model: bool = True,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Classify open deadletters by error class for operator triage.

        Returns counts by class, per-item classification, and optional Decision ids
        created for model TPM/budget exhaustion rows that lack remediation_decision_id.
        """
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        rows = connection.execute(
            text(
                """
                SELECT d.id, d.project_id, d.source_kind, d.source_id, d.reason,
                       d.payload, d.status, d.remediation_decision_id,
                       d.incident_task_id, d.created_at, d.updated_at,
                       j.kind AS job_kind, j.last_error AS job_last_error
                FROM deadletters d
                LEFT JOIN jobs j
                  ON d.source_kind = 'job' AND d.source_id = j.id
                WHERE d.project_id = :project_id
                  AND d.status = 'open'
                ORDER BY d.created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()

        by_class: dict[str, int] = {c: 0 for c in DEADLETTER_ERROR_CLASSES}
        items: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []

        for row in rows:
            payload = row["payload"] or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {"raw": payload}
            error_class = classify_deadletter_error(
                row["reason"] or "",
                payload if isinstance(payload, dict) else {},
                job_kind=row.get("job_kind"),
                last_error=row.get("job_last_error"),
            )
            by_class[error_class] = by_class.get(error_class, 0) + 1
            decision_id = row.get("remediation_decision_id")
            escalated = False
            if (
                escalate_model
                and error_class == "model"
                and not decision_id
                and is_model_capacity_exhaustion(row["reason"] or "", payload if isinstance(payload, dict) else {})
            ):
                decision_id = self._escalate_model_capacity(
                    connection,
                    project_id=project_id,
                    deadletter_id=row["id"],
                    reason=row["reason"] or "",
                    payload=payload if isinstance(payload, dict) else {},
                    actor=actor,
                    source_kind=row["source_kind"],
                    source_id=row["source_id"],
                )
                escalated = True
                escalations.append(
                    {
                        "deadletter_id": row["id"],
                        "decision_id": decision_id,
                        "reason": row["reason"],
                    }
                )
            items.append(
                {
                    "id": row["id"],
                    "source_kind": row["source_kind"],
                    "source_id": row["source_id"],
                    "reason": row["reason"],
                    "error_class": error_class,
                    "status": row["status"],
                    "job_kind": row.get("job_kind"),
                    "remediation_decision_id": decision_id,
                    "escalated_to_decision": escalated,
                    "created_at": row["created_at"].isoformat()
                    if hasattr(row["created_at"], "isoformat")
                    else row["created_at"],
                }
            )

        return {
            "project_id": project_id,
            "total_open": len(items),
            "by_class": by_class,
            "items": items,
            "escalations": escalations,
            "error_classes": list(DEADLETTER_ERROR_CLASSES),
        }

    def _escalate_model_capacity(
        self,
        connection: Connection,
        *,
        project_id: str,
        deadletter_id: str,
        reason: str,
        payload: dict[str, Any],
        actor: str,
        source_kind: str,
        source_id: str,
    ) -> str:
        """Create a hold-default Decision for model TPM/budget exhaustion and link it."""
        task_id = None
        if source_kind == "job":
            row = connection.execute(
                text("SELECT task_id FROM jobs WHERE id = :id AND project_id = :p"),
                {"id": source_id, "p": project_id},
            ).mappings().first()
            if row:
                task_id = row.get("task_id")
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            (
                "Model capacity exhausted (TPM / rate limit / credits / budget). "
                "Work stopped rather than retrying forever — raise limits, wait for "
                "quota reset, or requeue after capacity is available."
            ),
            {
                "action": "restore_model_capacity_or_raise_budget",
                "deadletter_id": deadletter_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "failure_reason": (reason or "")[:500],
                "error_class": "model",
            },
            {},
            "model_capacity_exhausted",
        )
        connection.execute(
            text(
                """
                UPDATE deadletters
                SET remediation_decision_id = :decision_id,
                    updated_at = now()
                WHERE id = :deadletter_id
                  AND remediation_decision_id IS NULL
                """
            ),
            {"decision_id": decision_id, "deadletter_id": deadletter_id},
        )
        emit_event(
            connection,
            project_id,
            "deadletter.model_capacity_escalated",
            actor,
            "deadletter",
            deadletter_id,
            {
                "decision_id": decision_id,
                "reason": (reason or "")[:300],
                "error_class": "model",
            },
        )
        return decision_id

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
