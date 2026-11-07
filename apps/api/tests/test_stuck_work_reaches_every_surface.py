"""What a stuck build waits for, what the Shepherd found, and its live log reach the API's surfaces.

The laptop, 2026-11-07: four CVE builds were "Queued 0 of 7" on the build page, "Running" on
Project › Work, and every attempt crashed in the step endpoint with a bare 500 nobody could read.
Here, through the real endpoints on Postgres:
  * the step endpoint records a crash ON THE RUN (what, where, how many times) and answers in words;
  * GET …/cockpit carries `why` and `diagnosis`, and its header says Stuck with the reason;
  * GET /v1/projects/{p}/work gives the row the same reason, Stalled not Running, and a `log`
    link that opens that run's log (`logs=build`).
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import code_task_service, stuck_work


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("sw-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client):
    slug = f"sw-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Stuck Work", "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _get(world, client, path):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def _crash_looping_build(engine, pid: str) -> str:
    rid = f"crun_sw_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx, created_at, updated_at) VALUES (:id, :p, 'running', 'queued', "
            "'Bump esbuild', '{}'::jsonb, '{}'::jsonb, CAST(:f AS jsonb), now() - interval '2 hours', "
            "now())"), {"id": rid, "p": pid,
                        "f": json.dumps({"redelivered_attempts": list(range(2, 31))})})
        db.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, attempt, max_attempts, "
            "lease_owner, lease_expires_at, heartbeat_at, on_behalf_of, chain, on_behalf_system) "
            "VALUES (:j, :p, 'code.task', 'running', CAST(:pl AS jsonb), 30, 3, 'w-s1', "
            "now() + interval '4 minutes', now(), 'sys_worker', '[]'::jsonb, true)"),
            {"j": f"job_sw_{uuid.uuid4().hex[:10]}", "p": pid, "pl": json.dumps({"run_id": rid})})
    return rid


def test_the_step_endpoint_records_a_crash_and_answers_in_words(engine, project, monkeypatch):
    rid = _crash_looping_build(engine, project)

    def boom(*a, **k):
        raise RuntimeError("Not an executable object")

    monkeypatch.setattr(code_task_service, "run_code_task", boom)
    monkeypatch.setattr(main, "require_worker_principal", lambda: None)
    monkeypatch.setattr(main, "require_project_actor", lambda *a, **k: None)
    from apps.api.app.request_bodies import CodeTaskStepBody
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as got:
        main.run_code_task_step_endpoint(CodeTaskStepBody(run_id=rid))
    assert got.value.status_code == 500
    assert "RuntimeError: Not an executable object" in got.value.detail
    assert "test_stuck_work_reaches_every_surface.py" in got.value.detail  # where it broke
    with engine.begin() as db:
        crash = db.execute(text("SELECT finalize_ctx->'last_crash' FROM code_task_runs WHERE id = :r"),
                           {"r": rid}).scalar()
        events = db.execute(text("SELECT count(*) FROM events WHERE entity_id = :r AND "
                                 "event_type = 'code_task.step_crashed'"), {"r": rid}).scalar()
    assert crash["count"] == 1 and crash["type"] == "RuntimeError"
    assert events == 1


def test_the_build_page_and_the_work_row_say_why_and_link_the_log(world, client, engine, project):
    rid = _crash_looping_build(engine, project)
    with engine.begin() as db:
        stuck_work.diagnose(db, main.settings, rid, use_model=False)
        # The diagnosis stopped the loop; a fresh look at the same build before that, for the page:
        db.execute(text("UPDATE code_task_runs SET status = 'running', phase = 'queued' WHERE id = :r"),
                   {"r": rid})
        db.execute(text("UPDATE jobs SET status = 'running', lease_expires_at = now() + interval "
                        "'4 minutes' WHERE payload->>'run_id' = :r"), {"r": rid})
    ck = _get(world, client, f"/v1/code-tasks/{rid}/cockpit?project_id={project}")
    assert ck["why"]["code"] == "crash_loop" and ck["why"]["who"] == "stuck"
    assert ck["state"]["word"] == "Stuck" and "every attempt crashed" in ck["state"]["tip"]
    assert [s["k"] for s in ck["diagnosis"]["steps"]] == ["stuck", "checked", "cause", "tried", "next"]

    work = _get(world, client, f"/v1/projects/{project}/work")
    row = next(w for w in work["items"] if w.get("build_id") == rid)
    assert row["state"] == "stalled"
    assert row["why"]["words"].startswith("Stuck: every attempt crashed")
    assert row["diagnosis"]["steps"][2]["k"] == "cause"
    q = parse_qs(row["links"]["log"].lstrip("?"))
    assert q["build"] == [rid] and q["logs"] == ["build"] and q["area"] == ["code_tasks"]


def test_the_stalled_work_board_is_served(world, client, engine, project):
    rid = _crash_looping_build(engine, project)
    with engine.begin() as db:
        stuck_work.diagnose(db, main.settings, rid, use_model=False)
    b = _get(world, client, f"/v1/ambient/stalled-work?project_id={project}")
    assert b["owner"] == "Shepherd" and b["max_fixes"] == stuck_work.MAX_FIXES_PER_ITEM
    entry = next(e for e in b["needs_person"] if e["run_id"] == rid)
    assert entry["why_not_fixed"] and entry["next"] and "logs=build" in entry["links"]["log"]
