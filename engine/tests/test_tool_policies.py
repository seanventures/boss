"""A revocable always-allow row for one tool.

Grant inserts an active row. A second grant inserts another row. Revoke
hides it from later active lists. Another project's row stays out of the list.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from boss_engine.services.tool_policies import (
    SCHEMA_SQL,
    grant_always_allow,
    list_always_allow,
    revoke_always_allow,
)

URL = os.environ.get("BOSS_POLICY_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="requires BOSS_POLICY_TEST_DATABASE_URL naming a disposable database",
)

RUN = uuid4().hex[:8]


def _project(name: str) -> str:
    return f"{name}-{RUN}"


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


def test_grant_then_list_shows_tool_hash_and_decision_id(engine):
    project_id = _project("p-grant")
    with engine.begin() as connection:
        row_id = grant_always_allow(
            connection,
            project_id=project_id,
            server_id="srv-1",
            tool="write_file",
            schema_sha256="hash-write-file",
            source_decision_id="dec-1",
        )
        rows = list_always_allow(connection, project_id=project_id)
    assert rows == [
        {
            "id": row_id,
            "tool": "write_file",
            "schema_sha256": "hash-write-file",
            "source_decision_id": "dec-1",
            "server_id": "srv-1",
        }
    ]


def test_a_second_grant_is_a_second_active_row(engine):
    project_id = _project("p-second")
    with engine.begin() as connection:
        first = grant_always_allow(
            connection,
            project_id=project_id,
            server_id="srv-1",
            tool="write_file",
            schema_sha256="hash-1",
            source_decision_id="dec-1",
        )
    with engine.begin() as connection:
        second = grant_always_allow(
            connection,
            project_id=project_id,
            server_id="srv-1",
            tool="write_file",
            schema_sha256="hash-2",
            source_decision_id="dec-2",
        )
    with engine.begin() as connection:
        rows = list_always_allow(connection, project_id=project_id)
    assert [row["id"] for row in rows] == [second, first]
    assert rows[0]["schema_sha256"] == "hash-2"
    assert rows[1]["schema_sha256"] == "hash-1"
    assert first != second


def test_revoke_removes_it_from_the_active_list_and_a_later_list(engine):
    project_id = _project("p-revoke")
    with engine.begin() as connection:
        kept = grant_always_allow(
            connection,
            project_id=project_id,
            server_id="srv-1",
            tool="read_file",
            schema_sha256="hash-read",
            source_decision_id="dec-keep",
        )
        gone = grant_always_allow(
            connection,
            project_id=project_id,
            server_id="srv-1",
            tool="write_file",
            schema_sha256="hash-write",
            source_decision_id="dec-gone",
        )
    with engine.begin() as connection:
        revoke_always_allow(connection, project_id=project_id, policy_id=gone)
        active = list_always_allow(connection, project_id=project_id)
    assert [row["id"] for row in active] == [kept]
    with engine.begin() as connection:
        later = list_always_allow(connection, project_id=project_id)
    assert [row["id"] for row in later] == [kept]


def test_revoke_of_an_unknown_id_raises_lookup_error(engine):
    with engine.begin() as connection:
        with pytest.raises(LookupError):
            revoke_always_allow(
                connection,
                project_id=_project("p-missing"),
                policy_id="tpl_does_not_exist",
            )


def test_another_projects_row_is_not_listed(engine):
    mine = _project("p-mine")
    other = _project("p-other")
    with engine.begin() as connection:
        own_id = grant_always_allow(
            connection,
            project_id=mine,
            server_id="srv-mine",
            tool="own_tool",
            schema_sha256="hash-own",
            source_decision_id="dec-own",
        )
        grant_always_allow(
            connection,
            project_id=other,
            server_id="srv-other",
            tool="other_tool",
            schema_sha256="hash-other",
            source_decision_id="dec-other",
        )
        rows = list_always_allow(connection, project_id=mine)
    assert rows == [
        {
            "id": own_id,
            "tool": "own_tool",
            "schema_sha256": "hash-own",
            "source_decision_id": "dec-own",
            "server_id": "srv-mine",
        }
    ]
