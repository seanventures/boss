"""Fundamental knowledge grants.

knowledge.read is held by every role. knowledge.propose is held by builder and
reviewer roles, and by any role whose spec already allows code edit or review.
The grant is a role_skills row. A wildcard in a role spec is not expanded.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

READ = "knowledge.read"
PROPOSE = "knowledge.propose"

_PROPOSE_ROLES = frozenset({
    "software_engineer",
    "engineer",
    "qa",
    "cto",
    "cao",
    "design_critic",
    "security",
    "reviewer",
    "archivist",
})
_PROPOSE_SKILLS = frozenset({"code.edit", "code.review"})


def skills_for_role(role_key: str, allowed_skills: list[str] | None = None) -> list[str]:
    allowed = {str(item).strip() for item in (allowed_skills or []) if str(item).strip()}
    granted = [READ]
    if role_key in _PROPOSE_ROLES or bool(allowed & _PROPOSE_SKILLS):
        granted.append(PROPOSE)
    return granted


def grant_knowledge_roles(
    connection: Connection, *, project_id: str, actor: str = "system"
) -> dict[str, Any]:
    from ..skill_definitions import SkillDefinitionError, assign_skill_to_role

    rows = connection.execute(
        text("SELECT role_key, spec FROM roles WHERE project_id = :project_id ORDER BY role_key"),
        {"project_id": project_id},
    ).mappings().all()
    assigned: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for row in rows:
        spec = row["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        if not isinstance(spec, dict):
            spec = {}
        allowed = ((spec.get("permissions") or {}).get("allowed_skills")) or []
        for skill_id in skills_for_role(str(row["role_key"]), [str(item) for item in allowed]):
            try:
                assign_skill_to_role(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    role_key=str(row["role_key"]),
                    skill_id=skill_id,
                )
                assigned.append({"role_key": str(row["role_key"]), "skill_id": skill_id})
            except SkillDefinitionError as error:
                missing.append({
                    "role_key": str(row["role_key"]),
                    "skill_id": skill_id,
                    "reason": str(error),
                })
    return {
        "assigned": assigned,
        "assigned_count": len(assigned),
        "unavailable": missing,
        "unavailable_count": len(missing),
    }
