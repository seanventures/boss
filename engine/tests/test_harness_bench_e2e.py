"""END TO END: the scripted harness benchmark on one task, through BOSS's real governed pipeline.

`scripts/harness-bench run --engine scripted --tasks helpdesk-classify-case` on a fresh boss_test*
database: the build opens a PR on the local GitHub, the held acceptance tests pass on its head, the
numbers come from the ledger (no cost, a fixed number of model calls), and the run is stored. Then
the same task with a fault that costs a fix round, against the first run as its baseline: the
comparison must say WORSE and exit 1. And the runner refuses a database not named boss_test*.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "scripts" / "harness-bench"
PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
DB_NAME = "boss_test_bench_e2e"
TASK = "helpdesk-classify-case"


@pytest.fixture(scope="module")
def bench_db():
    if not PG_URL:
        pytest.skip("needs BOSS_TEST_DATABASE_URL (a Postgres the suite may create databases on)")
    if not (os.environ.get("PATH") and any((Path(p) / "node").exists()
                                           for p in os.environ["PATH"].split(os.pathsep))):
        pytest.skip("the HelpDesk task needs node on PATH")
    url = make_url(PG_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create {DB_NAME}: {type(exc).__name__}")
    target = url.set(database=DB_NAME).render_as_string(hide_password=False)
    yield target
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
    admin.dispose()


def _bench(*args, db_url: str, timeout: int = 600) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "BOSS_TEST_DATABASE_URL")}
    env["BOSS_BENCH_DATABASE_URL"] = db_url
    return subprocess.run([str(BENCH), *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_the_runner_refuses_a_database_not_named_boss_test():
    done = _bench("run", "--engine", "scripted", "--tasks", TASK,
                  db_url="postgresql+psycopg://u:p@localhost:5432/boss")
    assert done.returncode == 2
    assert "boss_test" in done.stderr


def test_a_real_engine_needs_a_budget():
    done = _bench("run", "--engine", "claude_code", "--tasks", TASK, "--allow-unisolated",
                  db_url="postgresql+psycopg://u:p@localhost:5432/boss_test_x")
    assert done.returncode == 2
    assert "budget" in done.stderr or "not on PATH" in done.stderr


def test_one_scripted_task_end_to_end_and_a_regression_is_caught(bench_db, tmp_path):
    first = tmp_path / "first.json"
    done = _bench("run", "--engine", "scripted", "--tasks", TASK, "--report", str(first),
                  db_url=bench_db)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    report = json.loads(first.read_text())
    assert report["run"]["status"] == "completed"
    (r,) = report["results"]
    assert r["success"] is True, r.get("components")
    assert all(r["components"].values())
    assert r["pull_request_url"]
    assert any("held acceptance tests" in c["check"] and c["ok"] for c in r["acceptance"]["checks"])
    m = r["metrics"]
    assert m["cost_usd"] == 0 and m["model_calls"] > 0 and m["turns"] == m["model_calls"]
    assert m["tokens_in"] > 0 and m["interventions"] == 0 and m["repair_rounds"] == 0

    eng = create_engine(bench_db, future=True)
    try:
        with eng.connect() as c:
            row = c.execute(text("SELECT status, engine, level FROM harness_bench_runs WHERE id = :i"),
                            {"i": report["run"]["id"]}).mappings().one()
            assert dict(row) == {"status": "completed", "engine": "scripted", "level": "delegate"}
            calls = c.execute(text("SELECT model_calls, success FROM harness_bench_results "
                                   "WHERE bench_run_id = :i"), {"i": report["run"]["id"]}).one()
            assert calls == (m["model_calls"], True)
    finally:
        eng.dispose()

    same = _bench("compare", str(first), "--baseline", str(first), db_url=bench_db)
    assert same.returncode == 0 and "VERDICT: SAME" in same.stdout, same.stdout

    worse = _bench("run", "--engine", "scripted", "--tasks", TASK, "--scripted-faults",
                   "failing_test", "--baseline", str(first), "--report", str(tmp_path / "w.json"),
                   db_url=bench_db)
    assert worse.returncode == 1, worse.stdout[-3000:]
    assert "VERDICT: WORSE" in worse.stdout
    assert "repair_rounds" in worse.stdout and "FAIL" in worse.stdout

    score = _bench("report", db_url=bench_db)
    assert score.returncode == 0 and "scripted / delegate" in score.stdout
