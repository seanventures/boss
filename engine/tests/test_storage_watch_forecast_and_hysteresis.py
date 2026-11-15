"""The storage watch: the forecast's arithmetic and the two-reading hysteresis (LLD §4.1, §9).

`test_forecast_days_until_full`: slope and forecast on synthetic series; a flat or shrinking disk
is never "full in N days". `test_storage_watch_hysteresis`: one bad reading never opens an
incident, two do; leaving needs two readings above the EXIT band, which sits above the entry band.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.services import storage_rules, storage_watch as w

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
TH = dict(storage_rules.DEFAULT_THRESHOLDS)


def _series(days: int, start_used: float, per_day: float, total_free_at_end: float | None = None):
    pts = []
    for i in range(days * 24):
        at = T0 - timedelta(days=days) + timedelta(hours=i)
        pts.append((at, start_used + per_day * i / 24.0))
    return pts


def _reading(free_pct: float, total: float = 500.0, **extra):
    return {"total_gb": total, "free_gb": total * free_pct / 100.0, **extra}


# ── forecast ────────────────────────────────────────────────────────────────────────────────

def test_forecast_days_until_full_is_free_over_growth():
    pts = _series(7, start_used=300.0, per_day=10.0)             # +10 GB a day
    fc = w.forecast(pts, free_gb=50.0, now=T0)
    assert abs(fc.growth_gb_per_day - 10.0) < 0.05
    assert fc.days_until_full is not None and abs(fc.days_until_full - 5.0) < 0.05
    assert fc.full_at and fc.full_at.startswith("2026-11-19")
    assert fc.samples == 7 * 24 and fc.window_days == 7


def test_forecast_never_says_full_when_the_disk_is_flat_or_shrinking():
    flat = w.forecast(_series(7, 300.0, 0.0), free_gb=50.0, now=T0)
    assert flat.days_until_full is None and flat.full_at is None
    shrinking = w.forecast(_series(7, 300.0, -4.0), free_gb=50.0, now=T0)
    assert shrinking.days_until_full is None
    assert shrinking.growth_gb_per_day < 0


def test_forecast_uses_only_the_window_and_a_floor_rate_of_a_tenth_of_a_gb():
    old = _series(30, 100.0, 50.0)                                   # a wild month ago
    recent = [(T0 - timedelta(days=7) + timedelta(hours=i), 300.0 + 0.001 * i) for i in range(7 * 24)]
    fc = w.forecast([p for p in old if p[0] < T0 - timedelta(days=7)] + recent, free_gb=20.0, now=T0)
    assert fc.samples == 7 * 24
    assert fc.growth_gb_per_day < 0.1
    # The floor rate: a crawl still says a (long) finite time, never a division by ~0.
    assert fc.days_until_full is not None and abs(fc.days_until_full - 200.0) < 0.01


def test_forecast_with_rules_line_adds_what_the_rules_would_reclaim():
    fc = w.forecast(_series(7, 300.0, 10.0), free_gb=50.0, now=T0, rules_reclaim_gb=100.0)
    assert fc.with_rules_days_until_full is not None
    assert abs(fc.with_rules_days_until_full - 15.0) < 0.05
    assert w.forecast([], free_gb=None, now=T0).days_until_full is None


def test_slope_needs_two_points():
    assert w.slope_gb_per_day([]) == 0.0
    assert w.slope_gb_per_day([(T0, 1.0)]) == 0.0


# ── levels and hysteresis ───────────────────────────────────────────────────────────────────

def test_raw_level_bands_match_the_proposed_defaults():
    assert w.raw_level(_reading(30), None, TH) == "ok"
    assert w.raw_level(_reading(19), None, TH) == "warn"
    assert w.raw_level(_reading(30), 6.0, TH) == "warn"           # full in < 7 days
    assert w.raw_level(_reading(14), None, TH) == "critical"
    assert w.raw_level(_reading(30), 2.0, TH) == "critical"       # full in < 3 days
    assert w.raw_level(_reading(30, pg_temp_gb=6.0), None, TH) == "critical"
    # Docker.raw unreclaimed counts only when free is already under the warn line.
    assert w.raw_level(_reading(19, docker_alloc_gb=120, docker_used_gb=60), None, TH) == "critical"
    assert w.raw_level(_reading(40, docker_alloc_gb=120, docker_used_gb=60), None, TH) == "ok"
    assert w.at_floor(_reading(9), TH) and w.at_floor({"total_gb": 500, "free_gb": 4.0}, TH)
    assert not w.at_floor(_reading(12), TH)


def test_storage_watch_hysteresis_one_bad_reading_never_opens_two_do():
    ok, bad = _reading(30), _reading(12)
    # ok → one critical reading: still ok.
    assert w.next_level("ok", [(ok, None), (bad, None)], TH) == "ok"
    # two in a row: critical.
    assert w.next_level("ok", [(bad, None), (bad, None)], TH) == "critical"
    # a warn then a critical reading from ok: both reach warn, so warn (not critical yet).
    assert w.next_level("ok", [(_reading(18), None), (bad, None)], TH) == "warn"
    # escalation from warn needs two critical readings too.
    assert w.next_level("warn", [(_reading(18), None), (bad, None)], TH) == "warn"
    assert w.next_level("warn", [(bad, None), (bad, None)], TH) == "critical"


def test_storage_watch_hysteresis_exit_needs_two_readings_above_the_exit_band():
    # critical exits at the TARGET (25 %), not at the critical line (15 %).
    assert w.next_level("critical", [(_reading(20), None), (_reading(22), None)], TH) == "critical"
    assert w.next_level("critical", [(_reading(26), None), (_reading(20), None)], TH) == "critical"
    assert w.next_level("critical", [(_reading(26), None), (_reading(27), None)], TH) == "ok"
    # pg temp must have drained too.
    assert w.next_level("critical", [(_reading(30, pg_temp_gb=2.0), None),
                                     (_reading(30, pg_temp_gb=2.0), None)], TH) == "critical"
    # warn exits at 24 % AND ≥ 10 days, both readings.
    assert w.next_level("warn", [(_reading(21), None), (_reading(23), None)], TH) == "warn"
    assert w.next_level("warn", [(_reading(25), 8.0), (_reading(25), 8.0)], TH) == "warn"
    assert w.next_level("warn", [(_reading(25), 12.0), (_reading(25), None)], TH) == "ok"
    # with one reading there is never a change.
    assert w.next_level("warn", [(_reading(40), None)], TH) == "warn"


def test_clean_sample_fills_used_pct_and_keeps_only_known_numbers():
    s = w.clean_sample({"host_id": "h1", "total_gb": "500", "free_gb": 100, "images_gb": "x",
                        "by_type": {"images": 29.1, "junk": "no"}, "at": "2026-11-14T12:00:00Z"},
                       source="steward")
    assert s["host_id"] == "h1" and s["source"] == "steward"
    assert s["used_pct"] == 80.0 and s["images_gb"] is None
    assert s["by_type"] == {"images": 29.1}
    assert s["at"].tzinfo is not None


def test_by_type_rows_sum_to_the_disk_and_mark_what_is_rebuildable():
    rows = w.by_type_rows({"total_gb": 500, "free_gb": 100, "backups_gb": 170, "images_gb": 30,
                           "cache_gb": 8, "docker_alloc_gb": 128, "docker_used_gb": 60})
    by = {r["key"]: r for r in rows}
    assert by["free"]["gb"] == 100 and by["docker_vm_unreclaimed"]["gb"] == 68
    assert by["build_cache"]["rebuildable"] and not by["backups"]["rebuildable"]
    assert abs(sum(r["gb"] for r in rows) - 500) < 0.01
