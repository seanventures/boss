"""ST-14 (BOSSAPP-1159) findings, each fixed as a class and pinned here. The end-to-end proof
that found them is apps/api/tests/test_st14_a_full_disk_heals_itself_end_to_end.py.

1. A forecast fitted to minutes of readings said "full in hours" and made ONE bad reading a
   critical incident (the second reading agreed only because the slope did): no forecast until
   the readings span MIN_FORECAST_SPAN.
2. A scratch database's age was its directory's last-write time (not its birth; and a
   privileged call): the age is the stamp in its name, the label the test harness writes; an
   unstamped name is not automatic; the bytes reported are those of what was dropped.
3. An optional Postgres probe a role may not run (pg_ls_tmpdir, the backups table) aborted the
   caller's whole transaction: every probe runs under its own savepoint.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import storage_cleanup, storage_watch

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


# ── 1. no forecast from minutes of readings ─────────────────────────────────────────────────

def test_minutes_of_readings_make_no_forecast_and_one_bad_reading_opens_nothing():
    th = dict(storage_watch.storage_rules.DEFAULT_THRESHOLDS)
    # two healthy readings and one bad one, seconds apart: 27 % free, then 13 %
    pts = [(T0 - timedelta(seconds=20), 365.0), (T0 - timedelta(seconds=10), 365.0), (T0, 435.0)]
    fc = storage_watch.forecast(pts, free_gb=65.0, now=T0)
    assert fc.days_until_full is None and fc.growth_gb_per_day == 0.0 and not fc.enough_history
    assert fc.as_dict()["enough_history"] is False and fc.as_dict()["span_hours"] < 0.01
    healthy = {"total_gb": 500.0, "free_gb": 135.0}
    bad = {"total_gb": 500.0, "free_gb": 65.0}
    assert storage_watch.raw_level(healthy, fc.days_until_full, th) == "ok"
    assert storage_watch.next_level("ok", [(healthy, fc.days_until_full), (bad, fc.days_until_full)], th) == "ok", \
        "one bad reading after a healthy one is a blip"


def test_a_day_of_readings_is_enough_for_a_forecast():
    pts = [(T0 - timedelta(hours=24 - h), 300.0 + 2.0 * h) for h in range(25)]   # 48 GB/day
    fc = storage_watch.forecast(pts, free_gb=96.0, now=T0)
    assert fc.enough_history and round(fc.growth_gb_per_day) == 48
    assert round(fc.days_until_full, 1) == 2.0
    short = storage_watch.forecast(pts[-12:], free_gb=96.0, now=T0)
    assert short.days_until_full is None, "half a day is not enough"


# ── 2. a scratch database's birthday is in its name ─────────────────────────────────────────

@pytest.fixture()
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    eng = create_engine(PG_URL, future=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def scratch_dbs(pg):
    tag = uuid.uuid4().hex[:6]
    old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%dt%H%M%S")
    new = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    names = {"expired": f"boss_scratch_st14e{tag}_{old}", "fresh": f"boss_scratch_st14f{tag}_{new}",
             "unstamped": f"boss_scratch_st14u{tag}"}
    auto = create_engine(PG_URL, future=True, isolation_level="AUTOCOMMIT")
    with auto.connect() as c:
        for n in names.values():
            c.execute(text(f'CREATE DATABASE "{n}"'))
    yield tag, names
    with auto.connect() as c:
        for n in names.values():
            c.execute(text(f'DROP DATABASE IF EXISTS "{n}"'))
    auto.dispose()


def test_scratch_dbs_age_by_their_stamp_and_report_the_bytes_of_what_went(pg, scratch_dbs):
    tag, names = scratch_dbs
    with pg.begin() as c:
        items, size = storage_cleanup._drop_scratch_dbs(pg, c, 24, dry_run=True, expected=None)  # noqa: SLF001
        mine = [i for i in items if tag in i]
        assert mine == [names["expired"]], "only the stamped one past its ttl; fresh and unstamped stay"
        own_size = int(c.execute(text("SELECT pg_database_size(:n)"), {"n": names["expired"]}).scalar())
        # the dry run's bytes are exactly its items' bytes (other expired DBs on the server count too)
        sizes = {i: int(c.execute(text("SELECT pg_database_size(:n)"), {"n": i}).scalar()) for i in items}
        assert size == sum(sizes.values())
    with pg.begin() as c:
        done, freed = storage_cleanup._drop_scratch_dbs(pg, c, 24, dry_run=False,  # noqa: SLF001
                                                        expected=[names["expired"]])
    assert done == [names["expired"]] and freed == own_size, "bytes of what was dropped, not of every candidate"
    with pg.connect() as c:
        present = set(c.execute(text("SELECT datname FROM pg_database")).scalars().all())
    assert names["expired"] not in present and names["fresh"] in present and names["unstamped"] in present


# ── 3. a probe a role may not run never aborts the caller's transaction ─────────────────────

def test_optional_postgres_probes_never_abort_the_callers_transaction(pg):
    with pg.connect() as c:
        if not c.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar():
            pytest.skip("needs a role that may SET ROLE to a built-in one")
    with pg.begin() as c:
        # a built-in role with no pg_ls_tmpdir and no right on BOSS's tables
        c.execute(text("SET LOCAL ROLE pg_read_all_settings"))
        out = storage_watch.collect_api_sample(c)
        assert "pg_temp_gb" not in out and "backups_n" not in out, "refused probes are NULL readings"
        items, _ = storage_cleanup._remove_pg_temp(c, 10, dry_run=True, expected=None)  # noqa: SLF001
        assert isinstance(items, list)
        assert c.execute(text("SELECT 1")).scalar() == 1, "the transaction is still usable"
        c.execute(text("RESET ROLE"))
