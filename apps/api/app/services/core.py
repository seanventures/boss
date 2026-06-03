from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def json_hash(payload: Union[dict, list, str]) -> str:
    normalized = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


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


def create_blocking_decision(
    connection: Connection,
    project_id: str,
    task_id: Optional[str],
    question: str,
    recommendation: dict,
    cost_summary: dict,
    reason: str,
) -> str:
    decision_id = make_id("dec")
    connection.execute(
        text(
            """
            INSERT INTO decisions (
                id, project_id, task_id, question, recommendation, options, risk_tier,
                cost_summary, default_action, status
            )
            VALUES (
                :id, :project_id, :task_id, :question, CAST(:recommendation AS JSONB),
                CAST(:options AS JSONB), 2, CAST(:cost_summary AS JSONB), 'hold', 'pending'
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
            "cost_summary": json.dumps(cost_summary),
        },
    )
    emit_event(
        connection,
        project_id,
        "decision.created",
        "system",
        "decision",
        decision_id,
        {"reason": reason},
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
) -> str:
    artifact_id = make_id("art")
    artifact_dir = settings.artifact_root / project_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_id}.json"
    artifact_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    connection.execute(
        text(
            """
            INSERT INTO artifacts (
                id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
            )
            VALUES (
                :id, :project_id, :task_id, :artifact_type, :title, :uri,
                CAST(:content AS JSONB), :content_hash, :created_by_job_id
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
        },
    )
    emit_event(
        connection,
        project_id,
        "artifact.created",
        "system",
        "artifact",
        artifact_id,
        {"artifact_type": artifact_type, "title": title},
    )
    return artifact_id


def expires_at(minutes: int) -> datetime:
    return now_utc() + timedelta(minutes=minutes)
