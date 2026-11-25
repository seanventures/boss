"""Previews are kept while there is room (operator, 2026-11-19: "the preview is very important —
retain it if there's enough memory").

The reaper used to stop every preview nobody had opened for 20 minutes, and every preview past its
60-minute limit, whatever the machine had to spare. Now an idle or past-its-time preview is only a
CANDIDATE: it is stopped when the host is short of memory/CPU or more previews run than
BOSS_PREVIEW_MAX_RUNNING, older previews of the same app first, and the row says why ("to free
memory"). A kept preview's time limit is renewed. A dead process is always stopped.
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


@pytest.fixture()
def project(engine):
    pid = f"p_keep_{uuid.uuid4().hex[:8]}"
    yield pid
    with engine.begin() as c:
        c.execute(text("DELETE FROM app_previews WHERE project_id = :p"), {"p": pid})


@pytest.fixture(autouse=True)
def no_kill(monkeypatch):
    from boss_engine.services import preview_service

    monkeypatch.setattr(preview_service, "_kill", lambda pid: None)


def _preview(engine, project, *, repo="acme/recipes", created_ago=60, seen_ago=None,
             expires_in=60, pid=None) -> str:
    pv = f"prev_kp{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO app_previews (id, project_id, repo, mode, pid, status, expires_at, "
            "created_at, last_seen_at) VALUES (:id, :p, :repo, 'process', :pid, 'running', :exp, "
            ":cr, :seen)"),
            {"id": pv, "p": project, "repo": repo, "pid": pid or os.getpid(),
             "exp": now + timedelta(minutes=expires_in), "cr": now - timedelta(minutes=created_ago),
             "seen": (now - timedelta(minutes=seen_ago)) if seen_ago is not None else None})
    return pv


def _row(engine, pv: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text("SELECT status, error, expires_at FROM app_previews "
                                   "WHERE id = :i"), {"i": pv}).mappings().first())


def _reap(engine, **kw):
    from boss_engine.config import Settings
    from boss_engine.services import preview_service

    return preview_service.reap_previews(engine, Settings(), idle_minutes=20, **kw)


def test_with_room_an_idle_and_an_expired_preview_keep_running(engine, project):
    idle = _preview(engine, project, repo="acme/a", seen_ago=45)
    expired = _preview(engine, project, repo="acme/b", expires_in=-1)
    got = _reap(engine, pressure=lambda: False, max_running=50)
    assert _row(engine, idle)["status"] == "running"
    row = _row(engine, expired)
    assert row["status"] == "running"
    assert row["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=30)   # renewed
    assert got["kept"] >= 2


def test_a_dead_process_is_always_stopped(engine, project):
    dead = _preview(engine, project, pid=2 ** 22 - 11)
    _reap(engine, pressure=lambda: False, max_running=50)
    assert _row(engine, dead)["status"] == "stopped"


def test_short_of_memory_older_previews_of_an_app_go_first(engine, project):
    old = _preview(engine, project, created_ago=120, seen_ago=90)
    latest = _preview(engine, project, created_ago=60, seen_ago=45)
    other_app = _preview(engine, project, repo="acme/other", created_ago=60, seen_ago=45)
    answers = iter([True, False])           # freeing the older one was enough
    got = _reap(engine, pressure=lambda: next(answers, False), max_running=50)
    row = _row(engine, old)
    assert row["status"] == "stopped" and "to free memory" in row["error"]
    assert row["error"].startswith("Stopped by BOSS:")     # asleep: wakes on open
    assert _row(engine, latest)["status"] == "running"
    assert _row(engine, other_app)["status"] == "running"
    assert got["memory"] == 1


def test_still_short_of_memory_the_latest_idle_preview_goes_too(engine, project):
    latest = _preview(engine, project, seen_ago=45)
    watched = _preview(engine, project, repo="acme/watched", seen_ago=2)
    _reap(engine, pressure=lambda: True, max_running=50)
    assert _row(engine, latest)["status"] == "stopped"
    assert _row(engine, watched)["status"] == "running"      # in use: never a candidate


def test_over_the_cap_the_least_recently_opened_idle_previews_make_room(engine, project):
    a = _preview(engine, project, repo="acme/a", seen_ago=100)
    b = _preview(engine, project, repo="acme/b", seen_ago=50)
    c_ = _preview(engine, project, repo="acme/c", seen_ago=1)
    from boss_engine.services import preview_service
    with engine.begin() as c:
        running = c.execute(text("SELECT count(*) FROM app_previews WHERE status = 'running' "
                                 "AND mode <> 'fixture'")).scalar()
    _reap(engine, pressure=lambda: False, max_running=running - 1)
    assert _row(engine, a)["status"] == "stopped" and "to make room" in _row(engine, a)["error"]
    assert _row(engine, b)["status"] == "running" and _row(engine, c_)["status"] == "running"
    assert preview_service.preview_max_running() >= 1


def test_the_thresholds_are_settings(monkeypatch):
    from boss_engine.services import preview_service

    monkeypatch.setenv("BOSS_PREVIEW_MAX_RUNNING", "3")
    monkeypatch.setenv("BOSS_PREVIEW_MEMORY_FLOOR_PCT", "35")
    assert preview_service.preview_max_running() == 3
    assert preview_service.preview_memory_floor_pct() == 35.0
    monkeypatch.delenv("BOSS_PREVIEW_MAX_RUNNING")
    assert preview_service.preview_max_running() == preview_service.DEFAULT_PREVIEW_MAX_RUNNING
