"""An index declared in schema.sql is the index the database has.

Seen on the shared server (an install whose database predates the Community repo): "Ask Program
Manager" failed on every turn with a unique violation on `idx_agent_threads_surface_external`.
schema.sql had widened that index from (project_id, surface, external_ref) to
(…, agent_role) so each agent keeps its own thread on a shared subject — but
`CREATE UNIQUE INDEX IF NOT EXISTS` never redefines an index that already exists, so the shared server
kept the 3-column one and refused the second agent's thread forever. boss-1, created later,
had the 4-column index and worked.

So ensure_schema converges every DECLARED index: a different definition under the same name is
dropped and recreated, recorded in schema_apply_runs; an index schema.sql does not declare is
never touched.
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.db import _converge_indexes, declared_indexes, ensure_schema

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")

SCHEMA_SQL = pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"

AGENT_THREADS = """
CREATE TABLE IF NOT EXISTS agent_threads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  surface TEXT NOT NULL DEFAULT 'chat',
  external_ref TEXT,
  agent_role TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_threads_project_actor
  ON agent_threads(project_id, actor);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_threads_surface_external
  ON agent_threads(project_id, surface, external_ref, agent_role)
  WHERE external_ref IS NOT NULL;
"""


class _Settings:
    def __init__(self, path):
        self.schema_path = path


@pytest.fixture()
def private_schema():
    """A schema that exists only for this test: the engine's search_path points into it."""
    name = f"idx_drift_{uuid.uuid4().hex[:10]}"
    admin = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={name}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


