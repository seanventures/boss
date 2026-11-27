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


ROOMY = {"room_since": (NOW - timedelta(minutes=30)).isoformat()}


def _plan(cap=IDLE, **kw):
    args = dict(cap=cap, level=admission.level_of(cap), request_mb=1200, base=2, derived=2,
                boost=0, oldest_wait_s=3 * 3600, queue_empty_s=0, now=NOW, memo=dict(ROOMY))
    args.update(kw)
    return cs.plan(**args)


def test_work_waiting_on_an_idle_machine_gets_one_more_heavy_slot():
    p = _plan()
    assert p.action == "raise" and p.boost == 1
    assert "waited 180 minutes" in p.why and "at most 3" in p.why


def test_the_boost_is_capped_at_the_measured_value_plus_one():
    assert cs.ceiling(IDLE, 1200, derived=2) == 3
    top = _plan(boost=1, memo={**ROOMY, "changed_at": (NOW - timedelta(hours=1)).isoformat()})
    assert top.action == "busy" and "the most it runs" in top.why


def test_it_waits_for_sustained_room_and_between_raises():
    assert _plan(memo={"room_since": (NOW - timedelta(minutes=3)).isoformat()}).action == "none"
    recent = {**ROOMY, "changed_at": (NOW - timedelta(minutes=2)).isoformat()}
    assert _plan(base=1, boost=1, derived=2, memo=recent).action == "none"


def test_a_short_wait_is_left_alone():
    assert _plan(oldest_wait_s=4 * 60).action == "none"


def test_a_busy_machine_is_said_with_its_reason_and_an_estimate():
    p = _plan(cap=BUSY, typical_minutes=55)
    assert p.action == "busy" and "CPU load is 1.60 per core" in p.why
    assert p.estimate_minutes == 55


def test_memory_short_drains_at_once_and_an_empty_queue_resets():
    p = _plan(cap=SWAPPING, boost=1, memo={"changed_at": (NOW - timedelta(minutes=3)).isoformat()})
    assert p.action == "lower" and p.boost == 0 and "nothing running is stopped" in p.why
    assert p.memo["backoff_minutes"] == 30
    reset = _plan(boost=1, oldest_wait_s=0, queue_empty_s=20 * 60,
                  memo={"changed_at": (NOW - timedelta(hours=1)).isoformat()})
    assert reset.action == "reset" and reset.boost == 0


def test_an_undo_keeps_the_doctor_quiet():
    assert _plan(memo={**ROOMY, "quiet_until": (NOW + timedelta(hours=3)).isoformat()}
                 ).action != "raise"


def test_a_persons_pin_is_a_ceiling_said_never_changed():
    """Review PR #367 (ENT-CONC-04): a pin of 1 on a 4-core box used to be raised to 4."""
    memo, boost, now = dict(ROOMY), 0, NOW
    for _ in range(10):
        p = cs.plan(cap=IDLE, level=admission.L0, request_mb=1200, base=1, derived=2, boost=boost,
                    pinned=1, waiting=2, oldest_wait_s=3600, queue_empty_s=0, now=now, memo=memo)
        assert p.action == "pinned"
        boost, memo, now = p.boost, p.memo, now + timedelta(minutes=6)
    assert boost == 0
    assert p.why.startswith("Your limit of 1 is holding 2 builds; the machine has room for 2")


def test_an_unreadable_reading_never_adds_capacity():
    """Review PR #367: unreadable load and disk read as 0.0 and 100 GB and the doctor raised."""
    blind = admission.Capacity(total_mb=8192, available_mb=6000, cores=4,
                               unknown=("CPU load", "disk"))
    p = _plan(cap=blind)
    assert p.action == "busy" and "could not read this machine's CPU load, disk" in p.why


def _simulate(pattern, minutes=120, base=2):
    memo, boost, changes, now = {}, 0, 0, NOW
    for minute in range(minutes):
        cap = IDLE if pattern(minute) else BUSY
        p = cs.plan(cap=cap, level=admission.L0, request_mb=1200, base=base, derived=base,
                    boost=boost, oldest_wait_s=3600, queue_empty_s=0, now=now, memo=memo)
        if p.action in ("raise", "lower", "reset"):
            changes += 1
        boost, memo, now = p.boost, p.memo, now + timedelta(minutes=1)
    return changes


