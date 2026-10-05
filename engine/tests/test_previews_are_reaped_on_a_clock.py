"""Previews end on a clock, not when somebody happens to list them (stability spec §4a.6).

Expiry ran only in `list_previews` and at API boot, so a 60-minute preview nobody listed ran
for eight hours (11 such containers on one laptop). The reaper's pass ends a preview that is
past its TTL, whose process is gone, or that nobody has opened for the idle limit — and a
proxied request keeps a preview alive.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL.startswith("postgres"),
                                reason="needs BOSS_TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def engine():
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _preview(engine, *, pid=None, expires_in=60, seen_ago=None, created_ago=0) -> str:
    pv = f"prev_ck{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO app_previews (id, project_id, mode, pid, status, expires_at, created_at, "
            "last_seen_at) VALUES (:id, 'p_clock', 'process', :pid, 'running', :exp, :cr, :seen)"),
            {"id": pv, "pid": pid, "exp": now + timedelta(minutes=expires_in),
             "cr": now - timedelta(minutes=created_ago),
             "seen": (now - timedelta(minutes=seen_ago)) if seen_ago is not None else None})
    return pv


def _status(engine, pv: str) -> str:
    with engine.begin() as c:
        return c.execute(text("SELECT status FROM app_previews WHERE id=:id"), {"id": pv}).scalar()


def test_expired_dead_and_idle_previews_end_and_a_watched_one_stays(engine):
    from boss_engine.config import Settings
    from boss_engine.services import preview_service

    me = os.getpid()                                   # a live pid stands in for the app
    expired = _preview(engine, pid=me, expires_in=-1)
    dead = _preview(engine, pid=2 ** 22 - 7)
    idle = _preview(engine, pid=me, created_ago=45, seen_ago=30)
    watched = _preview(engine, pid=me, created_ago=45, seen_ago=2)
    fresh = _preview(engine, pid=me, created_ago=1)
    # _teardown of a process preview kills its group: never let it near this test process.
    killed = []
    orig = preview_service._kill
    preview_service._kill = lambda pid: killed.append(pid)
    try:
        got = preview_service.reap_previews(engine, Settings(), idle_minutes=20)
    finally:
        preview_service._kill = orig
    assert _status(engine, expired) == "expired"
    assert _status(engine, dead) == "stopped"
    assert _status(engine, idle) == "stopped"
    assert _status(engine, watched) == "running"
    assert _status(engine, fresh) == "running"
    assert got["expired"] >= 1 and got["dead"] >= 1 and got["idle"] >= 1


def test_a_proxied_request_marks_the_preview_seen(engine):
    from boss_engine.services import preview_service

    pv = _preview(engine, pid=os.getpid(), created_ago=45, seen_ago=30)
    preview_service._SEEN_WRITTEN.pop(pv, None)
    preview_service._touch_seen(engine, pv)
    with engine.begin() as c:
        seen = c.execute(text("SELECT last_seen_at FROM app_previews WHERE id=:id"),
                         {"id": pv}).scalar()
    assert datetime.now(timezone.utc) - seen < timedelta(minutes=1)
