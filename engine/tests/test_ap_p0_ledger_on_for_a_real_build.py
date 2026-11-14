"""AP-P0 (BOSSAPP-1051) acceptance (a): the run ledger is ON by default, and one real build's run
reads back from the `run_ledger` view joined across llm_calls, tool_call_audit and run_events.

docs/design/AGENT_PLATFORM.md §2.10 found the unified ledger written "only when
BOSS_LEDGER_EVENTS is set, and only a test sets it". This drives one build through BOSS's real
governed pipeline (the scripted harness bench, the same harness test_harness_bench_e2e uses),
with BOSS_LEDGER_EVENTS left UNSET, and reads the ledger for that build's run.

It also pins the retention the flag flip needs: next months' partitions are made by the sweep,
not only at boot, and a month past the window is dropped as a whole partition.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "scripts" / "harness-bench"
PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
#: Its own database: test_harness_bench_e2e drops boss_test_bench_e2e WITH (FORCE).
DB_NAME = "boss_test_ap_p0_ledger"
TASK = "helpdesk-classify-case"


def test_the_flag_is_on_unless_switched_off(monkeypatch):
    from boss_engine.kernel import ledger

    monkeypatch.delenv("BOSS_LEDGER_EVENTS", raising=False)
    assert ledger.enabled()
    for off in ("0", "false", "OFF", " no "):
        monkeypatch.setenv("BOSS_LEDGER_EVENTS", off)
        assert not ledger.enabled(), off
    monkeypatch.setenv("BOSS_LEDGER_EVENTS", "1")
    assert ledger.enabled()


def test_the_community_compose_forwards_it_on_to_api_and_worker():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "scripts/install/community-compose.yml").read_text())
    for svc in ("api", "worker"):
        env = compose["services"][svc]["environment"]
        assert env["BOSS_LEDGER_EVENTS"] == "${BOSS_LEDGER_EVENTS:-1}", svc
        assert env["BOSS_LEDGER_RETENTION_DAYS"] == "${BOSS_LEDGER_RETENTION_DAYS:-400}", svc


def test_only_months_wholly_past_the_window_expire():
    from boss_engine.services.ledger_retention import expired_partitions, retention_days

    names = ["run_events_2025_01", "run_events_2025_10", "run_events_2026_11",
             "run_events_default", "something_else"]
    got = expired_partitions(names, today=date(2026, 11, 14), days=400)
    # 2025_10 ends 2025-11-01: 378 days ago, inside the window.
    assert got == ["run_events_2025_01"]
    os.environ["BOSS_LEDGER_RETENTION_DAYS"] = "3"
    try:
        assert retention_days() == 30      # the floor holds
    finally:
        del os.environ["BOSS_LEDGER_RETENTION_DAYS"]


@pytest.fixture(scope="module")
def bench_db():
    if not PG_URL:
        pytest.skip("needs BOSS_TEST_DATABASE_URL (a Postgres the suite may create databases on)")
    if not any((Path(p) / "node").exists() for p in os.environ.get("PATH", "").split(os.pathsep)):
        pytest.skip("the HelpDesk task needs node on PATH")
    url = make_url(PG_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create {DB_NAME}: {type(exc).__name__}")
    yield url.set(database=DB_NAME).render_as_string(hide_password=False)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
    admin.dispose()


def _bench(*args, db_url: str, ledger: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "BOSS_TEST_DATABASE_URL", "BOSS_LEDGER_EVENTS")}
    if ledger is not None:
        env["BOSS_LEDGER_EVENTS"] = ledger
    env["BOSS_BENCH_DATABASE_URL"] = db_url
    return subprocess.run([str(BENCH), *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=900)


def _one_build(db_url: str, tmp: Path, *, ledger: str | None) -> str:
    out = tmp / f"report-{ledger}.json"
    done = _bench("run", "--engine", "scripted", "--tasks", TASK, "--report", str(out),
                  db_url=db_url, ledger=ledger)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    (r,) = json.loads(out.read_text())["results"]
    assert r["success"] is True and r["code_task_run_id"], r.get("components")
    return r["code_task_run_id"]


def _volume(c, run_id: str) -> dict:
    """Rows a build added, per table (llm_calls and tool_call_audit rows exist either way; the
    ledger fills their run columns and adds run_events rows)."""
    return {
        "llm_calls": c.execute(text("SELECT count(*) FROM llm_calls WHERE code_task_run_id = :i"),
                               {"i": run_id}).scalar(),
        "llm_calls_on_ledger": c.execute(text(
            "SELECT count(*) FROM llm_calls WHERE code_task_run_id = :i AND seq IS NOT NULL"),
            {"i": run_id}).scalar(),
        "tool_call_audit": c.execute(text(
            "SELECT count(*) FROM tool_call_audit WHERE run_id = :i"), {"i": run_id}).scalar(),
        "run_events": c.execute(text("SELECT count(*) FROM run_events WHERE run_id = :i"),
                                {"i": run_id}).scalar(),
    }


def test_one_real_build_reads_back_from_the_run_ledger(bench_db, tmp_path):
    on_run = _one_build(bench_db, tmp_path, ledger=None)      # the flag left UNSET
    off_run = _one_build(bench_db, tmp_path, ledger="0")
    eng = create_engine(bench_db, future=True)
    try:
        # The scripted build makes no MCP-gateway call (its tools are its own file writes), so
        # the build's one governed tool call is made through the gateway's own writer, exactly
        # as mcp_gateway records a call for the build's run (grant.run_key), with the flag unset.
        os.environ.pop("BOSS_LEDGER_EVENTS", None)
        from boss_engine.services.tool_call_audit import record_tool_call

        with eng.begin() as c:
            project_id = c.execute(text("SELECT project_id FROM code_task_runs WHERE id = :i"),
                                   {"i": on_run}).scalar()
            record_tool_call(c, project_id=project_id, run_id=on_run, agent="mcp_gateway",
                             principal="engineer", server_id=None, tool="lookup_ticket",
                             arguments={"id": "T-1"}, decision="allow", matched_policy_ids=[],
                             decision_id=None, outcome="ok", latency_ms=3)
        with eng.connect() as c:
            by_src = dict(c.execute(text(
                "SELECT src, count(*) FROM run_ledger WHERE run_id = :i GROUP BY src"),
                {"i": on_run}).all())
            on, off = _volume(c, on_run), _volume(c, off_run)
            # The join is real: the model rows and the event rows of the run interleave on one
            # sequence, and every model call of the build is on it.
            seqs = [r[0] for r in c.execute(text(
                "SELECT seq FROM run_ledger WHERE run_id = :i ORDER BY seq"), {"i": on_run})]
            tool_runs = c.execute(text(
                "SELECT count(*) FROM tool_call_audit WHERE run_id = :i AND seq IS NULL"),
                {"i": on_run}).scalar()
    finally:
        eng.dispose()
    print(f"\nLEDGER VOLUME per scripted build: on={on} off={off} run_ledger={by_src}")
    assert by_src.get("model", 0) > 0 and by_src.get("event", 0) > 0, by_src
    assert by_src.get("tool") == on["tool_call_audit"] == 1, by_src
    assert on["llm_calls_on_ledger"] == on["llm_calls"] > 0
    assert tool_runs == 0
    assert len(seqs) == len(set(seqs)) and seqs == sorted(seqs)
    assert off["run_events"] == 0 and off["llm_calls_on_ledger"] == 0


def test_the_sweep_makes_months_ahead_and_drops_an_expired_one(bench_db):
    from boss_engine.services.ledger_retention import prune

    eng = create_engine(bench_db, future=True)
    try:
        with eng.begin() as c:
            c.execute(text("CREATE TABLE IF NOT EXISTS run_events_2020_01 PARTITION OF run_events "
                           "FOR VALUES FROM ('2020-01-01') TO ('2020-02-01')"))
        with eng.begin() as c:
            got = prune(c, now=datetime.now(timezone.utc))
        assert "run_events_2020_01" in got["dropped"] and not got.get("error"), got
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('run_events_2020_01')")).scalar() is None
            ahead = date.today().replace(day=1)
            ahead = date(ahead.year + (ahead.month + 1) // 13, (ahead.month % 12) + 1, 1)
            name = f"run_events_{ahead:%Y_%m}"
            assert c.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is not None
    finally:
        eng.dispose()
