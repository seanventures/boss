"""A revocable always-allow row for one tool.

Grant inserts an active row. Revoke marks that row and later lists omit it.
This slice does not change the tool-call path.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  server_id TEXT,
  tool TEXT NOT NULL,
  schema_sha256 TEXT,
  source_decision_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  CHECK (status IN ('active', 'revoked'))
);
"""


def grant_always_allow(
    connection: Connection,
    *,
    project_id: str,
    server_id: Optional[str],
    tool: str,
    schema_sha256: Optional[str],
    source_decision_id: Optional[str],
) -> str:
    """Insert one active always-allow row and return its id.

    A second grant inserts another row. It does not update the first.
    """
    row_id = make_id("tpl")
    connection.execute(
        text(
            """
            INSERT INTO tool_policies (
              id, project_id, server_id, tool, schema_sha256, source_decision_id, status
            ) VALUES (
              :id, :project_id, :server_id, :tool, :schema_sha256, :source_decision_id,
              'active'
            )
            """
        ),
        {
            "id": row_id,
            "project_id": project_id,
            "server_id": server_id,
            "tool": tool,
            "schema_sha256": schema_sha256,
            "source_decision_id": source_decision_id,
        },
    )
    return row_id


def revoke_always_allow(connection: Connection, *, project_id: str, policy_id: str) -> None:
    """Mark one project's always-allow row revoked. Missing rows raise LookupError."""
    result = connection.execute(
        text(
            """
            UPDATE tool_policies
            SET status = 'revoked', revoked_at = now()
            WHERE id = :policy_id AND project_id = :project_id
            """
        ),
        {"policy_id": policy_id, "project_id": project_id},
    )
    if result.rowcount != 1:
        raise LookupError(policy_id)


def list_always_allow(connection: Connection, *, project_id: str) -> list[dict]:
    """Active always-allow rows for this project, newest created_at first."""
    rows = connection.execute(
        text(
            """
            SELECT id, tool, schema_sha256, source_decision_id, server_id
            FROM tool_policies
            WHERE project_id = :project_id AND status = 'active'
            ORDER BY created_at DESC
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]
