from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_runtime import AgentRuntimeService
from .core import emit_event, make_id
from .entitlements import EntitlementBrokerService


SKILL_SOURCES = {"system", "tenant", "module"}


def _skill_reference_matches(reference: str, skill_id: str) -> bool:
    """Match exact/wildcard RoleSpec ids and legacy dotted aliases.

    Existing modules use compact references such as ``health.check`` and
    ``deployment.plan`` for canonical SkillSpecs named
    ``deployment.health.check`` and ``deployment.run.plan``. Treat the
    reference segments as an ordered subsequence until B6 can reject stale
    aliases at pack-load time.
    """

    if reference == skill_id:
        return True
    if reference.endswith(".*") and skill_id.startswith(reference[:-1]):
        return True
    reference_parts = [part for part in reference.split(".") if part]
    skill_parts = [part for part in skill_id.split(".") if part]
    position = 0
    for part in skill_parts:
        if position < len(reference_parts) and part == reference_parts[position]:
            position += 1
    return bool(reference_parts) and position == len(reference_parts)


def _matching_skill_ids(reference: str, catalog_ids: set[str]) -> set[str]:
    if reference in catalog_ids:
        return {reference}
    return {
        skill_id
        for skill_id in catalog_ids
        if _skill_reference_matches(reference, skill_id)
    }


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


def resolve_for_task(
    connection: Connection,
    settings: Settings,
    project_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the eager and discoverable SkillSpecs for one project task.

    The eager set is the governed union of system skills, matched module
    defaults, and enabled tenant skills. Pack membership is applied by the
    entitlement catalog before RoleSpec allow/forbid filters. On-demand
    members of the same union remain discoverable but are not injected into
    the agent's eager schema list.
    """

    if not isinstance(task, dict):
        raise ValueError("task must be an object")
    job_kind = str(task.get("job_kind") or task.get("kind") or "").strip()
    agent_module_id = str(task.get("agent_module_id") or "").strip() or None
    runtime_context = task.get("agent_runtime")
    if not isinstance(runtime_context, dict):
        runtime_context = None
    if runtime_context is None and job_kind:
        runtime_context = AgentRuntimeService(settings).resolve(
            connection,
            project_id,
            job_kind=job_kind,
            agent_module_id=agent_module_id,
            task_payload=task,
            required=False,
        )
    runtime_context = runtime_context or {
        "matched": False,
        "agent_module": None,
        "runtime": {},
        "role": {},
    }

    catalog = EntitlementBrokerService(settings).list_skills(
        project_id,
        connection=connection,
    )
    by_id = {str(skill["id"]): skill for skill in catalog}
    catalog_ids = set(by_id)
    rows = list_project_skills(connection, project_id)
    rows_by_id = {str(row["skill_id"]): row for row in rows}

    resolution_sources: dict[str, set[str]] = {}

    def include(skill_id: str, source: str) -> None:
        if skill_id in catalog_ids:
            resolution_sources.setdefault(skill_id, set()).add(source)

    for skill in catalog:
        if skill.get("system") is True:
            include(str(skill["id"]), "system")

    runtime = runtime_context.get("runtime") or {}
    for reference in runtime.get("default_skill_ids") or []:
        if not isinstance(reference, str) or not reference.strip():
            continue
        for skill_id in _matching_skill_ids(reference.strip(), catalog_ids):
            include(skill_id, "module")

    for row in rows:
        if row["source"] == "tenant" and row["enabled"]:
            include(str(row["skill_id"]), "tenant")

    role = runtime_context.get("role") or {}
    role_key = role.get("role_key")
    permissions = role.get("permissions") or {}
    allowed = [
        item.strip()
        for item in permissions.get("allowed_skills") or []
        if isinstance(item, str) and item.strip()
    ]
    forbidden = [
        item.strip()
        for item in permissions.get("forbidden_skills") or []
        if isinstance(item, str) and item.strip()
    ]

    eager: list[dict[str, Any]] = []
    on_demand: list[dict[str, Any]] = []
    for skill_id in sorted(resolution_sources):
        row = rows_by_id.get(skill_id)
        if (
            row is not None
            and not row["enabled"]
            and by_id[skill_id].get("system") is not True
        ):
            continue
        if role_key:
            if any(
                _skill_reference_matches(pattern, skill_id)
                for pattern in forbidden
            ):
                continue
            if not any(
                _skill_reference_matches(pattern, skill_id)
                for pattern in allowed
            ):
                continue
        skill = dict(by_id[skill_id])
        skill["source"] = (
            row["source"]
            if row is not None
            else "system"
            if skill.get("system") is True
            else "module"
        )
        skill["enabled"] = (
            True
            if skill.get("system") is True
            else bool(row["enabled"])
            if row is not None
            else True
        )
        skill["on_demand"] = (
            bool(row["on_demand"])
            if row is not None
            else skill.get("load") == "on_demand"
        )
        skill["resolved_from"] = sorted(resolution_sources[skill_id])
        skill["role_key"] = role_key
        allowed_callers = skill.get("permissions", {}).get("allowed_callers") or []
        preferred_caller = str(task.get("skill_caller") or "").strip()
        skill["caller"] = (
            role_key
            if role_key in allowed_callers
            else preferred_caller
            if preferred_caller in allowed_callers
            else "software_factory"
            if job_kind == "openai.plan" and "software_factory" in allowed_callers
            else role_key or preferred_caller or "agent_loop"
        )
        if skill["on_demand"]:
            on_demand.append(skill)
        else:
            eager.append(skill)

    return {
        "project_id": project_id,
        "job_kind": job_kind or None,
        "agent_module": runtime_context.get("agent_module"),
        "role_key": role_key,
        "eager": eager,
        "on_demand": on_demand,
    }
