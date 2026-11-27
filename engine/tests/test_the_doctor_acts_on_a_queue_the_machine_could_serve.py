"""THE DOCTOR ACTS ON A QUEUE THE MACHINE COULD SERVE (operator, 2026-11-27: "why are our BOSS
agents not acting while the build is queued so long — all agents should be always on").

Enterprise validation: builds sat queued 3.4-3.6 h for one heavy slot while the host showed
0.02-0.05 load per core and 25% of its memory in use, and no always-on loop acted. The system
doctor (always on, every 15 s) now raises heavy slots within a measured ceiling when work waits
and the host has room, records it in "Decided for you" with an undo, drains back when headroom
goes, and — when it cannot help — says why on the waiting build and once to a person.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import admission, autonomy_gate, capacity_steward as cs, stuck_work
from boss_engine.services import system_doctor

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

NOW = datetime(2026, 11, 27, 3, 0, tzinfo=timezone.utc)
IDLE = admission.Capacity(total_mb=8192, available_mb=6100, cores=4, load_per_core=0.04,
                          disk_free_gb=80.0)
BUSY = admission.Capacity(total_mb=8192, available_mb=6100, cores=4, load_per_core=1.6,
                          disk_free_gb=80.0)
SWAPPING = admission.Capacity(total_mb=8192, available_mb=700, cores=4, load_per_core=0.4,
                              disk_free_gb=80.0)


def _plan(cap=IDLE, **kw):
    args = dict(cap=cap, level=admission.level_of(cap), request_mb=1200, base=1, boost=0,
                oldest_wait_s=3 * 3600, queue_empty_s=0, last_change=None, now=NOW)
    args.update(kw)
    return cs.plan(**args)


def test_work_waiting_on_an_idle_machine_gets_one_more_heavy_slot():
    p = _plan()
    assert p.action == "raise" and p.boost == 1
    assert "waited 180 minutes" in p.why and "at most 4" in p.why


def test_it_waits_before_raising_again_and_never_passes_the_ceiling():
    assert _plan(boost=1, last_change=NOW - timedelta(minutes=2)).action == "none"
    assert _plan(boost=1, last_change=NOW - timedelta(minutes=6)).action == "raise"
    top = _plan(base=1, boost=3, last_change=NOW - timedelta(hours=1))
    assert top.action == "busy" and "the most this machine measures" in top.why
    assert cs.ceiling(IDLE, 1200) == 4          # one per core, memory permitting


def test_a_short_wait_is_left_alone():
    assert _plan(oldest_wait_s=4 * 60).action == "none"


def test_a_busy_machine_is_said_with_its_reason_and_an_estimate():
    p = _plan(cap=BUSY, typical_minutes=55)
    assert p.action == "busy" and "CPU load is 1.60 per core" in p.why
    assert p.estimate_minutes == 55


def test_headroom_going_drains_one_slot_and_an_empty_queue_resets():
    p = _plan(cap=SWAPPING, boost=2, last_change=NOW - timedelta(minutes=3))
    assert p.action == "lower" and p.boost == 1 and "nothing running is stopped" in p.why
    reset = _plan(boost=2, oldest_wait_s=0, queue_empty_s=20 * 60,
                  last_change=NOW - timedelta(hours=1))
    assert reset.action == "reset" and reset.boost == 0


def test_an_undo_keeps_the_doctor_quiet():
    assert _plan(quiet_until=NOW + timedelta(hours=3)).action != "raise"


# ------------------------------------------------------------------------------------------ #
# on the database: the tick, "Decided for you", the undo, the notice and the waiting build
# ------------------------------------------------------------------------------------------ #

@pytest.fixture()
def clean(eng):  # noqa: F811
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'succeeded' WHERE status IN ('leased', 'running', "
                       "'queued')"))
        admission.set_state(c, "builds_at_once", {"value": 1})
        admission.set_state(c, cs.STATE_KEY, {})
    yield
    with eng.begin() as c:
        admission.set_state(c, "builds_at_once", {"value": None})
        admission.set_state(c, cs.STATE_KEY, {})
        c.execute(text("UPDATE system_findings SET status = 'resolved' WHERE kind = "
                       "'queue_waits_busy_host' AND status <> 'resolved'"))


def _queued(eng, project, minutes=200):  # noqa: F811
    rid, jid = f"crun_cs_{uuid4().hex[:12]}", f"job_cs_{uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title, spec) "
                       "VALUES (:id, :p, 'running', 'queued', 'License Shelf', '{}'::jsonb)"),
                  {"id": rid, "p": project})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, on_behalf_of, chain, "
            "on_behalf_system, attempt, max_attempts, last_error, created_at) VALUES (:j, :p, "
            "'code.task', 'queued', CAST(:pl AS jsonb), 'sys_legacy_system', '[]'::jsonb, false, "
            "0, 3, CAST(:e AS jsonb), now() - make_interval(mins => :m))"),
            {"j": jid, "p": project, "pl": json.dumps({"run_id": rid}), "m": minutes,
             "e": json.dumps({"code": "host_capacity", "message": "Queued: waiting for room "
                              "on this machine (1 of 1 builds at once already running)."})})
    return rid, jid


def test_the_doctor_raises_records_it_and_a_person_can_undo(eng, project, clean, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admission, "sample", lambda: IDLE)
    _queued(eng, project)
    p = cs.tick(eng, cap=IDLE)
    assert p.action == "raise"
    with eng.begin() as c:
        bao = admission.builds_at_once(c, IDLE, 1200)
        assert bao["value"] == 2 and bao["boost"] == 1 and "system doctor" in bao["why"]
        assert admission.snapshot(c, cap=IDLE)["worker_slots"] >= 2
        decided = autonomy_gate.decided_for_you(c, project)
    mine = [d for d in decided if d["agent"] == "system_doctor"]
    assert mine and mine[0]["question"].startswith("Run 2 heavy builds at once")
    assert mine[0]["undo"] == "restore_capacity" and "waited" in mine[0]["why"]
    with eng.begin() as c:
        got = autonomy_gate.undo(c, None, mine[0]["decision_id"], project_id=project,
                                 actor="usr_param")
        assert "will not add one" in got["result"]
        assert admission.builds_at_once(c, IDLE, 1200)["value"] == 1
    assert cs.tick(eng, cap=IDLE).action != "raise", "the person's undo stands"


def test_a_busy_machine_says_so_on_the_build_and_once_to_a_person(eng, project, clean, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admission, "sample", lambda: BUSY)
    monkeypatch.setattr(stuck_work, "_needs_model", lambda *_a: None)   # a project with a model
    rid, _ = _queued(eng, project)
    sig = system_doctor.Signals(cap=BUSY)
    system_doctor.tick(eng, sig=sig)
    assert sig.capacity["action"] == "busy"
    with eng.begin() as c:
        rows = c.execute(text("SELECT status, title, detail FROM system_findings WHERE kind = "
                              "'queue_waits_busy_host' AND status <> 'resolved'")).mappings().all()
        run = dict(c.execute(text("SELECT * FROM code_task_runs WHERE id = :r"),
                             {"r": rid}).mappings().first())
        said = stuck_work.wait_reason(c, run)
    assert len(rows) == 1 and "CPU load is 1.60 per core" in rows[0]["detail"]
    assert "BOSS is not adding a heavy slot: CPU load" in said["detail"], said
    # The next look keeps the one notice; it does not raise a second.
    system_doctor.tick(eng, sig=system_doctor.Signals(cap=BUSY))
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM system_findings WHERE kind = "
                              "'queue_waits_busy_host' AND status <> 'resolved'")).scalar() == 1


def test_the_findings_doctor_has_the_notice_as_an_offered_choice_only():
    found = system_doctor.diagnose(system_doctor.Signals(
        cap=BUSY, capacity={"action": "busy", "why": "BOSS is not adding a heavy slot: CPU load "
                            "is 1.60 per core", "estimate_minutes": 40, "boost": 0}))
    notice = [f for f in found if f["kind"] == "queue_waits_busy_host"]
    assert notice and notice[0]["auto"] == [] and "about 40 minutes" in notice[0]["detail"]
    assert {o["key"] for o in notice[0]["options"]} <= system_doctor.OFFERED_OPTIONS
