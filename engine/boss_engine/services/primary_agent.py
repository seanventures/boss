"""One primary agent per person per company.

The row is a preference. Writing it must not change that person's authority
on org_members: role_keys, permissions, status, member_type, or updated_at.
A second company keeps its own row for the same person.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id


class PrimaryAgentError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def authority_of(connection: Connection, *, org_id: str, person_id: str) -> dict[str, Any]:
    """The membership fields that are this person's authority in the company."""
    row = connection.execute(
        text(
            "SELECT role_keys, permissions, status, member_type, updated_at "
            "FROM org_members WHERE org_id = :org AND member_id = :person"
        ),
        {"org": org_id, "person": person_id},
    ).mappings().first()
    if row is None:
        raise PrimaryAgentError("not_a_member")
    return {
        "role_keys": list(row["role_keys"] or []),
        "permissions": list(row["permissions"] or []),
        "status": row["status"],
        "member_type": row["member_type"],
        "updated_at": row["updated_at"],
    }


def resolve_member(connection: Connection, *, org_id: str, candidates: Sequence[str]) -> str:
    """The active org member among the caller's ids. The first hit wins."""
    seen: list[str] = []
    for raw in candidates:
        candidate = (raw or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.append(candidate)
        found = connection.execute(
            text(
                "SELECT 1 FROM org_members "
                "WHERE org_id = :org AND member_id = :person AND status = 'active'"
            ),
            {"org": org_id, "person": candidate},
        ).first()
        if found is not None:
            return candidate
    raise PrimaryAgentError("not_a_member")


def primary_agent(connection: Connection, *, org_id: str, person_id: str) -> Optional[str]:
    """That person's one primary agent in this company, or None."""
    rows = connection.execute(
        text(
            "SELECT agent_id FROM person_primary_agents "
            "WHERE org_id = :org AND person_id = :person"
        ),
        {"org": org_id, "person": person_id},
    ).scalars().all()
    if len(rows) > 1:
        raise PrimaryAgentError("more_than_one")
    return rows[0] if rows else None


def _agent_in_company(connection: Connection, org_id: str, agent_id: str) -> bool:
    found = connection.execute(
        text(
            "SELECT 1 FROM agent_definitions a "
            "JOIN projects p ON p.id = a.project_id "
            "JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE a.id = :agent AND w.org_id = :org"
        ),
        {"agent": agent_id, "org": org_id},
    ).first()
    return found is not None


def set_primary(
    connection: Connection, *, org_id: str, person_id: str, agent_id: str
) -> dict[str, Optional[str]]:
    """Make agent_id this person's only primary in this company.

    Replaces the previous primary for that person in that company. Another
    company's row is left as it is. The membership row is read before and
    after; a change to authority rolls the preference write back with the
    caller's transaction.
    """
    org_id = (org_id or "").strip()
    person_id = (person_id or "").strip()
    agent_id = (agent_id or "").strip()
    if not org_id or not person_id or not agent_id:
        raise PrimaryAgentError("missing")
    before = authority_of(connection, org_id=org_id, person_id=person_id)
    if before["status"] != "active":
        raise PrimaryAgentError("not_a_member")
    if not _agent_in_company(connection, org_id, agent_id):
        raise PrimaryAgentError("agent_not_in_company")
    replaced = primary_agent(connection, org_id=org_id, person_id=person_id)
    connection.execute(
        text(
            "INSERT INTO person_primary_agents (id, org_id, person_id, agent_id) "
            "VALUES (:id, :org, :person, :agent) "
            "ON CONFLICT (org_id, person_id) DO UPDATE SET "
            "agent_id = EXCLUDED.agent_id, updated_at = now()"
        ),
        {"id": make_id("ppa"), "org": org_id, "person": person_id, "agent": agent_id},
    )
    after = authority_of(connection, org_id=org_id, person_id=person_id)
    if after != before:
        raise PrimaryAgentError("authority_changed")
    return {
        "org_id": org_id,
        "person_id": person_id,
        "agent_id": agent_id,
        "replaced_agent_id": None if replaced == agent_id else replaced,
    }
