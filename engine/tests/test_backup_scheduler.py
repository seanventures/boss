"""SCHEDULED SELF-BACKUPS (BOSSAPP-838): the lease, the tiers, verification, and health.

The clock is always passed in, so nothing here waits. The dump and the verification are a fake
runner (the real pg_dump/pg_restore live in the api image); the rows are real Postgres, in a
disposable boss_test* database.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import backup_scheduler as bs
from boss_engine.services import system_doctor

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
SCHEMA = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()


def _ddl() -> list[str]:
    tail = SCHEMA[SCHEMA.index("-- BOSSAPP-838"):]
    return [s.strip() for s in tail.split(";") if s.strip() and "CREATE" in s]


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not url or "test" not in name:
        pytest.skip("needs BOSS_TEST_DATABASE_URL naming a disposable boss_test* database")
    eng = create_engine(url, future=True)
    try:
        with eng.begin() as c:
            for stmt in _ddl():
                c.execute(text(stmt))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test postgres unreachable: {type(exc).__name__}")
    yield eng
    eng.dispose()


@pytest.fixture()
def clean(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM backups"))
        c.execute(text("DELETE FROM backup_scheduler_lease"))
    return engine


def _settings(tmp_path: Path, **over):
    base = dict(backup_schedule_enabled=True, backup_interval_minutes=60,
                backup_dir=str(tmp_path / "backups"), backup_keep_hourly=3,
                backup_keep_daily=2, backup_keep_weekly=1, backup_keep_monthly=1,
                backup_s3_upload=False, backup_s3_prefix="boss-backups", s3_bucket=None,
                backup_pg_dump="pg_dump", backup_pg_restore="pg_restore",
                database_url="postgresql+psycopg://boss:not-a-real-password@db:5432/boss")
    base.update(over)
    return SimpleNamespace(**base)


class FakeRunner:
    """pg_dump writes bytes to -f; pg_restore --list answers as told."""

    def __init__(self, *, dump_rc=0, list_rc=0, list_out="; Archive\n123; 1259 TABLE public x\n"):
        self.dump_rc, self.list_rc, self.list_out = dump_rc, list_rc, list_out
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.envs.append(kw.get("env") or {})
        if argv[0] == "pg_dump":
            if self.dump_rc == 0:
                Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP fake archive")
            return subprocess.CompletedProcess(argv, self.dump_rc, "", "dump broke")
        return subprocess.CompletedProcess(argv, self.list_rc, self.list_out,
                                           "pg_restore: error: input file is not a valid archive")


# ------------------------------------------------------------------------------------------ #
# retention (pure)
# ------------------------------------------------------------------------------------------ #

def _hourly(n: int, end: datetime) -> list[dict]:
    return [{"id": f"b{i}", "started_at": end - timedelta(hours=i)} for i in range(n)]


def test_retention_keeps_newest_hourly_and_one_per_day_week_month():
    rows = _hourly(24 * 70, T0)       # 70 days of hourly backups
    policy = bs.Policy(keep_hourly=24, keep_daily=7, keep_weekly=4, keep_monthly=3)
    keep = bs.retention_keep(rows, policy)
    assert {f"b{i}" for i in range(24)} <= keep                    # the last 24 hours
    days = {next(r for r in rows if r["id"] == k)["started_at"].date() for k in keep}
    assert len([d for d in days if d >= (T0 - timedelta(days=6)).date()]) == 7
    months = {(r["started_at"].year, r["started_at"].month) for r in rows if r["id"] in keep}
    assert len(months) == 3                                          # Nov, Oct, Sep
    # Bounded: 24 hourly + at most 7 + 4 + 3 others, nothing like the 1680 that exist.
    assert len(keep) <= 24 + 7 + 4 + 3
    assert "b0" in keep


def test_retention_always_keeps_the_newest_even_with_zero_tiers():
    rows = _hourly(5, T0)
    keep = bs.retention_keep(rows, bs.Policy(keep_hourly=1, keep_daily=0, keep_weekly=0,
                                             keep_monthly=0))
    assert keep == {"b0"}


# ------------------------------------------------------------------------------------------ #
# the lease
# ------------------------------------------------------------------------------------------ #

def test_exactly_one_holder_until_the_lease_expires(clean):
    with clean.begin() as c:
        assert bs.acquire_lease(c, "api-a", T0)
        assert not bs.acquire_lease(c, "api-b", T0 + timedelta(minutes=1))
        assert bs.acquire_lease(c, "api-a", T0 + timedelta(minutes=5))      # renews
        assert not bs.acquire_lease(c, "api-b", T0 + timedelta(minutes=14))
        # a renewed lease ran to T0+15min; api-a died, so api-b takes it after that
        assert bs.acquire_lease(c, "api-b", T0 + timedelta(minutes=16))
        assert not bs.acquire_lease(c, "api-a", T0 + timedelta(minutes=17))


def test_a_second_process_never_backs_up_while_the_first_holds_the_lease(clean, tmp_path):
    s, run = _settings(tmp_path), FakeRunner()
    assert bs.tick(clean, s, now=T0, holder="api-a", runner=run)["ran"] is True
    later = T0 + timedelta(hours=2)
    out = bs.tick(clean, s, now=T0 + timedelta(minutes=2), holder="api-b", runner=run)
    assert out == {"ran": False, "reason": "another process holds the lease"}
    assert bs.tick(clean, s, now=T0 + timedelta(minutes=30), holder="api-a",
                   runner=run)["reason"] == "not due"
    assert bs.tick(clean, s, now=later, holder="api-a", runner=run)["ran"] is True


def test_disabled_does_nothing(clean, tmp_path):
    out = bs.tick(clean, _settings(tmp_path, backup_schedule_enabled=False), now=T0)
    assert out == {"ran": False, "reason": "disabled"}


# ------------------------------------------------------------------------------------------ #
# one backup: dump, verify, record
# ------------------------------------------------------------------------------------------ #

def test_a_verified_backup_is_recorded_with_size_and_checksum(clean, tmp_path):
    s, run = _settings(tmp_path), FakeRunner()
    row = bs.run_backup(clean, s, now=T0, runner=run)
    assert row["status"] == "verified"
    assert Path(row["path"]).exists() and not Path(row["path"] + ".partial").exists()
    assert row["checksum"].startswith("sha256:") and row["bytes"] == len(b"PGDMP fake archive")
    assert [c[0] for c in run.calls] == ["pg_dump", "pg_restore"]
    assert run.calls[1][:2] == ["pg_restore", "--list"]
    # The password reaches the child's environment, never its argv.
    assert all("not-a-real-password" not in a for a in run.calls[0])
    assert run.envs[0]["PGPASSWORD"] == "not-a-real-password"
    with clean.begin() as c:
        db = c.execute(text("SELECT * FROM backups WHERE id = :i"), {"i": row["id"]}).mappings().one()
    assert db["status"] == "verified" and db["verified_at"] and db["finished_at"]


def test_a_dump_that_does_not_list_is_marked_failed_and_removed(clean, tmp_path):
    s = _settings(tmp_path)
    row = bs.run_backup(clean, s, now=T0, runner=FakeRunner(list_rc=1))
    assert row["status"] == "failed"
    assert "pg_restore --list failed" in row["error"]
    assert not Path(row["path"]).exists()
    with clean.begin() as c:
        db = c.execute(text("SELECT status, verified_at, error FROM backups WHERE id = :i"),
                       {"i": row["id"]}).mappings().one()
        assert db["status"] == "failed" and db["verified_at"] is None and db["error"]
        assert bs.health(c, s, now=T0 + timedelta(minutes=5))["state"] == "failed"


def test_a_failed_dump_is_marked_failed(clean, tmp_path):
    row = bs.run_backup(clean, _settings(tmp_path), now=T0, runner=FakeRunner(dump_rc=1))
    assert row["status"] == "failed" and "pg_dump failed" in row["error"]


def test_a_missing_pg_dump_is_a_failure_not_a_crash(clean, tmp_path):
    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])
    row = bs.run_backup(clean, _settings(tmp_path), now=T0, runner=missing)
    assert row["status"] == "failed" and "FileNotFoundError" in row["error"]


def test_an_upload_failure_keeps_the_local_copy_and_says_so(clean, tmp_path):
    s = _settings(tmp_path, backup_s3_upload=True, s3_bucket="b")

    def boom(path, key):
        raise RuntimeError("bucket unreachable")
    row = bs.run_backup(clean, s, now=T0, runner=FakeRunner(), uploader=boom)
    assert row["status"] == "verified" and row["error"].startswith("upload failed")
    assert Path(row["path"]).exists()
    with clean.begin() as c:
        assert bs.health(c, s, now=T0 + timedelta(minutes=5))["state"] == "failed"
    uploaded = []
    row2 = bs.run_backup(clean, s, now=T0 + timedelta(hours=1), runner=FakeRunner(),
                         uploader=lambda p, k: uploaded.append(k))
    assert row2["object_key"] == f"boss-backups/{Path(row2['path']).name}" == uploaded[0]


def test_an_interrupted_running_row_is_closed_as_failed(clean, tmp_path):
    with clean.begin() as c:
        c.execute(text("INSERT INTO backups (id, started_at, status) VALUES ('old', :t, 'running')"),
                  {"t": T0 - timedelta(hours=5)})
        assert bs.mark_interrupted(c, T0) == 1
        assert c.execute(text("SELECT status FROM backups WHERE id='old'")).scalar() == "failed"


# ------------------------------------------------------------------------------------------ #
# pruning through the scheduler, over simulated days
# ------------------------------------------------------------------------------------------ #

def test_ticks_over_three_days_prune_down_to_the_tiers(clean, tmp_path):
    s, run = _settings(tmp_path), FakeRunner()
    removed: list[str] = []
    for hour in range(72):
        bs.tick(clean, s, now=T0 + timedelta(hours=hour), holder="api-a", runner=run,
                remover=removed.append)
    with clean.begin() as c:
        verified = c.execute(text("SELECT id, path FROM backups WHERE status='verified'")).all()
        pruned = c.execute(text("SELECT path FROM backups WHERE status='pruned'")).all()
    # keep_hourly=3, keep_daily=2 (today's newest is already hourly; +1 for yesterday),
    # weekly=1 and monthly=1 are the newest → 4 verified remain.
    assert len(verified) == 4
    assert len(pruned) == 72 - 4
    assert all(Path(p).exists() for _, p in verified)
    assert not any(Path(p).exists() for (p,) in pruned)
    files = sorted(Path(s.backup_dir).glob("*.dump"))
    assert len(files) == 4


# ------------------------------------------------------------------------------------------ #
# health → the system doctor
# ------------------------------------------------------------------------------------------ #

def test_health_states(clean, tmp_path):
    s = _settings(tmp_path)
    with clean.begin() as c:
        assert bs.health(c, _settings(tmp_path, backup_schedule_enabled=False))["state"] == "disabled"
        bs.acquire_lease(c, "api-a", T0)
        assert bs.health(c, s, now=T0 + timedelta(minutes=30))["state"] == "pending"
        assert bs.health(c, s, now=T0 + timedelta(hours=3))["state"] == "missing"
    bs.run_backup(clean, s, now=T0 + timedelta(hours=3), runner=FakeRunner())
    with clean.begin() as c:
        assert bs.health(c, s, now=T0 + timedelta(hours=4))["state"] == "ok"
        stale = bs.health(c, s, now=T0 + timedelta(hours=7))
        assert stale["state"] == "stale" and stale["age_minutes"] >= 180


@pytest.mark.parametrize("state,kind", [("failed", "backup_failed"), ("stale", "backup_stale"),
                                        ("missing", "backup_missing")])
def test_the_doctor_raises_a_card_for_a_backup_problem(state, kind):
    found = _diagnose_backup_only({"state": state, "error": "disk full", "interval_minutes": 60,
                                   "last_verified_at": "2026-11-12T01:00:00"})
    assert [f["kind"] for f in found] == [kind]
    assert found[0]["needs_choice"] and found[0]["auto"] == []
    keys = [o["key"] for o in found[0]["options"]]
    assert keys and all(k in system_doctor.OFFERED_OPTIONS for k in keys)
    if state == "failed":
        assert "disk full" in found[0]["detail"]


def test_the_doctor_says_nothing_when_backups_are_ok_or_off():
    for state in ("ok", "pending", "disabled"):
        assert _diagnose_backup_only({"state": state}) == []


def _diagnose_backup_only(backup):
    """diagnose() on otherwise calm signals, keeping only the backup findings."""
    from boss_engine.services.admission import Capacity

    calm = Capacity(total_mb=15991, available_mb=13029, cores=8, disk_free_gb=40,
                    disk_used_pct=48)
    sig = system_doctor.Signals(cap=calm, backup=backup)
    return [f for f in system_doctor.diagnose(sig) if f["kind"].startswith("backup_")]
