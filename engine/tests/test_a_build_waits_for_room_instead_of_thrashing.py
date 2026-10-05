"""A build waits for room instead of thrashing the machine (stability spec §4b.2-§4b.3).

Before: what started a build was a static number and the budget; nothing asked the machine.
Three builds plus previews drove an 8 GB Docker VM into swap (pswpout 932 MB, memory PSI full
76 s) and the Mac under it to 91% swap. `admit_or_reject` existed and had no production caller.

Fixtures are the machines measured for the spec: boss-1 (16 GB, 8 cores, idle) and the
operator's Docker Desktop VM (7.6 GB) at rest and under the pressure that was observed.
"""
from __future__ import annotations

import inspect
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_engine.services import admission  # noqa: E402
from boss_engine.services.admission import Capacity  # noqa: E402
from boss_engine.services.workload_profiles import DEFAULTS  # noqa: E402

BOSS_1 = Capacity(total_mb=15991, available_mb=13029, cores=8, psi_full_avg10=0.0,
                  psi_full_avg60=0.0, disk_free_gb=16.0, disk_used_pct=80.0,
                  load_per_core=0.05, scope="machine")
MAC_VM = Capacity(total_mb=7650, available_mb=4900, cores=12, psi_full_avg60=0.4,
                  disk_free_gb=281.0, load_per_core=0.24, scope="docker_vm")
MAC_VM_THRASHING = Capacity(total_mb=7650, available_mb=600, cores=12, psi_full_avg10=12.0,
                            psi_full_avg60=6.0, disk_free_gb=281.0, load_per_core=0.9,
                            scope="docker_vm")
SMALL_BOX = Capacity(total_mb=3800, available_mb=2600, cores=2, scope="machine")
CODE = DEFAULTS["code.task"]


def test_an_idle_server_admits_and_derives_four_builds_at_once():
    assert admission.derived_slots(BOSS_1, CODE.request_mb) == 4      # capped by 8 cores / 2
    d = admission.decide(BOSS_1, CODE, in_flight=0, slots=4)
    assert d.admitted and d.code == "ok"


def test_the_docker_vm_has_room_for_a_few_and_a_four_gig_box_for_one():
    assert 2 <= admission.derived_slots(MAC_VM, CODE.request_mb) <= 6
    assert admission.derived_slots(SMALL_BOX, CODE.request_mb) == 1


def test_a_thrashing_vm_queues_the_build_and_says_what_is_short():
    d = admission.decide(MAC_VM_THRASHING, CODE, in_flight=0, slots=2,
                         level=admission.level_of(MAC_VM_THRASHING))
    assert not d.admitted
    assert d.code == "backpressure_reject"
    assert "needs 1.2 GB" in d.shortfall and "free" in d.shortfall
    assert admission.level_of(MAC_VM_THRASHING) == admission.L2


def test_slots_full_queues_rather_than_exceeding():
    d = admission.decide(BOSS_1, CODE, in_flight=4, slots=4)
    assert not d.admitted and d.code == "max_in_flight_overflow"
    assert d.shortfall == "4 of 4 builds at once already running"


def test_a_full_disk_and_a_busy_cpu_also_hold_a_build():
    low_disk = Capacity(**{**BOSS_1.__dict__, "disk_free_gb": 2.0, "notes": []})
    busy = Capacity(**{**BOSS_1.__dict__, "load_per_core": 2.0, "notes": []})
    assert "of disk free" in admission.decide(low_disk, CODE, in_flight=0, slots=4).shortfall
    assert "CPU load" in admission.decide(busy, CODE, in_flight=0, slots=4).shortfall


def test_light_work_is_never_held_back():
    assert admission.profile_for_job("meeting.step") is None
    assert admission.profile_for_job("code.task") == "code.task"


def test_the_overflow_hook_is_the_production_gate():
    src = inspect.getsource(admission.decide)
    assert "admit_or_reject(" in src
    lease = inspect.getsource(__import__("boss_engine.services.jobs", fromlist=["x"])
                              .JobLeaseService.claim_job)
    assert "admission.admit_job(" in lease


# ------------------------------------------------------------------------------------------ #
# the lease itself, against Postgres
# ------------------------------------------------------------------------------------------ #

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
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_adm', 'adm') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_adm', 'org_adm', "
                       "'adm') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "('p_adm', 'ws_adm', 'adm', 'p-adm') ON CONFLICT DO NOTHING"))
        c.execute(text("DELETE FROM system_state WHERE key IN ('degradation', 'builds_at_once')"))
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM events WHERE project_id = 'p_adm'"))
        c.execute(text("DELETE FROM jobs WHERE project_id = 'p_adm'"))
    eng.dispose()


def test_a_lease_on_a_thrashing_machine_leaves_the_job_queued_with_its_reason(pg, monkeypatch):
    from sqlalchemy import text

    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    job_id = f"job_adm_{uuid.uuid4().hex[:8]}"
    with pg.begin() as c:
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload) VALUES "
                       "(:id, 'p_adm', 'code.task', '{}'::jsonb)"), {"id": job_id})
    monkeypatch.setattr(admission, "sample", lambda: MAC_VM_THRASHING)
    with pg.begin() as c:
        leased = JobLeaseService(Settings()).claim_job(c, "w_adm", ["code.task"], 60)
    assert leased is None
    with pg.begin() as c:
        row = c.execute(text("SELECT status, last_error FROM jobs WHERE id = :id"),
                        {"id": job_id}).mappings().first()
        events = c.execute(text("SELECT count(*) FROM events WHERE entity_id = :id AND "
                                "event_type = 'job.capacity_deferred'"), {"id": job_id}).scalar()
    assert row["status"] == "queued"
    assert row["last_error"]["deferred_reason"] == "host_capacity"
    assert "needs 1.2 GB" in row["last_error"]["message"]
    assert events == 1
    with pg.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
        JobLeaseService(Settings()).claim_job(c, "w_adm", ["code.task"], 60)
        events = c.execute(text("SELECT count(*) FROM events WHERE entity_id = :id AND "
                                "event_type = 'job.capacity_deferred'"), {"id": job_id}).scalar()
    assert events == 1, "a second poll must not log the same wait again"


def test_the_profile_moves_toward_what_builds_really_peak_at(pg):
    from boss_engine.services import workload_profiles

    with pg.begin() as c:
        c.execute(__import__("sqlalchemy").text(
            "DELETE FROM workload_profiles WHERE kind = 'code.task'"))
        p = workload_profiles.observe_peak(c, "code.task", 2500)
    assert CODE.request_mb < p.request_mb < 2500
    assert p.limit_mb >= CODE.limit_mb
    with pg.begin() as c:
        c.execute(__import__("sqlalchemy").text(
            "DELETE FROM workload_profiles WHERE kind = 'code.task'"))
