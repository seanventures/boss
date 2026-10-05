"""A stall lands in the cell of the time it happened (infra-observability spec §9.1).

System's STALLS strip counts `host.stalled` events (the machine; recorded on the install's
default project, only those measured over an idle wait — rows without `source` were the old
clock and are ignored) plus this project's shepherd watch observations of a stall class. Each
lands in exactly the 15-minute cell its time falls in; the newest eight are listed with a short
word and what was done ("would requeue (shadow)" / "requeue done").
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import obs_view


def _event(c, pid, at, data):
    c.execute(text(
        "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data, "
        " created_at) VALUES (:id, :p, 'host.stalled', 'boss', 'host', 'self', CAST(:d AS jsonb), "
        " :at)"), {"id": f"ev_{uuid4().hex}", "p": pid, "d": json.dumps(data), "at": at})


def _watch(c, pid, at, cls, name, action, mode, acted):
    c.execute(text(
        "INSERT INTO shepherd_watch_observations (id, project_id, run_id, watch_class, class_name, "
        " action, mode, acted, reason, created_at) "
        "VALUES (:id, :p, :run, :cls, :name, :act, :mode, :acted, 'test', :at)"),
        {"id": f"sw_{uuid4().hex}", "p": pid, "run": f"run_{uuid4().hex[:6]}", "cls": cls,
         "name": name, "act": action, "mode": mode, "acted": acted, "at": at})


def test_each_stall_is_in_its_own_cell():
    eng = pg_engine()
    w = obs_view.window_bounds("24h", 96)
    with eng.begin() as c:
        pid = project(c)
        home = project(c, str(get_settings().default_project_id))
        now = c.execute(text("SELECT now()")).scalar()
        times = {
            "host": now - timedelta(hours=3, minutes=7),
            "stalled": now - timedelta(hours=11, minutes=2),
            "starved": now - timedelta(minutes=20),
        }
        _event(c, home, times["host"], {"stalled_seconds": 38.4, "source": "worker.idle_wait"})
        _event(c, home, now - timedelta(hours=1), {"stalled_seconds": 99})        # old clock
        _watch(c, pid, times["stalled"], 3, "stalled in one phase", "requeue", "shadow", False)
        _watch(c, pid, times["starved"], 5, "starved", "requeue", "live", True)
        _watch(c, pid, now - timedelta(hours=2), 1, "redundant duplicate", "cancel", "live", True)
        other = project(c)
        _watch(c, other, now - timedelta(hours=2), 3, "stalled in one phase", "none", "live", False)
        got = obs_view.stalls(c, pid, since=w["start"], cells=96)

    assert got["cell_s"] == 900 and len(got["strip"]) == 96
    expected = [0] * 96
    for at in times.values():
        expected[int((at - w["start"]).total_seconds() // 900)] += 1
    assert got["strip"] == expected
    assert got["total"] == 3
    words = [r["word"] for r in got["rows"]]
    assert words == ["Starved", "Host stalled", "Stalled in phase"]
    starved, host, stalled = got["rows"]
    assert host["subject"] == "38s · worker" and host["did"] is None
    assert stalled["did"] == "would requeue (shadow)" and stalled["shadow"] is True
    assert starved["did"] == "requeue done" and starved["shadow"] is False


def test_system_carries_the_strip_and_never_raises():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        body = obs_view.system(c, get_settings(), pid)
    assert len(body["stalls"]["strip"]) == 96
    assert body["state"] in ("ok", "warning", "critical")
    assert isinstance(body["partial"], list)
    assert body["etag"].startswith('"obs-')
