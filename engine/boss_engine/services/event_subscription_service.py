from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .core import emit_event, make_id, normalize_data_boundary


EVENT_TYPE_PATTERN = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*(?:\.\*)?$")
SINK_KINDS = {"job", "webhook_out"}

DEFAULT_EVENT_SUBSCRIPTIONS = (
    {
        "event_type": "code_task.completed",
        "sink_kind": "job",
        "sink_ref": "docs.refresh",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "code_task.pr_opened",
        "sink_kind": "job",
        "sink_ref": "docs.update",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "software_factory.pull_request_merged",
        "sink_kind": "job",
        "sink_ref": "docs.update",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "discovery.completed",
        "sink_kind": "job",
        "sink_ref": "docs.update",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "plan.dispatched",
        "sink_kind": "job",
        "sink_ref": "agent.orchestrator.next_move",
        "data_boundary": "internal",
        "config": {"consumer": "orchestration"},
    },
    {
        "event_type": "webhook.inbound.received",
        "sink_kind": "job",
        "sink_ref": "github.triage",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.issue_opened",
        "sink_kind": "job",
        "sink_ref": "github.triage",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.issue_comment",
        "sink_kind": "job",
        "sink_ref": "github.triage",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.pr_opened",
        "sink_kind": "job",
        "sink_ref": "github.pr_review",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.pr_review",
        "sink_kind": "job",
        "sink_ref": "github.pr_review",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
)


def validate_event_type(value: str) -> str:
    event_type = value.strip().lower()
    if not EVENT_TYPE_PATTERN.fullmatch(event_type):
        raise ValueError(
            "event_type must be an exact lowercase type or a suffix glob such as code_task.*."
        )
    return event_type


def validate_sink_kind(value: str) -> str:
    sink_kind = value.strip().lower()
    if sink_kind not in SINK_KINDS:
        raise ValueError("sink_kind must be one of: job, webhook_out.")
    return sink_kind


def event_type_matches(pattern: str, event_type: str) -> bool:
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return pattern == event_type


def _row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "event_type": row["event_type"],
        "sink_kind": row["sink_kind"],
        "sink_ref": row["sink_ref"],
        "data_boundary": row["data_boundary"],
        "enabled": bool(row["enabled"]),
        "config": row["config"] or {},
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else row["created_at"],
        "updated_at": row["updated_at"].isoformat()
        if hasattr(row["updated_at"], "isoformat")
        else row["updated_at"],
    }


def _invalidate(project_id: str) -> None:
    # Local import prevents the CRUD service and dispatcher from forming an
    # import cycle during application startup.
    from .event_dispatch import invalidate_subscription_cache

    invalidate_subscription_cache(project_id)


