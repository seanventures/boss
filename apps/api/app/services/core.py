from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings


DATA_BOUNDARIES = {"public", "internal", "confidential", "restricted", "local-only"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def json_hash(payload: Union[dict, list, str]) -> str:
    normalized = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def normalize_data_boundary(value: Optional[str] = None) -> str:
    boundary = (value or "internal").strip().lower()
    if boundary not in DATA_BOUNDARIES:
        raise ValueError(
            "data_boundary must be one of: "
            + ", ".join(sorted(DATA_BOUNDARIES))
        )
    return boundary


def emit_event(
    connection: Connection,
    project_id: str,
    event_type: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    data: dict,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data)
            VALUES (:id, :project_id, :event_type, :actor, :entity_type, :entity_id, CAST(:data AS JSONB))
            """
        ),
        {
            "id": make_id("evt"),
            "project_id": project_id,
            "event_type": event_type,
            "actor": actor,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": json.dumps(data),
        },
    )


def record_policy_eval(
    connection: Connection,
    project_id: str,
    boundary: str,
    decision: str,
    *,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[dict] = None,
) -> str:
    """Append a durable record of a policy evaluation at any enforcement boundary (spec §13:
    policy_eval_log). `decision` is allow | deny | defer. This is the audit trail behind
    'policy violation attempts (tracked and blocked)' and replay/'why did we do that'."""
    eval_id = make_id("pev")
    connection.execute(
        text(
            """
            INSERT INTO policy_eval_log (id, project_id, boundary, action, decision, reason, detail)
            VALUES (:id, :p, :b, :a, :d, :r, CAST(:det AS JSONB))
            """
        ),
        {"id": eval_id, "p": project_id, "b": boundary, "a": action, "d": decision,
         "r": reason, "det": json.dumps(detail or {})},
    )
    return eval_id


def create_blocking_decision(
    connection: Connection,
    project_id: str,
    task_id: Optional[str],
    question: str,
    recommendation: dict,
    cost_summary: dict,
    reason: str,
    *,
    risk_tier: int = 2,
    default_action: str = "hold",
) -> str:
    decision_id = make_id("dec")
    deadline_at = now_utc() + timedelta(hours=24)
    connection.execute(
        text(
            """
            INSERT INTO decisions (
                id, project_id, task_id, question, recommendation, options, risk_tier,
                cost_summary, deadline_at, default_action, status
            )
            VALUES (
                :id, :project_id, :task_id, :question, CAST(:recommendation AS JSONB),
                CAST(:options AS JSONB), :risk_tier, CAST(:cost_summary AS JSONB), :deadline_at,
                :default_action, 'pending'
            )
            """
        ),
        {
            "id": decision_id,
            "project_id": project_id,
            "task_id": task_id,
            "question": question,
            "recommendation": json.dumps(recommendation),
            "options": json.dumps(
                [
                    {"action": "approve", "label": "Approve and retry"},
                    {"action": "reject", "label": "Keep blocked"},
                ]
            ),
            "risk_tier": risk_tier,
            "cost_summary": json.dumps(cost_summary),
            "deadline_at": deadline_at,
            "default_action": default_action,
        },
    )
    emit_event(
        connection,
        project_id,
        "decision.created",
        "system",
        "decision",
        decision_id,
        {
            "reason": reason,
            "deadline_at": deadline_at.isoformat(),
            "default_action": default_action,
            "risk_tier": risk_tier,
        },
    )
    return decision_id


def create_artifact(
    connection: Connection,
    settings: Settings,
    project_id: str,
    task_id: Optional[str],
    artifact_type: str,
    title: str,
    content: dict,
    created_by_job_id: Optional[str] = None,
    data_boundary: Optional[str] = None,
) -> str:
    boundary_source = data_boundary or content.get("data_boundary")
    if not boundary_source and task_id:
        row = connection.execute(
            text("SELECT data_boundary FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).mappings().first()
        boundary_source = row["data_boundary"] if row else None
    boundary = normalize_data_boundary(boundary_source)
    artifact_id = make_id("art")
    artifact_dir = settings.artifact_root / project_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_id}.json"
    artifact_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    connection.execute(
        text(
            """
            INSERT INTO artifacts (
                id, project_id, task_id, artifact_type, title, uri, content,
                content_hash, created_by_job_id, data_boundary
            )
            VALUES (
                :id, :project_id, :task_id, :artifact_type, :title, :uri,
                CAST(:content AS JSONB), :content_hash, :created_by_job_id,
                :data_boundary
            )
            """
        ),
        {
            "id": artifact_id,
            "project_id": project_id,
            "task_id": task_id,
            "artifact_type": artifact_type,
            "title": title,
            "uri": str(artifact_path),
            "content": json.dumps(content),
            "content_hash": json_hash(content),
            "created_by_job_id": created_by_job_id,
            "data_boundary": boundary,
        },
    )
    emit_event(
        connection,
        project_id,
        "artifact.created",
        "system",
        "artifact",
        artifact_id,
        {"artifact_type": artifact_type, "title": title, "data_boundary": boundary},
    )
    return artifact_id


def expires_at(minutes: int) -> datetime:
    return now_utc() + timedelta(minutes=minutes)