def test_a_bursty_load_does_not_flap():
    """Review PR #367: builds alternating 4 min thinking / 4 min tests made 10+ changes in 2 h."""
    assert _simulate(lambda m: (m // 4) % 2 == 0) <= 2
    # Slower swings (12 min / 12 min): raised and drained, then each raise waits longer.
    assert _simulate(lambda m: (m // 12) % 2 == 0) <= 4


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
    with eng.begin() as c:
        admission.set_state(c, "builds_at_once", {"value": None})     # auto: not a person's pin
        admission.set_state(c, cs.STATE_KEY, {"room_since": "2026-01-01T00:00:00+00:00"})
    p = cs.tick(eng, cap=IDLE)
    assert p.action == "raise", p
    with eng.begin() as c:
        bao = admission.builds_at_once(c, IDLE, 1200)
        assert bao["value"] == 3 and bao["boost"] == 1 and "system doctor" in bao["why"]
        assert admission.snapshot(c, cap=IDLE)["worker_slots"] >= 3
        decided = autonomy_gate.decided_for_you(c, project)
    mine = [d for d in decided if d["agent"] == "system_doctor"]
    assert len(mine) == 1 and mine[0]["question"].startswith("Run 3 heavy builds at once")
    assert mine[0]["undo"] == "restore_capacity" and "waited" in mine[0]["why"]
    with eng.begin() as c:
        got = autonomy_gate.undo(c, None, mine[0]["decision_id"], project_id=project,
                                 actor="usr_param")
        assert "will not add one" in got["result"]
        assert admission.builds_at_once(c, IDLE, 1200)["value"] == 2
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


def test_a_pin_holding_the_queue_on_an_idle_host_is_said_not_changed(eng, project, clean, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admission, "sample", lambda: IDLE)
    monkeypatch.setattr(stuck_work, "_needs_model", lambda *_a: None)
    rid, _ = _queued(eng, project)
    _queued(eng, project)
    sig = system_doctor.Signals(cap=IDLE)
    system_doctor.tick(eng, sig=sig)
    assert sig.capacity["action"] == "pinned" and sig.capacity["boost"] == 0
    with eng.begin() as c:
        assert admission.builds_at_once(c, IDLE, 1200)["value"] == 1      # never above the pin
        notice = c.execute(text("SELECT detail FROM system_findings WHERE kind = "
                                "'queue_held_by_limit' AND status <> 'resolved'")).scalars().all()
        run = dict(c.execute(text("SELECT * FROM code_task_runs WHERE id = :r"),
                             {"r": rid}).mappings().first())
        said = stuck_work.wait_reason(c, run)
        c.execute(text("UPDATE system_findings SET status = 'resolved' WHERE kind = "
                       "'queue_held_by_limit' AND status <> 'resolved'"))
    assert len(notice) == 1 and "Your limit of 1 is holding 2 builds" in notice[0]
    assert "the machine has room for 2" in said["detail"]


def test_one_decided_for_you_row_per_episode(eng, project, clean, monkeypatch):  # noqa: F811
    with eng.begin() as c:
        admission.set_state(c, "builds_at_once", {"value": None})
        admission.set_state(c, cs.STATE_KEY, {"room_since": "2026-01-01T00:00:00+00:00"})
    _queued(eng, project)
    assert cs.tick(eng, cap=IDLE).action == "raise"
    later = datetime.now(timezone.utc) + timedelta(minutes=3)
    assert cs.tick(eng, cap=SWAPPING, now=later).action == "lower"
    with eng.begin() as c:
        mine = [d for d in autonomy_gate.decided_for_you(c, project) if d["agent"] == "system_doctor"]
        steps = c.execute(text("SELECT recommendation->'steps' FROM decisions WHERE id = :d"),
                          {"d": mine[0]["decision_id"]}).scalar()
    assert len(mine) == 1, "the raise and the drain are one row"
    assert [s["action"] for s in steps] == ["raise", "lower"]
    assert mine[0]["undo"] is None, "nothing left to undo once drained"


def test_the_wait_counts_from_the_latest_deferral_not_creation(eng, project, clean):  # noqa: F811
    rid, jid = _queued(eng, project, minutes=300)
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET last_error = last_error || jsonb_build_object("
                       "'deferred_since', (now() - interval '2 minutes')::text) WHERE id = :j"),
                  {"j": jid})
        waits = [w for w in cs.queue_waits(c) if w["id"] == jid]
    assert waits and float(waits[0]["waited"]) < 5 * 60
