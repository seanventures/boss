"""DISK PRESSURE FOR A TEST (services/qa_disk_pressure.py, `boss qa disk-pressure`, ENT-REC-29..31).

The thresholds move just above today's free space so the watch reads the level the case needs —
with nothing written to disk — and they come back: by `off`, by the expiry the storage watch
enforces, and never over a person's own edit. Only ever raised, the levels kept in order, refused
on a production install. On the bench database, on a host of the test's own (its own threshold
row), so the install-wide rule is never touched."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import production, qa_disk_pressure as qdp, storage_rules, storage_watch

PG_URL = (os.environ.get("BOSS_TEST_DATABASE_URL") or "").replace("postgresql://",
                                                                   "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def host(pg, monkeypatch):
    """A host reading 41 % free, with its own threshold row (the defaults), removed after."""
    monkeypatch.setattr(production, "resolve_install",
                        lambda: production._dev("dev", "install_tier", "BOSS_INSTALL_TIER=dev"))
    hid = f"qa-host-{uuid.uuid4().hex[:8]}"
    with pg.begin() as c:
        storage_rules.upsert(c, kind="threshold", rule=dict(storage_rules.DEFAULT_THRESHOLDS),
                             host_id=hid, source="default", actor="boss")
        storage_watch.record_sample(c, storage_watch.clean_sample(
            {"host_id": hid, "total_gb": 100.0, "free_gb": 41.0}, source="steward"))
    yield hid
    with pg.begin() as c:
        c.execute(text("DELETE FROM retention_rules WHERE host_id = :h"), {"h": hid})
        c.execute(text("DELETE FROM storage_samples WHERE host_id = :h"), {"h": hid})


def _th(pg, hid):
    with pg.begin() as c:
        return storage_rules.thresholds(c, hid)


def _row(pg, hid):
    with pg.begin() as c:
        return dict(c.execute(text("SELECT source, rule FROM retention_rules WHERE host_id = :h "
                                   "AND kind = 'threshold'"), {"h": hid}).mappings().one())


def test_critical_raises_warn_and_critical_just_above_free_space_and_marks_it(pg, host):
    with pg.begin() as c:
        out = qdp.apply(c, level="critical", actor="usr_tester", host_id=host)
    th = _th(pg, host)
    assert th["warn_free_pct"] == 47.0 and th["critical_free_pct"] == 45.0
    assert th["floor_free_pct"] == 10.0, "the floor is its own case"
    assert th["warn_exit_free_pct"] > th["warn_free_pct"], "the watch can still leave the level"
    assert out["moved"]["warn_free_pct"] == [20.0, 47.0]
    with pg.begin() as c:
        mark = qdp.state(c, host)
        level = storage_watch.raw_level({"total_gb": 100.0, "free_gb": 41.0}, None,
                                        storage_rules.thresholds(c, host))
    assert mark["level"] == "critical" and mark["by"] == "usr_tester"
    assert datetime.fromisoformat(mark["until"]) > datetime.now(timezone.utc) + timedelta(hours=23)
    assert level == "critical", "the next readings cross it with nothing written to disk"


def test_a_threshold_is_never_lowered(pg, host):
    with pg.begin() as c:
        storage_rules.upsert(c, kind="threshold", rule={"warn_free_pct": 60.0,
                                                        "warn_exit_free_pct": 64.0},
                             host_id=host, actor="usr_tester")
        qdp.apply(c, level="warn", actor="usr_tester", host_id=host)
    assert _th(pg, host)["warn_free_pct"] == 60.0


def test_off_puts_back_only_what_the_test_set_and_a_persons_edit_stands(pg, host):
    with pg.begin() as c:
        qdp.apply(c, level="floor", actor="usr_tester", host_id=host)
        # A person edits one moved key in the console afterwards.
        storage_rules.upsert(c, kind="threshold", rule={"warn_free_pct": 50.0,
                                                        "warn_exit_free_pct": 55.0},
                             host_id=host, actor="usr_person")
    with pg.begin() as c:
        assert qdp.state(c, host)["level"] == "floor", "the save carried the test's mark"
        out = qdp.restore(c, actor="usr_tester", host_id=host)
    assert "warn_free_pct" in out["kept"] and out["restored"]["critical_free_pct"] == 15.0
    th = _th(pg, host)
    assert th["warn_free_pct"] == 50.0, "a person's later edit stands"
    assert th["critical_free_pct"] == 15.0 and th["floor_free_pct"] == 10.0


def test_a_console_save_of_another_threshold_keeps_the_expiry(pg, host, monkeypatch):
    """Apply critical, a person saves warn_days in the console, 100 h pass: restored."""
    with pg.begin() as c:
        qdp.apply(c, level="critical", actor="usr_tester", host_id=host)
        storage_rules.upsert(c, kind="threshold", rule={"warn_days": 8.0}, host_id=host,
                             actor="usr_person")
    later = datetime.now(timezone.utc) + timedelta(hours=100)
    monkeypatch.setattr(qdp, "_now", lambda: later)
    for _ in range(5):
        with pg.begin() as c:
            if qdp.expire(c) is None:
                break
    th = _th(pg, host)
    assert th["warn_free_pct"] == 20.0 and th["critical_free_pct"] == 15.0
    assert th["warn_days"] == 8.0, "the person's own save is kept"
    with pg.begin() as c:
        assert qdp.state(c, host) is None


def test_a_rule_saved_out_of_order_before_the_check_does_not_block_other_saves(pg, host):
    with pg.begin() as c:
        c.execute(text("UPDATE retention_rules SET rule = rule || '{\"critical_free_pct\": 30}' "
                       "WHERE host_id = :h AND kind = 'threshold'"), {"h": host})
        storage_rules.upsert(c, kind="threshold", rule={"warn_days": 9.0}, host_id=host,
                             actor="usr_person")
    assert _th(pg, host)["warn_days"] == 9.0
    with pg.begin() as c, pytest.raises(ValueError, match="fix warn_free_pct"):
        storage_rules.upsert(c, kind="threshold", rule={"warn_free_pct": 25.0}, host_id=host,
                             actor="usr_person")


def test_off_restores_the_originals_and_the_default_source(pg, host):
    with pg.begin() as c:
        qdp.apply(c, level="critical", actor="usr_tester", host_id=host)
        qdp.apply(c, level="floor", actor="usr_tester", host_id=host)   # a second move
        out = qdp.restore(c, actor="usr_tester", host_id=host)
    assert out["restored"]["warn_free_pct"] == 20.0 and out["restored"]["floor_free_pct"] == 10.0
    assert _th(pg, host) == storage_rules.DEFAULT_THRESHOLDS
    row = _row(pg, host)
    assert row["source"] == "default", "never pinned as an operator's value"
    assert qdp.MARK not in row["rule"]


def test_the_move_expires_by_itself(pg, host):
    with pg.begin() as c:
        qdp.apply(c, level="warn", actor="usr_tester", host_id=host, hours=1)
        c.execute(text("UPDATE retention_rules SET rule = jsonb_set(rule, '{_qa,until}', "
                       "to_jsonb(CAST(:u AS text))) WHERE host_id = :h AND kind = 'threshold'"),
                  {"u": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "h": host})
    for _ in range(5):        # other hosts' marks first, if any; ours within a few passes
        with pg.begin() as c:
            if qdp.expire(c) is None:
                break
    assert _th(pg, host)["warn_free_pct"] == 20.0


def test_a_production_install_is_refused(pg, host, monkeypatch):
    monkeypatch.setattr(production, "resolve_install",
                        lambda: production._prod("install_tier", "BOSS_INSTALL_TIER=prod"))
    with pg.begin() as c, pytest.raises(qdp.Refused, match="production"):
        qdp.apply(c, level="warn", actor="usr_tester", host_id=host)
    assert _th(pg, host) == storage_rules.DEFAULT_THRESHOLDS


def test_the_levels_must_stay_in_order():
    storage_rules.check_order({"warn_free_pct": 20, "critical_free_pct": 15, "floor_free_pct": 10})
    for bad in ({"warn_free_pct": 15, "critical_free_pct": 15, "floor_free_pct": 10},
                {"warn_free_pct": 20, "critical_free_pct": 8, "floor_free_pct": 10}):
        with pytest.raises(ValueError, match="warn > critical > floor"):
            storage_rules.check_order(bad)


def test_the_console_cannot_save_thresholds_out_of_order(pg, host):
    with pg.begin() as c, pytest.raises(ValueError, match="warn > critical > floor"):
        storage_rules.upsert(c, kind="threshold", rule={"critical_free_pct": 25.0},
                             host_id=host, actor="usr_person")
