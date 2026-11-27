"""A failure turned into a sentence for a person keeps its cause, and the log really has it.

northgate-gateway, 2026-11-16: a step said "BOSS couldn't save this to its database. Try again.
If it keeps happening, the details are in the server log." The exception was in no container
log (the API's only root handler wrote to app_logs) and not on the step or its dead letter —
and it was not a database failure at all: a circuit-breaker refusal that QUOTED an old build's
psycopg error was classified by its text.
"""
from __future__ import annotations

import logging

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy.exc import OperationalError

from boss_engine.services import log_capture
from boss_engine.services.model_failures import (
    explain,
    failure_record,
    is_storage_failure,
    log_failure,
)

#: The exact sentence the northgate step's retry raised (workflow_capability_runner).
BREAKER = (
    "could not resume the previous build crun_5f8a0293c23847beb7b9: circuit breaker: 2 attempts "
    "in this lineage have failed with infrastructure errors (last: '(psycopg.errors."
    "IdleInTransactionSessionTimeout) terminating connection due to idle-in-transaction timeout"
    "\\n[SQL: \\n            INSERT INTO events (\\n                id, project_id, event_type, "
    "actor, en') and the run is already on the local substrate. A blind retry will fail the same "
    "way — fix the platform issue (see the error above), then press Retry or retry with a note.")

FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _raised(error: BaseException) -> BaseException:
    try:
        raise error
    except BaseException as caught:  # noqa: BLE001 — a real traceback for the record
        return caught


def _db_error() -> OperationalError:
    return OperationalError("INSERT INTO events (id) VALUES (%(id)s)", {"id": "evt_1"},
                            Exception("terminating connection due to idle-in-transaction timeout"))


def test_a_refusal_that_quotes_a_database_error_is_not_a_database_failure():
    assert not is_storage_failure(BREAKER)
    wrapped = RuntimeError(BREAKER)
    wrapped.__cause__ = ValueError(BREAKER)
    assert not is_storage_failure(wrapped)
    assert explain(BREAKER)["kind"] != "storage"


def test_a_real_database_failure_is_still_one_by_type_and_by_text():
    error = _db_error()
    assert is_storage_failure(error)
    assert is_storage_failure(str(error))                         # a recorded last_error
    assert is_storage_failure(f"OperationalError: {error}")       # the runner's generic shape
    wrapped = RuntimeError("could not save the step")
    wrapped.__cause__ = error
    assert is_storage_failure(wrapped)
    assert is_storage_failure("current transaction is aborted, commands ignored until end")
    assert is_storage_failure('insert or update on table "x" violates foreign key constraint "y"')


def test_the_person_facing_sentence_never_points_at_a_log_that_was_not_written(caplog):
    caplog.set_level(logging.WARNING, logger="boss_engine.services.model_failures")
    error = _raised(_db_error())
    said = explain(error)
    assert said["kind"] == "storage"
    assert "server log" not in said["action"]
    assert "Activity Log" in said["action"]
    logged = [r for r in caplog.records if r.name == "boss_engine.services.model_failures"]
    assert logged, "the sentence names a log the failure was never written to"
    line = logged[-1].getMessage()
    assert "sqlalchemy.exc.OperationalError" in line and "Traceback" in line


def test_the_record_has_type_message_and_stack_with_secrets_redacted():
    error = _raised(RuntimeError(f"push refused for https://x:{FAKE_TOKEN}@github.com/a/b"))
    record = failure_record(error)
    assert record["type"] == "builtins.RuntimeError"
    assert "push refused" in record["message"]
    assert "Traceback" in record["stack"] and "_raised" in record["stack"]
    assert FAKE_TOKEN not in record["message"] and FAKE_TOKEN not in record["stack"]
    assert failure_record("plain text")["type"] == "text"


def test_a_live_failure_is_logged_once_with_its_stack(caplog):
    caplog.set_level(logging.WARNING, logger="boss_engine.services.model_failures")
    error = _raised(_db_error())
    log_failure(error, where="workflow step wfs_x failed on the database")
    explain(error)                      # the same exception, explained after: not logged twice
    lines = [r.getMessage() for r in caplog.records
             if r.name == "boss_engine.services.model_failures"]
    assert len(lines) == 1 and lines[0].startswith("workflow step wfs_x failed on the database")
    assert "Traceback" in lines[0]


def test_the_activity_log_capture_keeps_warnings_in_the_container_log():
    root = logging.Logger("isolated-root")
    log_capture._keep_the_container_log(root)
    streams = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert len(streams) == 1 and streams[0].level == logging.WARNING
    log_capture._keep_the_container_log(root)          # idempotent
    assert len([h for h in root.handlers if isinstance(h, logging.StreamHandler)]) == 1


def test_a_statements_bound_values_are_not_recorded():
    record = failure_record(_raised(OperationalError(
        "INSERT INTO users (email) VALUES (%(e)s)", {"e": "someone@example.com"},
        Exception("server closed the connection"))))
    assert "someone@example.com" not in record["message"] + record.get("stack", "")
    assert "server closed the connection" in record["message"]


# ── BOSSAPP-1300(c): dotted prefixes, prefixed messages, connection errors, __context__ ──────

def test_storage_failure_detects_dotted_prefixes_prefixed_messages_and_connection_errors():
    for msg in (
        "sqlalchemy.exc.IntegrityError: duplicate key value violates unique constraint x",
        "psycopg.OperationalError: connection refused",
        "RuntimeError: sqlalchemy.exc.OperationalError: (psycopg.OperationalError) connection failed",
        "database error: server closed the connection unexpectedly",
        "BOSS could not complete that turn: IntegrityError: (psycopg.errors.UniqueViolation) dup",
        '(psycopg.OperationalError) connection to server at "127.0.0.1", port 5432 failed: Connection refused',
        "server closed the connection unexpectedly",
        "SSL connection has been closed unexpectedly",
        "remaining connection slots are reserved for non-replication superuser connections",
        "terminating connection due to idle-in-transaction timeout",
    ):
        assert is_storage_failure(msg), msg


def test_storage_failure_and_explain_walk_context_chain_without_matching_quoted_breaker():
    try:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
    except Exception:
        try:
            raise RuntimeError("step failed")  # sets __context__ with __cause__=None
        except RuntimeError as outer:
            assert outer.__cause__ is None
            assert is_storage_failure(outer) is True
            assert explain(outer)["kind"] == "storage"

    # Quoted breaker in __context__ must still return False.
    try:
        raise ValueError(BREAKER)
    except ValueError:
        try:
            raise RuntimeError(BREAKER)
        except RuntimeError as outer_breaker:
            assert outer_breaker.__cause__ is None
            assert is_storage_failure(outer_breaker) is False
