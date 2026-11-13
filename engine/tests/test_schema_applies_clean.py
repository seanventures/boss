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
    from boss_engine.db import ensure_schema

    admin = create_engine(os.environ["BOSS_TEST_DATABASE_URL"])
    name = f"boss_schema_clean_{uuid.uuid4().hex[:10]}"
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        scratch = create_engine(admin.url.set(database=name))
        ensure_schema(scratch)
        scratch.dispose()
        err = capfd.readouterr()
        failed = [l for l in (err.out + err.err).splitlines() if "FAILED" in l]
        assert not failed, "schema.sql did not apply cleanly to a fresh database:\n" + "\n".join(failed[:20])
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
