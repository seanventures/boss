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

-- The run ledger's columns (db/schema.sql, RUN LEDGER).
ALTER TABLE tool_call_audit ADD COLUMN IF NOT EXISTS act_id TEXT;
ALTER TABLE tool_call_audit ADD COLUMN IF NOT EXISTS seq BIGINT;
ALTER TABLE tool_call_audit ADD COLUMN IF NOT EXISTS lineage_id TEXT;
ALTER TABLE tool_call_audit ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_call_idem ON tool_call_audit (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- BOSSAPP-822: the prompt-injection screen's verdict on the result (db/schema.sql, at the end).
ALTER TABLE tool_call_audit ADD COLUMN IF NOT EXISTS screen JSONB;
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
    act_id: Optional[str] = None,
    seq: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    screen: Optional[dict] = None,
) -> str:
    """Insert one audit row and return its id. The table rejects update and delete.

    THE RUN LEDGER (BOSS_LEDGER_EVENTS, kernel/ledger.py): while it is on, or when a kernel run
    hands in its `seq`, the row also carries the run, its sequence, the act and the retry
    lineage, so it reads back in `run_ledger` beside the run's model calls. A write call's
    `idempotency_key` (run:step:call) is unique: the same held call never runs twice.

    `screen` is the injection screen's verdict on a delivered result (BOSSAPP-822:
    {"mode", "verdict": clean|suspicious|unscreened, "rules"}); a refused call has none."""
    if isinstance(arguments, dict):
        digest = args_sha256(tool, arguments)
        preview = redacted_preview(arguments)
    else:
        digest = None
        preview = {}
    row_id = make_id("tca")
    ledger = _ledger_columns(connection, run_id=run_id, act_id=act_id, row_id=row_id, seq=seq)
    if ledger:
        run_id = ledger["ledger_run_id"]
    extra_cols = "".join(f", {c}" for c in ("act_id", "seq", "lineage_id") if c in ledger)
    extra_vals = "".join(f", :{c}" for c in ("act_id", "seq", "lineage_id") if c in ledger)
    if idempotency_key:
        extra_cols, extra_vals = extra_cols + ", idempotency_key", extra_vals + ", :idem"
    if screen is not None:
        extra_cols, extra_vals = extra_cols + ", screen", extra_vals + ", CAST(:screen AS jsonb)"
    connection.execute(
        text(
            f"""
            INSERT INTO tool_call_audit (
              id, project_id, run_id, agent, principal, server_id, tool,
              args_sha256, args_redacted, decision, matched_policy_ids,
              decision_id, outcome, latency_ms{extra_cols}
            ) VALUES (
              :id, :project_id, :run_id, :agent, :principal, :server_id, :tool,
              :args_sha256, CAST(:args_redacted AS jsonb), :decision,
              CAST(:matched_policy_ids AS jsonb), :decision_id, :outcome, :latency_ms{extra_vals}
            )
            """
        ),
        {
            **{k: v for k, v in ledger.items() if k != "ledger_run_id"},
            "idem": idempotency_key,
            "screen": json.dumps(screen) if screen is not None else None,
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


def _ledger_columns(connection: Connection, *, run_id: Optional[str], act_id: Optional[str],
                    row_id: str, seq: Optional[int]) -> dict:
    """{} unless the ledger is on (or a kernel run handed in its seq); never raises. The act is
    the caller's, else the one the ambient model-call context is signed under."""
    from ..kernel import ledger

    if not (ledger.enabled() or seq is not None):
        return {}
    if act_id is None:
        from .llm_telemetry import get_llm_context

        act_id = get_llm_context().get("act_id")
    try:
        with connection.begin_nested():
            return ledger.tool_call_fields(connection, run_id=run_id, act_id=act_id,
                                           row_id=row_id, seq=seq)
    except Exception:  # noqa: BLE001 — the audit row is written whatever the ledger can add
        return {}


def list_tool_calls(
    connection: Connection,
    *,
    project_id: str,
    limit: int = 50,
) -> list[dict]:
    """Return one project's rows, newest recorded_at first, at most limit.

    When recorded_at ties, id descending breaks the tie so the order is stable.
    """
    rows = connection.execute(
        text(
            """
            SELECT tool, args_sha256, args_redacted, decision, outcome, latency_ms
            FROM tool_call_audit
            WHERE project_id = :project_id
            ORDER BY recorded_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "limit": int(limit)},
    ).mappings().all()
    listed = []
    for row in rows:
        redacted = row["args_redacted"]
        if isinstance(redacted, str):
            redacted = json.loads(redacted)
        listed.append(
            {
                "tool": row["tool"],
                "args_sha256": row["args_sha256"],
                "args_redacted": dict(redacted or {}),
                "decision": row["decision"],
                "outcome": row["outcome"],
                "latency_ms": row["latency_ms"],
            }
        )
    return listed
