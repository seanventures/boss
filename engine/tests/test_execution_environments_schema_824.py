"""BOSSAPP-824 (e): the new schema block is idempotent on real Postgres.

The block is applied TWICE into a disposable database this test creates itself
(`boss_test_824` — the name carries 'test', the db_guard rule) and drops after.
Every statement must survive the second pass untouched, because ensure_schema
runs the whole of schema.sql on every deploy.

Needs a Postgres with the right to create databases, named by BOSS_TEST_PG_URL
(the test-tier convention) or derived from BOSS_TEST_DATABASE_URL; without one
the test skips — the SQLite suite next door covers the behaviour, this one
covers the DDL.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "boss_test_824"

START = "-- EXECUTION ENVIRONMENTS (BOSSAPP-824 phase 1)"
END = "-- ================================ end EXECUTION ENVIRONMENTS (BOSSAPP-824)"

#: The one table the block's foreign keys reach outside itself.
STUBS = "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY)"


def admin_url() -> str:
    url = os.environ.get("BOSS_TEST_PG_URL") or os.environ.get("BOSS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("no BOSS_TEST_PG_URL / BOSS_TEST_DATABASE_URL (a disposable Postgres)")
    url = url.replace("postgresql+psycopg://", "postgresql://")
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/postgres", "", ""))


def block() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = sql.index(START)
    end = sql.index(END)
    return sql[start:end]


def statements(sql: str) -> list[str]:
    from boss_engine.db import split_statements

    return split_statements(sql)


def test_the_block_applies_twice_without_error_or_drift():
    psycopg = pytest.importorskip("psycopg")

    admin = admin_url()
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
        c.execute(f'CREATE DATABASE "{DB_NAME}"')
    target = admin.rsplit("/", 1)[0] + f"/{DB_NAME}"
    try:
        with psycopg.connect(target, autocommit=True) as c:
            c.execute(STUBS)
            for _round in (1, 2):  # idempotency IS the assertion
                for statement in statements(block()):
                    c.execute(statement)
            tables = {r[0] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            assert {"execution_environments", "environment_leases"} <= tables
            # the driver CHECK holds
            c.execute(
                "INSERT INTO execution_environments (id, name, driver) "
                "VALUES ('env_1', 'box', 'ssh')")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(
                    "INSERT INTO execution_environments (id, name, driver) "
                    "VALUES ('env_2', 'box2', 'mainframe')")
            # the lease FK and state CHECK hold
            c.execute(
                "INSERT INTO environment_leases (id, environment_id, state) "
                "VALUES ('envl_1', 'env_1', 'held')")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(
                    "INSERT INTO environment_leases (id, environment_id, state) "
                    "VALUES ('envl_2', 'env_1', 'borrowed')")
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                c.execute(
                    "INSERT INTO environment_leases (id, environment_id) "
                    "VALUES ('envl_3', 'env_missing')")
            # org-wide names are unique too (the partial index, NULLs included)
            c.execute(
                "INSERT INTO execution_environments (id, name, driver) "
                "VALUES ('env_4', 'org-box', 'local')")
            with pytest.raises(psycopg.errors.UniqueViolation):
                c.execute(
                    "INSERT INTO execution_environments (id, name, driver) "
                    "VALUES ('env_5', 'org-box', 'local')")
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def test_the_block_is_in_schema_sql_exactly_once():
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE IF NOT EXISTS execution_environments") == 1
    assert sql.count("CREATE TABLE IF NOT EXISTS environment_leases") == 1
    # every statement in the block carries IF NOT EXISTS (the neighbors' shape)
    for statement in statements(block()):
        head = " ".join(statement.split())[:80].upper()
        if head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in statement.upper(), head
