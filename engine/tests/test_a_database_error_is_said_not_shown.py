"""A database error is said as a sentence; its SQL never reaches the operator.

The shared server's dock: "BOSS could not complete that turn: IntegrityError:
(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint
"idx_agent_threads_surface_external" DETAIL: Key (…) already exists. [SQL: INSERT INTO
agent_threads (…". And model_failures.explain, the one explainer every model surface uses, read
the same error as "The model call failed." with the SQL as its detail.
"""
from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy.exc import IntegrityError

from boss_engine.services.model_failures import (
    explain,
    explain_turn_failure,
    is_storage_failure,
    technical_summary,
)

RAW = ("[SQL", "INSERT INTO", "psycopg", "IntegrityError", "UniqueViolation", "parameters")


class UniqueViolation(Exception):
    """Stands in for psycopg.errors.UniqueViolation (same module prefix, same text)."""


UniqueViolation.__module__ = "psycopg.errors"


def _shared_server_error() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO agent_threads (id, project_id, actor, surface, external_ref, agent_role) "
        "VALUES (%(id)s, %(project_id)s, %(actor)s, %(surface)s, %(external_ref)s, "
        "%(agent_role)s) RETURNING id",
        {"id": "athr_x", "project_id": "boss-dogfood"},
        UniqueViolation('duplicate key value violates unique constraint '
                        '"idx_agent_threads_surface_external"\nDETAIL:  Key (project_id, surface, '
                        'external_ref)=(boss-dogfood, chat, role:Program Manager) already exists.'),
    )


def test_the_shared_server_error_is_a_storage_failure_not_a_model_failure():
    error = _shared_server_error()
    assert "[SQL: INSERT INTO agent_threads" in str(error)   # what used to be shown
    said = explain(error)
    assert said["kind"] == "storage" and said["retry"] is True
    assert not [raw for raw in RAW if raw in said["headline"] + said["action"]]


def test_a_turn_says_one_short_sentence():
    said = explain_turn_failure(_shared_server_error())
    assert said["sentence"].startswith("BOSS couldn't save this conversation. Try again")
    assert not [raw for raw in RAW if raw in said["sentence"]], said["sentence"]


def test_the_technical_line_keeps_the_database_message_and_drops_the_statement():
    line = technical_summary(_shared_server_error())
    assert line.startswith("IntegrityError: duplicate key value violates unique constraint")
    assert "already exists" in line
    assert "[SQL" not in line and "INSERT INTO" not in line and "athr_x" not in line
    assert "sqlalche.me" not in line and "(psycopg" not in line


def test_a_recorded_last_error_is_recognised_by_its_text():
    """A step's error arrives as a string — classified the same way."""
    assert is_storage_failure(str(_shared_server_error()))
    assert is_storage_failure("current transaction is aborted, commands ignored until end")
    assert not is_storage_failure("Not logged in · Please run /login")


def test_a_failure_with_no_better_name_is_still_a_sentence():
    said = explain_turn_failure(RuntimeError("boom at step 3"))
    assert said["kind"] == "turn_failed"
    assert said["sentence"].startswith("BOSS couldn't finish this turn.")
    assert "RuntimeError" not in said["sentence"] and "boom" not in said["sentence"]
    assert said["detail"] == "RuntimeError: boom at step 3"


def test_a_model_failure_keeps_its_own_sentence():
    said = explain_turn_failure(RuntimeError("Invalid bearer token"))
    assert said["kind"] == "login_rejected"
    assert "login was rejected" in said["sentence"]
