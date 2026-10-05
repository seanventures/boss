"""An operator's build is admitted before the system's own passes, and those passes yield.

Measured locally (one build at a time): Metatron's "QUALITY IMPROVE PASS" took 7 of 13 builds
while the operator's build sat queued with last_error "host_capacity". Two defects together:

* the captain and swarm starters never said they were background work, so their builds were
  queued at the same priority as a person's;
* a build waiting for room was deferred 20s at a time, so the next free slot went to whichever
  deferred job's timer ran out first — not to the most important one.

Everything here runs the real claim path (`JobLeaseService.claim_job`) on the real database. Only
the machine's capacity reading is fixed, so "one build at a time" is the same on every host.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from boss_engine.services import admission
from boss_engine.services.jobs import (
    JOB_PRIORITY_BACKGROUND, JOB_PRIORITY_REQUESTED, now_utc,
)


def _project() -> str:
    return get_settings().default_project_id


@pytest.fixture()
def one_build_at_a_time(monkeypatch):
    roomy = admission.Capacity(total_mb=64_000, available_mb=60_000, cores=8,
                               disk_free_gb=200.0)
    monkeypatch.setattr(admission, "sample", lambda: roomy)
    monkeypatch.setattr(admission, "builds_at_once",
                        lambda *_a, **_k: {"value": 1, "auto": False, "derived": 1,
                                           "pinned": 1, "warning": None})


def _job(engine, name, priority, *, seconds_ago, status="queued"):
    job_id = f"job_{name}_{uuid4().hex[:6]}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO jobs (id, project_id, kind, priority, status, payload, created_at, "
            "run_after, lease_owner, lease_expires_at) VALUES (:id, :p, 'code.task', :pr, :s, "
            "CAST(:pl AS JSONB), :at, :at, :owner, :exp)"
        ), {"id": job_id, "p": _project(), "pr": priority, "s": status,
            "pl": json.dumps({"run_id": f"crun_{name}"}),
            "at": now_utc() - timedelta(seconds=seconds_ago),
            "owner": "worker-busy" if status == "running" else None,
            "exp": now_utc() + timedelta(minutes=5) if status == "running" else None})
    return job_id


def _claim(engine, worker="worker-a"):
    with engine.begin() as connection:
        leased = api_main.jobs.claim_job(connection, worker, ["code.task"], 300)
    return leased["id"] if leased else None


def _status(engine, job_id):
    with engine.begin() as connection:
        return connection.execute(text("SELECT status, last_error FROM jobs WHERE id = :i"),
                                  {"i": job_id}).mappings().one()


def test_when_the_slot_frees_the_operators_build_is_leased_first(engine, one_build_at_a_time):
    busy = _job(engine, "busy", JOB_PRIORITY_REQUESTED, seconds_ago=600, status="running")
    autonomous = _job(engine, "captain", JOB_PRIORITY_BACKGROUND, seconds_ago=120)
    operator = _job(engine, "operator", JOB_PRIORITY_REQUESTED, seconds_ago=60)

    # The slot is taken: nothing is leased, both wait.
    assert _claim(engine) is None
    assert _status(engine, operator)["last_error"]["code"] == "host_capacity"

    # The slot frees. The captain's 20s deferral happens to run out first; the operator's
    # has not — before the fix that alone handed the slot to the captain.
    with engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET status = 'succeeded', lease_owner = NULL, "
                                "lease_expires_at = NULL WHERE id = :i"), {"i": busy})
        connection.execute(text("UPDATE jobs SET run_after = now() - interval '1 second' "
                                "WHERE id = :i"), {"i": autonomous})
    assert _claim(engine) == operator
    # And while the operator's build holds the only slot, the captain still waits.
    assert _claim(engine, "worker-b") is None
    assert _status(engine, autonomous)["status"] == "queued"


def test_autonomous_work_does_not_take_a_free_slot_while_a_person_waits(
        engine, one_build_at_a_time, monkeypatch):
    autonomous = _job(engine, "swarm", JOB_PRIORITY_BACKGROUND, seconds_ago=120)
    operator = _job(engine, "op", JOB_PRIORITY_REQUESTED, seconds_ago=60)
    # The person's build is not leasable by this worker on this poll (say a budget hold left it
    # for the next one); the slot is free. The swarm pass yields rather than take it.
    with engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET run_after = now() + interval '30 seconds' "
                                "WHERE id = :i"), {"i": operator})
    assert _claim(engine) is None
    held = _status(engine, autonomous)
    assert held["status"] == "queued"
    assert held["last_error"]["code"] == "yielded_to_operator"

    # With no person waiting, background work runs as before.
    with engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :i"),
                           {"i": operator})
    assert _claim(engine) == autonomous


def test_captain_and_swarm_builds_are_queued_as_background(engine):
    from boss_engine.services.code_task_service import start_code_task

    def start(fctx):
        with engine.begin() as connection:
            run_id = start_code_task(
                connection, project_id=_project(), task_id=None, title=f"t {uuid4().hex[:6]}",
                instructions=f"do {uuid4().hex}", acceptance_criteria=[], test_commands=[],
                owner="acme", repo="sandbox", base_branch="main", finalize_ctx=fctx,
                jobs=api_main.jobs)
            return connection.execute(text(
                "SELECT priority FROM jobs WHERE kind = 'code.task' "
                "AND payload->>'run_id' = :r"), {"r": run_id}).scalar()

    assert start({"source": "metatron_captain"}) == JOB_PRIORITY_BACKGROUND
    assert start({"source": "agent_process", "process_run_id": "apr_1"}) == \
        JOB_PRIORITY_BACKGROUND
    assert start({"actor": "ceo"}) == JOB_PRIORITY_REQUESTED
