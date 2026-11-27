"""A 4-core / 8 GiB host under pressure (R3, BOSSAPP-1431; RCA gap 6).

An enterprise partner's small laptop thrashed or starved: one job slot for everything, pinned
builds admitted at twice the measured room, and the first build admitted under high memory
pressure. Our hosts derive two to four slots, so none of that was ever exercised. These run the
admission rules on the small host's numbers (tests/_small_host.py):

- builds at once follow the box (cores / 2), a pin above it is capped with a warning;
- job slots are their own setting, never "one, because one build fits";
- heavy builds back off under memory pressure, light work never does;
- lowering capacity drains: running builds keep running and renewing their leases, only new
  heavy work waits.
"""
from __future__ import annotations

import os
import uuid

import pytest

from boss_engine.services import admission
from boss_engine.services.workload_profiles import DEFAULTS

from ._small_host import SMALL_HOST, SMALL_HOST_PRESSURED

CODE = DEFAULTS["code.task"]


class _Conn:
    """Just enough connection for builds_at_once/snapshot with `_state` faked."""

    def execute(self, *_a, **_k):
        return type("R", (), {"scalar": lambda self: 0})()


def test_the_small_host_derives_builds_from_its_cores_and_caps_a_pin(monkeypatch):
    assert admission.derived_slots(SMALL_HOST, CODE.request_mb) == 2         # 4 cores / 2
    # Three running previews take room from builds, never below one.
    previews = 3 * DEFAULTS["preview_node"].request_mb
    assert 1 <= admission.derived_slots(SMALL_HOST, CODE.request_mb, previews_mb=previews) <= 2
    # A pin of 4 on this box is capped at what it measures, with the warning the console shows
    # (it used to admit at twice the derived room).
    monkeypatch.setattr(admission, "_state", lambda _c, key: {"value": 4} if key ==
                        "builds_at_once" else {})
    monkeypatch.setattr(admission, "running_previews_mb", lambda *_: 0.0)
    bao = admission.builds_at_once(_Conn(), SMALL_HOST, CODE.request_mb)
    assert bao["value"] == 2 and bao["pinned"] == 4 and not bao["auto"]
    assert "this machine measures room for 2" in bao["warning"]


def test_job_slots_are_their_own_setting_not_one_because_one_build_fits(monkeypatch):
    """Job slots (light work, approvals, reviews) are not the build limit: the operator's saved
    slots win even when the box fits one build."""
    state: dict = {"builds_at_once": {"value": 1}}
    monkeypatch.setattr(admission, "_state", lambda _c, key: state.get(key, {}))
    monkeypatch.setattr(admission, "running_previews_mb", lambda *_: 0.0)
    monkeypatch.setattr(admission, "heavy_in_flight", lambda *_: 1)
    monkeypatch.setattr(admission, "current_level", lambda *_a, **_k: admission.L0)
    snap = admission.snapshot(_Conn(), cap=SMALL_HOST)
    assert snap["builds_at_once"]["value"] == 1
    # Unsaved, job slots are measured (derived_worker_slots): builds give their heavy slot
    # back while they think (heavy_slot), so one heavy build still leaves room for three more
    # thinking builds on this 8 GB box — the heavy work stays at one.
    assert snap["worker_slots"] == 4 and snap["worker_slots_auto"]
    assert "thinking" in snap["worker_slots_why"]
    state["agent_capacity"] = {"worker_slots": 3}
    snap = admission.snapshot(_Conn(), cap=SMALL_HOST)
    assert snap["worker_slots"] == 3 and snap["builds_at_once"]["value"] == 1
    state["agent_capacity"] = {"worker_slots": 12}       # clamped, never unbounded
    assert admission.snapshot(_Conn(), cap=SMALL_HOST)["worker_slots"] == 4


def test_heavy_builds_back_off_under_memory_pressure_and_say_why():
    level = admission.level_of(SMALL_HOST_PRESSURED)
    assert level == admission.L2
    # One build already running: the second waits, in the operator's words.
    second = admission.decide(SMALL_HOST_PRESSURED, CODE, in_flight=1, slots=2, level=level)
    assert not second.admitted and second.code == "backpressure_reject"
    assert "needs 1.2 GB" in second.shortfall
    assert "waited on memory 9% of the last minute" in second.shortfall
    assert "CPU load 1.8 per core" in second.shortfall
    # Nothing running, but the doctor's critical rung: even the "one may run" build waits ...
    assert not admission.decide(SMALL_HOST_PRESSURED, CODE, in_flight=0, slots=2,
                                level=level).admitted
    # ... until a person chooses "build anyway" (and never at sustained critical).
    assert admission.decide(SMALL_HOST_PRESSURED, CODE, in_flight=0, slots=2, level=level,
                            force_one=True).admitted
    assert not admission.decide(SMALL_HOST_PRESSURED, CODE, in_flight=0, slots=2,
                                level=admission.L3, force_one=True).admitted
    # At rest the same box admits up to its slots and no further.
    assert admission.decide(SMALL_HOST, CODE, in_flight=1, slots=2).admitted
    full = admission.decide(SMALL_HOST, CODE, in_flight=2, slots=2)
    assert not full.admitted and full.shortfall == "2 of 2 builds at once already running"