def _indexdef(engine, name: str) -> str:
    with engine.connect() as c:
        return str(c.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = :n"), {"n": name}).scalar_one())


def _oid(engine, name: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT CAST(:n AS regclass)::oid"), {"n": name}).scalar_one())


def test_the_old_three_column_index_becomes_the_declared_four_column_one(private_schema,
                                                                          tmp_path):
    engine = private_schema
    with engine.begin() as c:
        c.execute(text(AGENT_THREADS.split(";")[0]))
        # The shared server's index: the definition schema.sql had before agent_role joined the key.
        c.execute(text(
            "CREATE UNIQUE INDEX idx_agent_threads_surface_external "
            "ON agent_threads(project_id, surface, external_ref) WHERE external_ref IS NOT NULL"))
        # An index schema.sql does not declare: an operator's own, never ours to touch.
        c.execute(text("CREATE INDEX operator_own_idx ON agent_threads(actor, surface)"))
        c.execute(text("INSERT INTO agent_threads (id, project_id, actor, external_ref, "
                       "agent_role) VALUES ('athr_1', 'boss-dogfood', 'ceo', "
                       "'role:Program Manager', '')"))
    script = tmp_path / "schema.sql"
    script.write_text(AGENT_THREADS)

    ensure_schema(engine, _Settings(script))

    fixed = _indexdef(engine, "idx_agent_threads_surface_external")
    assert "agent_role" in fixed and "UNIQUE" in fixed and "external_ref IS NOT NULL" in fixed
    assert _indexdef(engine, "operator_own_idx").endswith("(actor, surface)")
    # The thread that existed survives, and the second agent can now have its own.
    with engine.begin() as c:
        c.execute(text("INSERT INTO agent_threads (id, project_id, actor, external_ref, "
                       "agent_role) VALUES ('athr_2', 'boss-dogfood', 'ceo', "
                       "'role:Program Manager', 'Program Manager')"))
        repairs = c.execute(text(
            "SELECT repairs FROM schema_apply_runs ORDER BY id DESC LIMIT 1")).scalar_one()
    assert any("idx_agent_threads_surface_external" in r for r in repairs), repairs


def test_an_index_that_already_matches_is_left_alone(private_schema, tmp_path):
    """No churn: a boot must not rebuild every index it finds."""
    engine = private_schema
    script = tmp_path / "schema.sql"
    script.write_text(AGENT_THREADS)
    ensure_schema(engine, _Settings(script))
    before = _oid(engine, "idx_agent_threads_surface_external")
    ensure_schema(engine, _Settings(script))
    assert _oid(engine, "idx_agent_threads_surface_external") == before
    with engine.connect() as c:
        repairs = c.execute(text(
            "SELECT repairs FROM schema_apply_runs ORDER BY id DESC LIMIT 1")).scalar_one()
    assert repairs == []


def test_a_rebuild_that_the_data_refuses_keeps_the_old_index_and_says_so(private_schema,
                                                                         tmp_path):
    """Narrowing a unique index over rows that violate it cannot succeed; the old index stays."""
    engine = private_schema
    with engine.begin() as c:
        c.execute(text("CREATE TABLE t (a int, b int)"))
        c.execute(text("CREATE UNIQUE INDEX t_ab ON t(a, b)"))
        c.execute(text("INSERT INTO t VALUES (1, 1), (1, 2)"))
    script = tmp_path / "schema.sql"
    script.write_text("CREATE TABLE IF NOT EXISTS t (a int, b int);\n"
                      "CREATE UNIQUE INDEX IF NOT EXISTS t_ab ON t(a);\n")
    ensure_schema(engine, _Settings(script))
    assert _indexdef(engine, "t_ab").endswith("(a, b)")
    with engine.connect() as c:
        failures = c.execute(text(
            "SELECT failures FROM schema_apply_runs ORDER BY id DESC LIMIT 1")).scalar_one()
    assert any("t_ab" in f and "could not be rebuilt" in f for f in failures), failures


def test_every_index_schema_sql_declares_matches_after_apply(private_schema):
    """The real file, all of it: after one apply every declared index exists and a second
    convergence finds nothing to change — so the comparison never mistakes Postgres's own
    rendering of a predicate or an expression for drift (which would rebuild it every boot)."""
    engine = private_schema
    ensure_schema(engine, _Settings(SCHEMA_SQL))
    declared = declared_indexes(SCHEMA_SQL.read_text(encoding="utf-8"))
    assert len(declared) > 300
    with engine.connect() as c:
        live = {r[0] for r in c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))}
    missing = [d["name"] for d in declared if d["name"] not in live]
    assert not missing, missing
    raw = engine.raw_connection()
    try:
        repairs, failures = _converge_indexes(raw, SCHEMA_SQL.read_text(encoding="utf-8"))
    finally:
        raw.close()
    assert repairs == [] and failures == [], (repairs, failures)


def test_a_huge_table_is_reported_not_rebuilt_during_a_boot(private_schema, tmp_path,
                                                            monkeypatch):
    """A CREATE INDEX inside a transaction blocks writers for the whole build, and CONCURRENTLY
    cannot run in one — so past the size bound the drift is reported with the statements to run
    by hand, and the old index stays."""
    from boss_engine import db

    monkeypatch.setattr(db, "INDEX_REBUILD_MAX_PAGES", -1)
    engine = private_schema
    with engine.begin() as c:
        c.execute(text("CREATE TABLE big (a int, b int)"))
        c.execute(text("CREATE INDEX big_ab ON big(a, b)"))
    script = tmp_path / "schema.sql"
    script.write_text("CREATE TABLE IF NOT EXISTS big (a int, b int);\n"
                      "CREATE INDEX IF NOT EXISTS big_ab ON big(a);\n")
    ensure_schema(engine, _Settings(script))
    assert _indexdef(engine, "big_ab").endswith("(a, b)")
    with engine.connect() as c:
        failures = c.execute(text(
            "SELECT failures FROM schema_apply_runs ORDER BY id DESC LIMIT 1")).scalar_one()
    assert any("big_ab" in f and "DROP INDEX CONCURRENTLY" in f
               and "CREATE INDEX CONCURRENTLY IF NOT EXISTS big_ab" in f for f in failures), failures
