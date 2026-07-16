from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id


SKILL_SOURCES = {"system", "tenant", "module"}


def _row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "skill_id": row["skill_id"],
        "source": row["source"],
        "enabled": bool(row["enabled"]),
        "on_demand": bool(row["on_demand"]),
        "config": row["config"] or {},
        "publisher": row["publisher"] or {},
        "created_at": (
            row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat")
            else row["created_at"]
        ),
        "updated_at": (
            row["updated_at"].isoformat()
            if hasattr(row["updated_at"], "isoformat")
            else row["updated_at"]
        ),
    }


def _validate_source(source: str) -> str:
    if not isinstance(source, str):
        raise ValueError("source must be a string")
    normalized = source.strip().lower()
    if normalized not in SKILL_SOURCES:
        raise ValueError(
            f"source must be one of {', '.join(sorted(SKILL_SOURCES))}"
        )
    return normalized


def _validate_skill_id(skill_id: str) -> str:
    if not isinstance(skill_id, str) or not skill_id.strip():
        raise ValueError("skill_id must not be empty")
    return skill_id.strip()


def list_project_skills(
    connection: Connection,
    project_id: str,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, project_id, skill_id, source, enabled, on_demand, config,
               publisher, created_at, updated_at
        FROM project_skills
        WHERE project_id = :project_id
    """
    if enabled_only:
        query += " AND enabled = true"
    query += " ORDER BY source, skill_id"
    rows = connection.execute(
        text(query), {"project_id": project_id}
    ).mappings().all()
    return [_row(row) for row in rows]


def register_skill(
    connection: Connection,
    project_id: str,
    skill_id: str,
    *,
    source: str = "tenant",
    enabled: bool = True,
    on_demand: bool = False,
    config: Optional[dict[str, Any]] = None,
    publisher: Optional[dict[str, Any]] = None,
    actor: str = "system",
) -> dict[str, Any]:
    normalized_skill_id = _validate_skill_id(skill_id)
    normalized_source = _validate_source(source)
    row = connection.execute(
        text(
            """
            INSERT INTO project_skills (
                id, project_id, skill_id, source, enabled, on_demand,
                config, publisher
            )
            VALUES (
                :id, :project_id, :skill_id, :source, :enabled, :on_demand,
                CAST(:config AS JSONB), CAST(:publisher AS JSONB)
            )
            RETURNING id, project_id, skill_id, source, enabled, on_demand,
                      config, publisher, created_at, updated_at
            """
        ),
        {
            "id": make_id("psk"),
            "project_id": project_id,
            "skill_id": normalized_skill_id,
            "source": normalized_source,
            "enabled": bool(enabled),
            "on_demand": bool(on_demand),
            "config": json.dumps(config or {}),
            "publisher": json.dumps(publisher or {}),
        },
    ).mappings().one()
    item = _row(row)
    emit_event(
        connection,
        project_id,
        "skill.registered",
        actor,
        "project_skill",
        item["id"],
        {
            "skill_id": normalized_skill_id,
            "source": normalized_source,
            "enabled": bool(enabled),
            "on_demand": bool(on_demand),
        },
    )
    return item


def _set_skill_enabled(
    connection: Connection,
    project_id: str,
    skill_id: str,
    enabled: bool,
    actor: str,
) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            UPDATE project_skills
            SET enabled = :enabled, updated_at = now()
            WHERE project_id = :project_id AND skill_id = :skill_id
            RETURNING id, project_id, skill_id, source, enabled, on_demand,
                      config, publisher, created_at, updated_at
            """
        ),
        {
            "enabled": enabled,
            "project_id": project_id,
            "skill_id": skill_id,
        },
    ).mappings().one_or_none()
    if row is None:
        return None
    item = _row(row)
    emit_event(
        connection,
        project_id,
        "skill.enabled" if enabled else "skill.disabled",
        actor,
        "project_skill",
        item["id"],
        {"skill_id": skill_id, "enabled": enabled},
    )
    return item


def enable_skill(
    connection: Connection,
    project_id: str,
    skill_id: str,
    actor: str = "system",
) -> Optional[dict[str, Any]]:
    return _set_skill_enabled(
        connection, project_id, skill_id, True, actor
    )


def disable_skill(
    connection: Connection,
    project_id: str,
    skill_id: str,
    actor: str = "system",
) -> Optional[dict[str, Any]]:
    return _set_skill_enabled(
        connection, project_id, skill_id, False, actor
    )


def ensure_skill(
    connection: Connection,
    project_id: str,
    skill_id: str,
    *,
    source: str = "system",
    enabled: bool = True,
    on_demand: bool = False,
    config: Optional[dict[str, Any]] = None,
    publisher: Optional[dict[str, Any]] = None,
    actor: str = "bootstrap",
) -> dict[str, Any]:
    """Idempotently seed a project skill without duplicating restart events."""

    normalized_skill_id = _validate_skill_id(skill_id)
    normalized_source = _validate_source(source)
    existing = connection.execute(
        text(
            """
            SELECT id
            FROM project_skills
            WHERE project_id = :project_id AND skill_id = :skill_id
            """
        ),
        {"project_id": project_id, "skill_id": normalized_skill_id},
    ).mappings().one_or_none()
    if existing is None:
        return register_skill(
            connection,
            project_id,
            normalized_skill_id,
            source=normalized_source,
            enabled=enabled,
            on_demand=on_demand,
            config=config,
            publisher=publisher,
            actor=actor,
        )

    row = connection.execute(
        text(
            """
            UPDATE project_skills
            SET source = :source,
                enabled = CASE WHEN :source = 'system' THEN true ELSE enabled END,
                on_demand = :on_demand,
                config = CAST(:config AS JSONB),
                publisher = CAST(:publisher AS JSONB),
                updated_at = now()
            WHERE id = :id
            RETURNING id, project_id, skill_id, source, enabled, on_demand,
                      config, publisher, created_at, updated_at
            """
        ),
        {
            "id": existing["id"],
            "source": normalized_source,
            "on_demand": bool(on_demand),
            "config": json.dumps(config or {}),
            "publisher": json.dumps(publisher or {}),
        },
    ).mappings().one()
    return _row(row)


def seed_system_skills(
    connection: Connection,
    project_id: str,
    skills: list[dict[str, Any]],
    actor: str = "bootstrap",
) -> list[dict[str, Any]]:
    seeded: list[dict[str, Any]] = []
    for skill in skills:
        if skill.get("system") is not True:
            continue
        seeded.append(
            ensure_skill(
                connection,
                project_id,
                str(skill["id"]),
                source="system",
                enabled=True,
                on_demand=skill.get("load") == "on_demand",
                publisher={"id": "boss", "kind": "system"},
                actor=actor,
            )
        )
    return seeded
