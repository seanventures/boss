"""BOSSAPP-826: the workspace_snapshots block is idempotent on real Postgres,
and the service's SQL runs there (not only on the SQLite unit suite).

A disposable database `boss_test_826` (the name carries 'test', the db_guard
rule) is created here and dropped after. Needs BOSS_TEST_PG_URL (or
BOSS_TEST_DATABASE_URL) naming a Postgres that may create databases; skips
without one.
"""
from __future__ import annotations

import os
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "boss_test_826"
START = "-- WORKSPACE SNAPSHOTS (BOSSAPP-826)"
END = "-- ================================ end WORKSPACE SNAPSHOTS (BOSSAPP-826)"


def admin_url() -> str:
    url = os.environ.get("BOSS_TEST_PG_URL") or os.environ.get("BOSS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("no BOSS_TEST_PG_URL / BOSS_TEST_DATABASE_URL (a disposable Postgres)")
    url = url.replace("postgresql+psycopg://", "postgresql://")
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


def block() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    return sql[sql.index(START):sql.index(END)]


def statements(sql: str) -> list[str]:
    from boss_engine.db import split_statements

    return split_statements(sql)


@pytest.fixture()
def database():
    psycopg = pytest.importorskip("psycopg")
    admin = admin_url()
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
        c.execute(f'CREATE DATABASE "{DB_NAME}"')
    try:
        yield admin.rsplit("/", 1)[0] + f"/{DB_NAME}"
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def test_the_block_applies_twice_and_its_constraints_hold(database):
    import psycopg

    with psycopg.connect(database, autocommit=True) as c:
        for _round in (1, 2):  # idempotency IS the assertion
            for statement in statements(block()):
                c.execute(statement)
        columns = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'workspace_snapshots'")}
        assert {"run_id", "generation", "object_key", "digest", "bytes", "expires_at",
                "created_at", "kind", "seed_key"} <= columns
        c.execute("INSERT INTO workspace_snapshots (id, run_id, root_run_id, kind, generation,"
                  " object_key, digest) VALUES ('w1', 'r', 'r', 'full', 1, 'k1', 'd')")
        with pytest.raises(psycopg.errors.UniqueViolation):  # one generation per lineage
            c.execute("INSERT INTO workspace_snapshots (id, run_id, root_run_id, kind,"
                      " generation, object_key, digest) VALUES ('w2', 'r2', 'r', 'full', 1,"
                      " 'k2', 'd')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO workspace_snapshots (id, run_id, kind, object_key, digest)"
                      " VALUES ('w3', 'r', 'partial', 'k3', 'd')")
        c.execute("INSERT INTO workspace_snapshots (id, run_id, project_id, repo, kind,"
                  " seed_key, object_key, digest) VALUES ('s1', 'r', 'p', 'a/b', 'seed',"
                  " 'h', 'k4', 'd')")
        with pytest.raises(psycopg.errors.UniqueViolation):  # one seed per lockfile state
            c.execute("INSERT INTO workspace_snapshots (id, run_id, project_id, repo, kind,"
                      " seed_key, object_key, digest) VALUES ('s2', 'r2', 'p', 'a/b', 'seed',"
                      " 'h', 'k5', 'd')")


def test_the_block_is_end_appended_once_after_every_earlier_block():
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE IF NOT EXISTS workspace_snapshots") == 1
    start = sql.index("-- BOSSAPP-826")
    assert start > sql.index("end EXECUTION ENVIRONMENTS (BOSSAPP-824)")
    assert not sql[sql.index(END) + len(END):].strip().strip("=-").strip()
    for statement in statements(block()):
        head = " ".join(statement.split())[:80].upper()
        if head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in statement.upper(), head


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_capture_restore_and_reap_run_on_postgres(database, tmp_path):
    """The service's SQL (ISO-string timestamps, JSONB cast, ON CONFLICT, the
    partial unique indexes) against the real dialect."""
    from sqlalchemy import create_engine, text

    from boss_engine.services import workspace_snapshots as ws
    from boss_engine.services.object_store import DirectoryObjectStore

    from .test_workspace_snapshots_826 import ctx_for, clone, install_deps, settings

    engine = create_engine(database.replace("postgresql://", "postgresql+psycopg://"))
    try:
        with engine.begin() as c:
            for statement in statements(block()):
                c.exec_driver_sql(statement)
        origin = tmp_path / "origin"
        origin.mkdir()
        _git(origin, "init", "-q", "-b", "main")
        (origin / "package-lock.json").write_text("{}\n")
        (origin / ".gitignore").write_text("node_modules/\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-q", "-m", "init")
        store = DirectoryObjectStore(tmp_path / "store")
        box = clone(origin, tmp_path / "box1")
        install_deps(box)
        (box.workdir / "new.txt").write_text("work\n")
        ctx = ctx_for(engine, store, run_id="crun_a", root_run_id="crun_a")
        assert ws.capture(ctx, box, "write_tests")["seed"] == "written"
        assert ws.capture(ctx, box, "implement")["seed"] == "reused"
        fresh = clone(origin, tmp_path / "box2")
        result = ws.restore(ctx_for(engine, store, run_id="crun_b", root_run_id="crun_a"),
                            fresh, is_repair=True)
        assert result["generation"] == 2 and result["deps"] == ["node_modules"]
        assert (fresh.workdir / "new.txt").read_text() == "work\n"
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        with engine.begin() as c:
            c.execute(text("UPDATE workspace_snapshots SET expires_at = :p WHERE kind = 'full'"),
                      {"p": past})
        assert ws.reap_expired(engine, settings(), store=store) == {"expired": 2, "failed": 0}
        with engine.begin() as c:
            kinds = [r[0] for r in c.execute(text("SELECT kind FROM workspace_snapshots"))]
        assert kinds == ["seed"]
    finally:
        engine.dispose()
