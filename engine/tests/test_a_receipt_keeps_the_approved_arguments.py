"""A tool receipt may run only the arguments the approval digested.

An approved decision that stores args_sha256 releases that call and no other.
A decision with no digest still releases the call. The check happens before a
receipt is created and before the tool is called.
"""
from __future__ import annotations

import inspect
import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.argument_binding import args_sha256
from boss_engine.services.mcp_registry import (
    approved_arguments_match,
    call_tool_with_receipt,
)

URL = os.environ.get("BOSS_RECEIPT_TEST_DATABASE_URL", "")
_needs_db = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="requires BOSS_RECEIPT_TEST_DATABASE_URL naming a disposable database",
)

# sha256 of {"arguments":{"password":"hunter2","path":"a.py"},"tool":"write_file"}
_DIGEST = "520fdccd4b173d629f76cc8d2c29a597b897e4edfb2fdf09f3e7b4151094a589"
_ARGS = {"path": "a.py", "password": "hunter2"}


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    with eng.begin() as connection:
        connection.execute(text(
            """
            CREATE TABLE IF NOT EXISTS decisions (
              id TEXT PRIMARY KEY,
              project_id TEXT,
              recommendation JSONB
            )
            """
        ))
    yield eng
    eng.dispose()


@pytest.fixture()
def connection(engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM decisions"))
        yield connection


def _decision(connection, project_id: str, decision_id: str, recommendation) -> None:
    connection.execute(
        text(
            """
            INSERT INTO decisions (id, project_id, recommendation)
            VALUES (:id, :project_id, CAST(:recommendation AS jsonb))
            """
        ),
        {
            "id": decision_id,
            "project_id": project_id,
            "recommendation": json.dumps(recommendation),
        },
    )


@_needs_db
def test_a_matching_digest_is_the_approved_call(connection):
    assert args_sha256("write_file", _ARGS) == _DIGEST
    _decision(connection, "p-receipt", "dec-match", {
        "tool": "write_file",
        "args_sha256": _DIGEST,
    })
    assert approved_arguments_match(
        connection, "p-receipt", "dec-match", "write_file", dict(_ARGS),
    ) is True


@_needs_db
def test_different_arguments_are_not_the_approved_call(connection):
    _decision(connection, "p-receipt", "dec-other", {
        "tool": "write_file",
        "args_sha256": _DIGEST,
    })
    assert approved_arguments_match(
        connection, "p-receipt", "dec-other", "write_file",
        {"path": "b.py", "password": "hunter2"},
    ) is False


@_needs_db
def test_a_decision_without_a_digest_still_matches(connection):
    _decision(connection, "p-receipt", "dec-old", {"run_id": "run-1"})
    assert approved_arguments_match(
        connection, "p-receipt", "dec-old", "write_file",
        {"path": "b.py"},
    ) is True
    assert approved_arguments_match(
        connection, "p-receipt", "", "write_file", {"path": "b.py"},
    ) is True
    assert approved_arguments_match(
        connection, "p-receipt", "missing", "write_file", {"path": "b.py"},
    ) is True


@_needs_db
def test_a_non_dict_does_not_match_a_stored_digest(connection):
    _decision(connection, "p-receipt", "dec-shape", {"args_sha256": _DIGEST})
    assert approved_arguments_match(
        connection, "p-receipt", "dec-shape", "write_file", "not-a-dict",
    ) is False


def test_the_receipt_checks_the_digest_before_it_creates_one_or_calls_the_tool():
    source = inspect.getsource(call_tool_with_receipt)
    check = source.index("approved_arguments_match(")
    assert check < source.index("create_or_reuse(")
    assert check < source.index("MCPClient(settings).call_tool")
    assert "McpArgumentsChanged" in source
    assert source.count("approved_arguments_match(") == 1
