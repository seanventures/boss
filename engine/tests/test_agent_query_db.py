"""The agent can ask the database, and only ask it.

Twelve hand-written tools could not answer "why did the last build fail", because that answer
lives in Postgres and every new question needed a new tool. Twenty would not have fixed it.
So the agent gets read access — and read access is only safe if "read" is actually enforced,
which is what these pin.

The guard is deliberately paranoid about STATEMENT CHAINING: a single SELECT is harmless, and
`SELECT 1; DROP TABLE runs` is how a read tool becomes a write tool.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services.agent_loop import _query_rejection, _tool_query_db
from boss_engine.tools.builtin import _is_read_only


_ENTERPRISE = SimpleNamespace(boss_edition="enterprise_self_hosted")


@pytest.mark.parametrize("sql", [
    "SELECT id, status FROM code_task_runs LIMIT 5",
    "select * from jobs where status='deadlettered'",
    "  WITH recent AS (SELECT * FROM events LIMIT 10) SELECT count(*) FROM recent  ",
    "SELECT id FROM runs; ",                       # a trailing semicolon is not a second statement
    "-- what failed?\nSELECT error FROM jobs LIMIT 1",
])
def test_reads_are_allowed(sql):
    assert _query_rejection(sql) is None


@pytest.mark.parametrize("sql", [
    "DELETE FROM code_task_runs",
    "UPDATE decisions SET status='approved'",
    "INSERT INTO events (id) VALUES ('x')",
    "DROP TABLE jobs",
    "TRUNCATE events",
    "ALTER TABLE jobs ADD COLUMN x int",
    "GRANT ALL ON events TO PUBLIC",
])
def test_writes_are_refused(sql):
    reason = _query_rejection(sql)
    assert reason and "read-only" in reason


def test_a_second_statement_is_refused_even_when_the_first_is_a_select():
    """The whole point of the guard: chaining is how a read tool becomes a write tool."""
    assert _query_rejection("SELECT 1; DROP TABLE code_task_runs") == "one statement at a time"


def test_a_write_hidden_behind_a_comment_is_still_refused():
    assert _query_rejection("/* harmless */ DELETE FROM jobs") is not None


def test_a_column_named_like_a_keyword_does_not_block_a_real_query():
    """`created_at` contains no keyword; `updated_at` contains 'update' only as a substring.
    Word boundaries matter — a guard that rejects valid reads teaches people to avoid the
    tool."""
    assert _query_rejection("SELECT updated_at, created_at FROM jobs LIMIT 5") is None


def test_empty_is_refused():
    assert _query_rejection("   ") == "empty query"


class _Row(dict):
    def keys(self):
        return super().keys()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    def __init__(self, rows=None, boom=None):
        self.rows = rows or []
        self.boom = boom
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append(str(statement))
        if self.boom:
            raise self.boom
        return _Result(self.rows)


def test_it_returns_rows():
    connection = _Connection(rows=[_Row(id="crun_1", status="failed")])
    out = _tool_query_db(connection, "p1", _ENTERPRISE, {"sql": "SELECT id, status FROM runs"})
    assert out["rows"] == [{"id": "crun_1", "status": "failed"}]
    assert out["row_count"] == 1 and out["truncated"] is False


def test_a_refused_query_never_reaches_the_database():
    connection = _Connection()
    out = _tool_query_db(connection, "p1", _ENTERPRISE, {"sql": "DELETE FROM jobs"})
    assert "error" in out
    assert connection.executed == []          # the guard runs BEFORE the connection is touched


def test_truncation_is_declared_rather_than_silent():
    """A silently cut result set is worse than a short one: the model reasons from it as if it
    were complete."""
    connection = _Connection(rows=[_Row(n=i) for i in range(500)])
    out = _tool_query_db(connection, "p1", _ENTERPRISE, {"sql": "SELECT n FROM big"})
    assert out["truncated"] is True and out["row_count"] == 200


def test_a_broken_query_returns_its_error_instead_of_raising():
    """For a bad query the database's message IS the answer — it is how the model corrects
    itself on the next turn."""
    connection = _Connection(boom=RuntimeError('column "nope" does not exist'))
    out = _tool_query_db(connection, "p1", _ENTERPRISE, {"sql": "SELECT nope FROM jobs"})
    assert "nope" in out["error"]


def test_select_into_is_a_write_and_is_refused():
    """SELECT INTO creates a table. The write-keyword list omitted INTO, so a 'read' tool
    could persist another tenant's rows (or dump secret_refs) into a new relation."""
    assert _query_rejection("SELECT * INTO stolen FROM secret_refs") is not None
    connection = _Connection()
    out = _tool_query_db(connection, "p1", _ENTERPRISE, {"sql": "SELECT * INTO stolen FROM jobs"})
    assert "error" in out
    assert connection.executed == []


def test_secret_tables_are_not_queryable():
    """The discuss path is a language model choosing SQL. Catalog tables that store
    credential material must not be readable even as SELECT."""
    for sql in (
        "SELECT * FROM secret_refs",
        "SELECT ciphertext FROM secret_material",
        "WITH s AS (SELECT * FROM secret_refs) SELECT * FROM s",
    ):
        assert _query_rejection(sql) is not None, sql


def test_builtin_query_db_refuses_into_and_secret_catalogs():
    """The MCP/default registry has its own guard. Both must refuse the same writes."""
    ok, why = _is_read_only("SELECT * INTO stolen FROM secret_refs")
    assert ok is False and why
    ok, why = _is_read_only("SELECT * FROM secret_material")
    assert ok is False and why
    ok, _ = _is_read_only("SELECT id, status FROM jobs LIMIT 5")
    assert ok is True
