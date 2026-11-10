"""A tool call is appended once and cannot be rewritten.

The row names the tool, the argument digest, the redacted arguments, the
decision, and the outcome. Update and delete are rejected. The export route
and the Activity tab are a later slice.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from boss_engine.services.tool_call_audit import (
    SCHEMA_SQL,
    list_tool_calls,
    record_tool_call,
)

URL = os.environ.get("BOSS_AUDIT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="requires BOSS_AUDIT_TEST_DATABASE_URL naming a disposable database",
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    raw = eng.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute(SCHEMA_SQL)
        raw.commit()
    finally:
        raw.close()
    yield eng
    eng.dispose()


def test_a_call_is_appended_with_the_hash_and_the_redacted_arguments(engine):
    with engine.begin() as connection:
        row_id = record_tool_call(
            connection,
            project_id="p-audit",
            run_id="run-1",
            agent="agent_loop",
            principal="ceo",
            server_id="srv-1",
            tool="write_file",
            arguments={"path": "a.py", "password": "hunter2"},
            decision="allow",
            matched_policy_ids=["allow-write"],
            decision_id=None,
            outcome="succeeded",
            latency_ms=12,
        )
        row = connection.execute(
            text("SELECT tool, args_sha256, args_redacted, decision, outcome, latency_ms "
                 "FROM tool_call_audit WHERE id = :id"),
            {"id": row_id},
        ).mappings().one()
    assert row["tool"] == "write_file"
    assert row["decision"] == "allow"
    assert row["outcome"] == "succeeded"
    assert row["latency_ms"] == 12
    assert len(row["args_sha256"]) == 64
    redacted = row["args_redacted"]
    assert redacted["path"] == "a.py"
    assert redacted["password"] == "[redacted]"


def test_update_and_delete_are_rejected(engine):
    with engine.begin() as connection:
        row_id = record_tool_call(
            connection,
            project_id="p-audit",
            run_id=None,
            agent=None,
            principal=None,
            server_id=None,
            tool="read_file",
            arguments={"path": "a.py"},
            decision="off",
            matched_policy_ids=[],
            decision_id=None,
            outcome="refused",
            latency_ms=0,
        )
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="append-only"):
            connection.execute(
                text("UPDATE tool_call_audit SET outcome = 'succeeded' WHERE id = :id"),
                {"id": row_id},
            )
        transaction.rollback()
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="append-only"):
            connection.execute(
                text("DELETE FROM tool_call_audit WHERE id = :id"),
                {"id": row_id},
            )
        transaction.rollback()
        still = connection.execute(
            text("SELECT outcome FROM tool_call_audit WHERE id = :id"),
            {"id": row_id},
        ).scalar()
    assert still == "refused"


def test_list_tool_calls_returns_one_project_newest_first(engine):
    # now() is the transaction start, so each call is its own transaction.
    # Append-only: a rerun wipes the table instead of deleting rows.
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE tool_call_audit"))
    with engine.begin() as connection:
        record_tool_call(
            connection,
            project_id="p-list",
            run_id=None,
            agent=None,
            principal=None,
            server_id=None,
            tool="older_tool",
            arguments={"path": "notes.txt", "password": "hunter2"},
            decision="allow",
            matched_policy_ids=[],
            decision_id=None,
            outcome="succeeded",
            latency_ms=4,
        )
    with engine.begin() as connection:
        record_tool_call(
            connection,
            project_id="p-list",
            run_id="run-9",
            agent="agent_loop",
            principal="ceo",
            server_id="srv-1",
            tool="newer_tool",
            arguments={"path": "notes.txt", "password": "hunter2"},
            decision="allow",
            matched_policy_ids=["allow-write"],
            decision_id=None,
            outcome="succeeded",
            latency_ms=9,
        )
    with engine.begin() as connection:
        record_tool_call(
            connection,
            project_id="p-other",
            run_id=None,
            agent=None,
            principal=None,
            server_id=None,
            tool="other_tool",
            arguments={"path": "other.txt"},
            decision="off",
            matched_policy_ids=[],
            decision_id=None,
            outcome="refused",
            latency_ms=1,
        )
    with engine.begin() as connection:
        rows = list_tool_calls(connection, project_id="p-list")
        newest = list_tool_calls(connection, project_id="p-list", limit=1)
    assert [row["tool"] for row in rows] == ["newer_tool", "older_tool"]
    assert [row["tool"] for row in newest] == ["newer_tool"]
    assert rows[0]["latency_ms"] == 9
    assert rows[1]["latency_ms"] == 4
    assert rows[0]["decision"] == "allow"
    assert rows[0]["outcome"] == "succeeded"
    assert len(rows[0]["args_sha256"]) == 64
    assert rows[0]["args_redacted"] == {"path": "notes.txt", "password": "[redacted]"}
    assert set(rows[0]) == {
        "tool",
        "args_sha256",
        "args_redacted",
        "decision",
        "outcome",
        "latency_ms",
    }


def test_call_tool_with_receipt_records_every_outcome():
    source = (
        Path(__file__).resolve().parents[1] / "boss_engine" / "services" / "mcp_registry.py"
    ).read_text()
    start = source.index("def call_tool_with_receipt")
    end = source.index("\ndef _refusal", start)
    body = source[start:end]
    assert "record_tool_call(" in body
    assert body.count("\n        _audit_tool_call(") >= 4
