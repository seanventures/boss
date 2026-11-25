"""db/schema.sql must apply to a FRESH database with zero failed statements.

Born 2026-11-13: four feature branches each appended their block to the schema tail, and the
merge-conflict stacking glued two statements together. Every table still existed in the file,
`IF NOT EXISTS` hid the damage on live databases that predated it, and nothing gated a clean
apply — so a fresh install silently lost the chat tables. This test is that gate: it applies
the whole file to an empty database and fails on ANY statement the apply had to skip."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text


@pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"),
                    reason="needs BOSS_TEST_DATABASE_URL (a postgres the test may create DBs on)")
def test_schema_applies_to_a_fresh_database_with_zero_failures(capfd):
    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema, split_statements

    admin = create_engine(os.environ["BOSS_TEST_DATABASE_URL"])
    name = f"boss_schema_clean_{uuid.uuid4().hex[:10]}"
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        scratch = create_engine(admin.url.set(database=name))
        # First fresh apply + second idempotent re-apply (BOSSAPP-1310).
        for attempt in (1, 2):
            ensure_schema(scratch)
            err = capfd.readouterr()
            failed = [line for line in (err.out + err.err).splitlines() if "FAILED" in line]
            assert not failed, f"ensure_schema attempt {attempt} failed:\n" + "\n".join(failed[:20])
        with scratch.connect() as c:
            runs = c.execute(
                text("SELECT id, statements, failed, failures FROM schema_apply_runs ORDER BY id")
            ).mappings().all()
        assert len(runs) == 2
        for r in runs:
            assert r["statements"] > 0
            assert r["failed"] == 0, f"schema_apply_runs row had failures: {dict(r)}"
            assert list(r["failures"] or []) == []
        scratch.dispose()

        # Also verify raw ON_ERROR_STOP-style sequential execution of db/schema.sql twice (no SAVEPOINT swallowing).
        raw_name = f"boss_schema_raw_{uuid.uuid4().hex[:10]}"
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f'CREATE DATABASE "{raw_name}"'))
        try:
            raw_eng = create_engine(admin.url.set(database=raw_name))
            sql = get_settings().schema_path.read_text(encoding="utf-8")
            stmts = split_statements(sql)
            raw_conn = raw_eng.raw_connection()
            try:
                for attempt in (1, 2):
                    with raw_conn.cursor() as cur:
                        for stmt in stmts:
                            cur.execute(stmt)
                    raw_conn.commit()
            finally:
                raw_conn.close()
                raw_eng.dispose()
        finally:
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{raw_name}"'))
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
