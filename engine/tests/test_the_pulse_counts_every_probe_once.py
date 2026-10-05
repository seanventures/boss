"""The Pulse counts every probe once (infra-observability spec §9.1).

720 samples with known ok/latency over the last 24 h, with a two-hour hole. Every cell's
n, ok and p95 must equal a direct SQL recount of that cell's time range — counted here with
plain range predicates, not the view's bucket expression — and an empty cell is n == 0, never
green. The same holds when the narrow layouts ask for 48 or 24 cells.
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project, sample, target
from boss_engine.config import get_settings
from boss_engine.services import obs_view


@pytest.fixture(scope="module")
def seeded():
    eng = pg_engine()
    rnd = random.Random(7)
    w = obs_view.window_bounds("24h")
    with eng.begin() as c:
        pid = project(c)
        target(c, pid, f"{pid}_t_live", "invoice")
        target(c, pid, f"{pid}_t_new", "billing", deployed=False)
        now = c.execute(text("SELECT now()")).scalar()
        hole_from, hole_to = now - timedelta(hours=10), now - timedelta(hours=8)
        span = (now - w["start"]).total_seconds() - 1
        placed = 0
        while placed < 720:
            at = w["start"] + timedelta(seconds=rnd.uniform(0, span))
            if hole_from <= at < hole_to:
                continue
            ok = rnd.random() > 0.15
            sample(c, pid, f"{pid}_t_live", at, ok,
                   rnd.randint(40, 2400) if (ok or rnd.random() < 0.5) else None)
            placed += 1
    return eng, pid


def _recount(c, tid, a, z):
    return c.execute(text(
        "SELECT count(*) AS n, count(*) FILTER (WHERE ok) AS ok, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
        "    FILTER (WHERE ok AND latency_ms IS NOT NULL) AS p95 "
        "FROM deployment_health_samples WHERE deployment_target_id = :t "
        "AND checked_at >= :a AND checked_at < :z"), {"t": tid, "a": a, "z": z}).mappings().one()


@pytest.mark.parametrize("cells", [None, 48, 24])
def test_every_cell_equals_a_direct_recount(seeded, cells):
    eng, pid = seeded
    tid = f"{pid}_t_live"
    with eng.begin() as c:
        body = obs_view.pulse(c, get_settings(), pid, win="24h", cells=cells)
        row = next(r for r in body["rows"] if r["target_id"] == tid)
        win = body["window"]
        assert win["cells"] == (cells or 96)
        assert len(row["strip"]) == win["cells"]
        start = obs_view.window_bounds("24h", cells)["start"]
        empty = 0
        for i, (ok, n, p95) in enumerate(row["strip"]):
            a = start + timedelta(seconds=i * win["cell_s"])
            got = _recount(c, tid, a, a + timedelta(seconds=win["cell_s"]))
            assert (n, ok) == (got["n"], got["ok"]), f"cell {i}"
            assert p95 == (None if got["p95"] is None else int(round(got["p95"]))), f"cell {i}"
            if n == 0:
                empty += 1
                assert ok == 0 and p95 is None
        assert sum(c_[1] for c_ in row["strip"]) == 720
        assert empty >= 2 * 3600 // win["cell_s"] - 1, "the two-hour hole is a gap, not green"
        whole = _recount(c, tid, start, start + timedelta(hours=24))
        assert row["uptime"] == round(whole["ok"] / whole["n"], 4)
        assert row["p95_ms"] == int(round(whole["p95"]))


def test_a_target_never_deployed_is_last_and_says_so(seeded):
    eng, pid = seeded
    with eng.begin() as c:
        body = obs_view.pulse(c, get_settings(), pid)
    assert body["rows"][-1]["state"] == "not_deployed"
    assert body["rows"][-1]["word"] == "Not deployed"
    assert body["counts"]["not_deployed"] == 1
    assert body["etag"].startswith('"obs-')


def test_the_state_is_health_views_state(seeded):
    """Batched, but the same derivation: deployment_health.health_view agrees on state/since."""
    from boss_engine.services.deployment_health import health_view

    eng, pid = seeded
    with eng.begin() as c:
        body = obs_view.pulse(c, get_settings(), pid)
        old = {t["target_id"]: t for t in health_view(c, pid)["targets"]}
    for r in body["rows"]:
        assert r["state"] == old[r["target_id"]]["status"]
        assert r["since"] == old[r["target_id"]]["status_since"]