def list_event_subscriptions(
    connection: Connection,
    project_id: str,
    *,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, project_id, event_type, sink_kind, sink_ref, data_boundary,
               enabled, config, created_at, updated_at
        FROM event_subscriptions
        WHERE project_id = :project_id
    """
    if enabled_only:
        query += " AND enabled = true"
    query += " ORDER BY event_type, sink_kind, sink_ref"
    rows = connection.execute(
        text(query), {"project_id": project_id}
    ).mappings().all()
    return [_row(row) for row in rows]


def create_event_subscription(
    connection: Connection,
    project_id: str,
    event_type: str,
    sink_kind: str,
    sink_ref: str,
    *,
    data_boundary: str = "internal",
    enabled: bool = True,
    config: Optional[dict[str, Any]] = None,
    actor: str = "system",
) -> dict[str, Any]:
    normalized_event_type = validate_event_type(event_type)
    normalized_sink_kind = validate_sink_kind(sink_kind)
    normalized_boundary = normalize_data_boundary(data_boundary)
    if not sink_ref.strip():
        raise ValueError("sink_ref must not be empty.")
    subscription_id = make_id("sub")
    try:
        row = connection.execute(
            text(
                """
                INSERT INTO event_subscriptions (
                    id, project_id, event_type, sink_kind, sink_ref,
                    data_boundary, enabled, config
                )
                VALUES (
                    :id, :project_id, :event_type, :sink_kind, :sink_ref,
                    :data_boundary, :enabled, CAST(:config AS JSONB)
                )
                RETURNING id, project_id, event_type, sink_kind, sink_ref,
                          data_boundary, enabled, config, created_at, updated_at
                """
            ),
            {
                "id": subscription_id,
                "project_id": project_id,
                "event_type": normalized_event_type,
                "sink_kind": normalized_sink_kind,
                "sink_ref": sink_ref.strip(),
                "data_boundary": normalized_boundary,
                "enabled": bool(enabled),
                "config": json.dumps(config or {}),
            },
        ).mappings().one()
    except IntegrityError as exc:
        raise ValueError(
            "The event subscription conflicts with an existing subscription or project."
        ) from exc
    _invalidate(project_id)
    emit_event(
        connection,
        project_id,
        "event_subscription.created",
        actor,
        "event_subscription",
        subscription_id,
        {
            "event_type": normalized_event_type,
            "sink_kind": normalized_sink_kind,
            "sink_ref": sink_ref.strip(),
            "enabled": bool(enabled),
        },
    )
    return _row(row)


def update_event_subscription(
    connection: Connection,
    project_id: str,
    subscription_id: str,
    *,
    event_type: Optional[str] = None,
    sink_kind: Optional[str] = None,
    sink_ref: Optional[str] = None,
    data_boundary: Optional[str] = None,
    enabled: Optional[bool] = None,
    config: Optional[dict[str, Any]] = None,
    actor: str = "system",
) -> Optional[dict[str, Any]]:
    assignments: list[str] = []
    params: dict[str, Any] = {
        "project_id": project_id,
        "subscription_id": subscription_id,
    }
    if event_type is not None:
        assignments.append("event_type = :event_type")
        params["event_type"] = validate_event_type(event_type)
    if sink_kind is not None:
        assignments.append("sink_kind = :sink_kind")
        params["sink_kind"] = validate_sink_kind(sink_kind)
    if sink_ref is not None:
        if not sink_ref.strip():
            raise ValueError("sink_ref must not be empty.")
        assignments.append("sink_ref = :sink_ref")
        params["sink_ref"] = sink_ref.strip()
    if data_boundary is not None:
        assignments.append("data_boundary = :data_boundary")
        params["data_boundary"] = normalize_data_boundary(data_boundary)
    if enabled is not None:
        assignments.append("enabled = :enabled")
        params["enabled"] = bool(enabled)
    if config is not None:
        assignments.append("config = CAST(:config AS JSONB)")
        params["config"] = json.dumps(config)

    if assignments:
        changed_fields = [item.split(" =", 1)[0] for item in assignments]
        assignments.append("updated_at = now()")
        query = f"""
            UPDATE event_subscriptions
            SET {', '.join(assignments)}
            WHERE id = :subscription_id AND project_id = :project_id
            RETURNING id, project_id, event_type, sink_kind, sink_ref,
                      data_boundary, enabled, config, created_at, updated_at
        """
        try:
            row = connection.execute(text(query), params).mappings().first()
        except IntegrityError as exc:
            raise ValueError(
                "The event subscription update conflicts with an existing subscription."
            ) from exc
    else:
        changed_fields = []
        row = connection.execute(
            text(
                """
                SELECT id, project_id, event_type, sink_kind, sink_ref,
                       data_boundary, enabled, config, created_at, updated_at
                FROM event_subscriptions
                WHERE id = :subscription_id AND project_id = :project_id
                """
            ),
            params,
        ).mappings().first()
    if row is None:
        return None
    _invalidate(project_id)
    emit_event(
        connection,
        project_id,
        "event_subscription.updated",
        actor,
        "event_subscription",
        subscription_id,
        {"changed_fields": changed_fields},
    )
    return _row(row)


def delete_event_subscription(
    connection: Connection,
    project_id: str,
    subscription_id: str,
    *,
    actor: str = "system",
) -> bool:
    row = connection.execute(
        text(
            """
            DELETE FROM event_subscriptions
            WHERE id = :subscription_id AND project_id = :project_id
            RETURNING id
            """
        ),
        {"subscription_id": subscription_id, "project_id": project_id},
    ).mappings().first()
    if row is None:
        return False
    _invalidate(project_id)
    emit_event(
        connection,
        project_id,
        "event_subscription.deleted",
        actor,
        "event_subscription",
        subscription_id,
        {},
    )
    return True


def ensure_event_subscription(
    connection: Connection,
    project_id: str,
    event_type: str,
    sink_kind: str,
    sink_ref: str,
    *,
    data_boundary: str = "internal",
    enabled: bool = True,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            INSERT INTO event_subscriptions (
                id, project_id, event_type, sink_kind, sink_ref,
                data_boundary, enabled, config
            )
            VALUES (
                :id, :project_id, :event_type, :sink_kind, :sink_ref,
                :data_boundary, :enabled, CAST(:config AS JSONB)
            )
            ON CONFLICT (project_id, event_type, sink_kind, sink_ref)
            DO UPDATE SET
                data_boundary = EXCLUDED.data_boundary,
                enabled = EXCLUDED.enabled,
                config = EXCLUDED.config,
                updated_at = now()
            RETURNING id, project_id, event_type, sink_kind, sink_ref,
                      data_boundary, enabled, config, created_at, updated_at
            """
        ),
        {
            "id": make_id("sub"),
            "project_id": project_id,
            "event_type": validate_event_type(event_type),
            "sink_kind": validate_sink_kind(sink_kind),
            "sink_ref": sink_ref.strip(),
            "data_boundary": normalize_data_boundary(data_boundary),
            "enabled": bool(enabled),
            "config": json.dumps(config or {}),
        },
    ).mappings().one()
    _invalidate(project_id)
    return _row(row)


def seed_default_event_subscriptions(
    connection: Connection, project_id: str
) -> list[dict[str, Any]]:
    return [
        ensure_event_subscription(connection, project_id, **subscription)
        for subscription in DEFAULT_EVENT_SUBSCRIPTIONS
    ]