def test_light_work_is_never_held_by_the_small_host():
    for kind in ("meeting.step", "approval.notify", "workflow.step"):
        assert admission.profile_for_job(kind) is None


# ------------------------------------------------------------------------------------------ #
# the lease, against Postgres
# ------------------------------------------------------------------------------------------ #

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
PROJECT = "p_small_host"


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine, text

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_sh', 'sh') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_sh', 'org_sh', 'sh') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_sh', 'sh', 'p-small-host') ON CONFLICT DO NOTHING"), {"p": PROJECT})
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM system_state WHERE key IN ('degradation', 'builds_at_once')"))
    eng.dispose()


@pytest.fixture()
def clean(pg):
    from sqlalchemy import text

    with pg.begin() as c:
        # Only this module's jobs may count as in flight.
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM system_state WHERE key IN ('degradation', 'builds_at_once')"))
    yield pg


def _job(c, kind: str, *, status: str = "queued", token: str = "") -> str:
    from sqlalchemy import text

    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    token_hash = JobLeaseService(Settings())._hash_token(token) if token else None
    job_id = f"job_sh_{uuid.uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, payload, status, lease_owner, lease_token_hash, "
        "lease_expires_at, priority) VALUES (:id, :p, :k, '{}'::jsonb, :s, :o, :t, "
        "CASE WHEN :s = 'queued' THEN NULL ELSE now() + interval '5 minutes' END, 100)"),
        {"id": job_id, "p": PROJECT, "k": kind, "s": status,
         "o": "w_sh" if status != "queued" else None,
         "t": token_hash})
    return job_id


def _others_in_flight(c) -> int:
    from sqlalchemy import text

    return int(c.execute(text(
        "SELECT count(*) FROM jobs WHERE kind = ANY(:k) AND status IN ('leased', 'running') "
        "AND project_id <> :p AND (lease_expires_at IS NULL OR lease_expires_at > now())"),
        {"k": sorted(admission.HEAVY_JOB_KINDS), "p": PROJECT}).scalar() or 0)


def test_on_the_pressured_box_the_build_waits_and_light_work_is_leased(clean, monkeypatch):
    from sqlalchemy import text

    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    monkeypatch.setattr(admission, "sample", lambda: SMALL_HOST_PRESSURED)
    with clean.begin() as c:
        if _others_in_flight(c):
            pytest.skip("another suite holds a heavy lease on this database")
        _job(c, "code.task", status="running", token="t_running")
        build = _job(c, "code.task")
        light = _job(c, "meeting.step")
    with clean.begin() as c:
        leased = JobLeaseService(Settings()).claim_job(c, "w_sh2", ["code.task", "meeting.step"],
                                                       60)
    assert leased is not None and leased["id"] == light
    with clean.begin() as c:
        row = c.execute(text("SELECT status, last_error FROM jobs WHERE id = :id"),
                        {"id": build}).mappings().first()
    assert row["status"] == "queued"
    assert row["last_error"]["deferred_reason"] == "host_capacity"
    assert "waited on memory" in row["last_error"]["message"]


def test_lowering_capacity_drains_never_kills(clean, monkeypatch):
    """The operator lowers "Builds at once" to 1 (or the box comes under pressure) while two
    builds run: both keep their leases and renew them; only the next build waits."""
    from sqlalchemy import text

    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    with clean.begin() as c:
        if _others_in_flight(c):
            pytest.skip("another suite holds a heavy lease on this database")
        running = [_job(c, "code.task", status="running", token=f"t_{i}") for i in range(2)]
        waiting = _job(c, "code.task")
        admission.set_state(c, "builds_at_once", {"value": 1})
    monkeypatch.setattr(admission, "sample", lambda: SMALL_HOST_PRESSURED)
    svc = JobLeaseService(Settings())
    with clean.begin() as c:
        assert svc.claim_job(c, "w_sh3", ["code.task"], 60) is None
    with clean.begin() as c:
        for i, job_id in enumerate(running):
            beat = svc.heartbeat(c, "w_sh", job_id, f"t_{i}", 300)
            assert beat["status"] == "running"
    with clean.begin() as c:
        rows = {r["id"]: r for r in c.execute(text(
            "SELECT id, status, last_error FROM jobs WHERE project_id = :p"),
            {"p": PROJECT}).mappings()}
    assert all(rows[j]["status"] == "running" for j in running)
    assert rows[waiting]["status"] == "queued"
    assert rows[waiting]["last_error"]["deferred_reason"] == "host_capacity"
