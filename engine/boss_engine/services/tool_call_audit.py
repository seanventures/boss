"""Append-only record of one tool call.

A row names the tool, the argument digest, the redacted arguments, the
decision, and the outcome. Update and delete are rejected by the database.
The export route and the Activity tab are a later slice.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .argument_binding import args_sha256, redacted_preview
from .core import make_id

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_call_audit (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  run_id TEXT,
  agent TEXT,
  principal TEXT,
  server_id TEXT,
  tool TEXT NOT NULL,
  args_sha256 TEXT,
  args_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
  decision TEXT NOT NULL,
  matched_policy_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  decision_id TEXT,
  outcome TEXT NOT NULL,
  latency_ms INT NOT NULL DEFAULT 0,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION tool_call_audit_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'tool_call_audit is append-only';
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'tool_call_audit_no_update'
  ) THEN
    CREATE TRIGGER tool_call_audit_no_update
      BEFORE UPDATE OR DELETE ON tool_call_audit
      FOR EACH ROW
      EXECUTE FUNCTION tool_call_audit_append_only();
  END IF;
END;
$$;
"""


def record_tool_call(
    connection: Connection,
    *,
    project_id: str,
    run_id: Optional[str],
    agent: Optional[str],
    principal: Optional[str],
    server_id: Optional[str],
    tool: str,
    arguments: Any,
    decision: str,
    matched_policy_ids: list,
    decision_id: Optional[str],
    outcome: str,
    latency_ms: int,
) -> str:
    """Insert one audit row and return its id. The table rejects update and delete."""
    if isinstance(arguments, dict):
        digest = args_sha256(tool, arguments)
        preview = redacted_preview(arguments)
    else:
        digest = None
        preview = {}
    row_id = make_id("tca")
    connection.execute(
        text(
            """
            INSERT INTO tool_call_audit (
              id, project_id, run_id, agent, principal, server_id, tool,
              args_sha256, args_redacted, decision, matched_policy_ids,
              decision_id, outcome, latency_ms
            ) VALUES (
              :id, :project_id, :run_id, :agent, :principal, :server_id, :tool,
              :args_sha256, CAST(:args_redacted AS jsonb), :decision,
              CAST(:matched_policy_ids AS jsonb), :decision_id, :outcome, :latency_ms
            )
            """
        ),
        {
            "id": row_id,
            "project_id": project_id,
            "run_id": run_id,
            "agent": agent,
            "principal": principal,
            "server_id": server_id,
            "tool": tool,
            "args_sha256": digest,
            "args_redacted": json.dumps(preview),
            "decision": decision,
            "matched_policy_ids": json.dumps(list(matched_policy_ids or [])),
            "decision_id": decision_id,
            "outcome": outcome,
            "latency_ms": int(latency_ms),
        },
    )
    return row_id
