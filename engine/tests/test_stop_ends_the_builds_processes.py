"""Stop actually stops (stability spec §4a.3): Stop pressed → zero processes of that build within
10 s. Cancel used to be a row the executor read at its next stage boundary; nothing was
signalled, so an agent ran on for its whole turn after Stop."""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_engine.services import owned_process, stop_watch  # noqa: E402

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine, text

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_sw', 'sw') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_sw', 'org_sw', 'sw') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('p_sw', "
                       "'ws_sw', 'sw', 'p-sw') ON CONFLICT DO NOTHING"))
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM code_task_stop_requests WHERE project_id = 'p_sw'"))
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = 'p_sw'"))
    eng.dispose()


def _run(pg, status="running") -> str:
    from sqlalchemy import text
    rid = f"crun_sw{uuid.uuid4().hex[:8]}"
    with pg.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) VALUES "
                       "(:i, 'p_sw', 'sw', :s)"), {"i": rid, "s": status})
    return rid


def test_a_stop_request_ends_the_build_within_ten_seconds(pg):
    from sqlalchemy import text
    rid = _run(pg)
    keep = _run(pg)
    with owned_process.owner_scope("code_run", rid):
        with owned_process.owner_scope("review_seat", f"{rid}:review[0]"):
            seat = owned_process.popen(["sh", "-c", "sleep 600 & sleep 600"])
        agent = owned_process.popen(["sleep", "600"])
    with owned_process.owner_scope("code_run", keep):
        other = owned_process.popen(["sleep", "600"])
    try:
        assert stop_watch.tick(pg) == []                       # nobody pressed Stop yet
        with pg.begin() as c:
            c.execute(text("INSERT INTO code_task_stop_requests (run_id, project_id, actor) "
                           "VALUES (:i, 'p_sw', 'ceo')"), {"i": rid})
        started = time.monotonic()
        ended = stop_watch.tick(pg)
        assert [e["owner_id"] for e in ended] == [rid]
        assert time.monotonic() - started <= 10
        for proc in (seat, agent):
            assert owned_process.members(proc.pgid, proc.token)["alive"] == []
        assert other.poll() is None, "another build's processes were touched"
        assert owned_process.was_cancelled(rid)
    finally:
        other.kill()


def test_a_run_another_actor_marked_failed_is_ended_too(pg):
    from sqlalchemy import text
    rid = _run(pg)
    with owned_process.owner_scope("code_run", rid):
        proc = owned_process.popen(["sleep", "600"])
    with pg.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'failed' WHERE id = :i"), {"i": rid})
    stop_watch.tick(pg)
    assert owned_process.members(proc.pgid, proc.token)["alive"] == []
