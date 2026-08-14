"""Fail-closed behavior when schema.sql has drifted ahead of a live database.

Live dogfood 500'd GET /v1/token-efficiency/policies (and elastic /
worker-autoscaling) because ensure_schema aborted on
``CREATE INDEX … kg_nodes(valid_from)`` before later tables were created.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from apps.api.app.main import programming_error_fail_closed, undefined_table_response
from boss_engine.db import ensure_schema


class _UndefinedTable(Exception):
    """Stand-in for psycopg.errors.UndefinedTable (name is what we match)."""


_UndefinedTable.__name__ = "UndefinedTable"


def test_undefined_table_response_is_503():
    orig = _UndefinedTable('relation "token_budget_policies" does not exist')
    exc = ProgrammingError("SELECT 1", {}, orig)
    resp = undefined_table_response(exc)
    assert resp is not None
    assert resp.status_code == 503
    assert b"not applied" in resp.body


def test_undefined_table_response_ignores_other_sql_errors():
    exc = ProgrammingError("SELECT 1", {}, Exception("syntax error at or near X"))
    assert undefined_table_response(exc) is None


def test_programming_error_handler_returns_503():
    app = FastAPI()
    app.add_exception_handler(ProgrammingError, programming_error_fail_closed)

    @app.get("/boom")
    def boom():
        raise ProgrammingError(
            "SELECT 1",
            {},
            _UndefinedTable('relation "worker_instances" does not exist'),
        )

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 503, r.text
    assert "not applied" in r.json()["detail"]


def test_ensure_schema_reconciles_columns_before_full_sql(monkeypatch, tmp_path):
    order: list[str] = []

    def fake_reconcile(cursor, sql):
        order.append("reconcile")
        assert "CREATE TABLE" in sql
        return 2, []

    class FakeCursor:
        def execute(self, sql):
            order.append("execute")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeRaw:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            order.append("commit")

        def close(self):
            order.append("close")

    class FakeEngine:
        def raw_connection(self):
            return FakeRaw()

    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE IF NOT EXISTS kg_nodes (id TEXT);\n")

    class Settings:
        schema_path = schema

    monkeypatch.setattr("boss_engine.db._reconcile_columns", fake_reconcile)
    ensure_schema(FakeEngine(), settings=Settings())
    assert order[:2] == ["reconcile", "commit"]
    assert "execute" in order
    assert order.index("reconcile") < order.index("execute")
