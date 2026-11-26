"""An approved plan resumes after a redelivery (BOSSAPP-1364).

A build's job was redelivered while the laptop slept (reclaimed once: epoch 2), the build
reached its plan gate and parked, a person approved the plan — and every resume was refused
"owned by another execution (rejected at claim)". The approval enqueued a NEW job whose first
lease was epoch 1, older than the run's 2, so the fence turned the person's Yes away; twenty
minutes later the Shepherd closed the run "abandoned: no job remains". An hour of planning lost
and nothing told anyone.

Pinned here, on the real database and the real approval path:
  * park on the plan -> approve -> the resume's claim succeeds (it failed before the fix);
  * a stale owner whose lease expired is taken over, a live rival still fences;
  * the Shepherd re-queues an approved run instead of cancelling it, and asks once at its cap.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.core import now_utc
from boss_engine.services.jobs import JobLeaseService, enqueue_job
from boss_engine.services.run_fencing import (
    RELEASED, RunFenced, claim_run, guarded_update, hash_token, is_owner, release_run)

from ._ambient_support import eng, project, purge  # noqa: F401


def _jobs() -> JobLeaseService:
    return JobLeaseService(get_settings())


def _ceo(c, pid):
    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
        "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{pid}_ceo", "p": pid})


def _run(c, pid) -> str:
    rid = f"crun_rs_{uuid4().hex[:12]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title) "
                   "VALUES (:id, :p, 'running', 'design', 'Approved plan resumes')"),
              {"id": rid, "p": pid})
    return rid


def _lease(eng, pid, jid, worker):  # noqa: F811
    with eng.begin() as c:
        return _jobs().lease_owned_job(c, project_id=pid, job_id=jid, worker_id=worker,
                                       lease_ttl_seconds=60)


def _expire(eng, jid):  # noqa: F811
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET lease_expires_at = :exp, heartbeat_at = :exp "
                       "WHERE id = :j"), {"j": jid, "exp": now_utc() - timedelta(minutes=6)})


def _reclaim(eng, jid, worker):  # noqa: F811
    for _ in range(20):
        with eng.begin() as c:
            got = _jobs().claim_job(c, worker, ["code.task"], 60)
        if got and got["id"] == jid:
            return got
    raise AssertionError("the expired job was never reclaimed")


def _park_on_plan(eng, pid, rid, token=None) -> str:  # noqa: F811
    """What _block_for_plan_approval writes: the plan card, the parked run, ownership released."""
    dec = f"dec_rs_{uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation) "
            "VALUES (:d, :p, 'Approve the technical plan?', CAST(:r AS JSONB))"),
            {"d": dec, "p": pid, "r": json.dumps({"action": "resume_code_task", "run_id": rid})})
        c.execute(text("UPDATE code_task_runs SET status = 'blocked', "
                       "phase = 'awaiting_plan_approval', decision_id = :d WHERE id = :r"),
                  {"d": dec, "r": rid})
        release_run(c, rid, token)
    return dec


def _approve(eng, dec):  # noqa: F811
    from boss_engine.services.decisions import DecisionService

    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, dec, action="approve", actor="ceo",
                                                   note="Go ahead.")


def _resume_job(eng, pid, rid) -> str:  # noqa: F811
    with eng.begin() as c:
        jid = c.execute(text(
            "SELECT id FROM jobs WHERE project_id = :p AND kind = 'code.task' "
            "AND payload->>'run_id' = :r AND status = 'queued' ORDER BY created_at DESC LIMIT 1"),
            {"p": pid, "r": rid}).scalar()
    assert jid, "approving the plan must queue the resume"
    return jid


def _cleanup(eng, pid):  # noqa: F811
    with eng.begin() as c:
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": pid})
    purge(eng, pid)


def test_park_approve_redelivery_the_resume_claims_the_run(eng, project):  # noqa: F811
    """THE incident: first job reclaimed once, parks, approved -> the resume must own the run."""
    with eng.begin() as c:
        _ceo(c, project)
        rid = _run(c, project)
        j1 = enqueue_job(c, project, "code.task", {"run_id": rid}, priority=20,
                         max_attempts=5)["id"]
    try:
        _lease(eng, project, j1, "worker-a")
        _expire(eng, j1)                      # the laptop slept
        redelivered = _reclaim(eng, j1, "worker-b")
        assert redelivered["fence_epoch"] == 2
        owner = hash_token(redelivered["lease_token"])
        with eng.begin() as c:
            assert claim_run(c, rid, owner) is True
            # Only the owner releases: a straggler with another token cannot.
            release_run(c, rid, hash_token("lease_someone_else"))
            assert is_owner(c, rid, owner)

        dec = _park_on_plan(eng, project, rid, owner)
        with eng.begin() as c:            # the parked job ends done
            c.execute(text("UPDATE jobs SET status = 'completed' WHERE id = :j"), {"j": j1})
            assert c.execute(text("SELECT owner_token FROM code_task_runs WHERE id = :r"),
                             {"r": rid}).scalar() == RELEASED, "a parked run is owned by nobody"
            # The parked execution is not the owner any more: no check passes, no write lands.
            assert not is_owner(c, rid, owner)
            with pytest.raises(RunFenced):
                guarded_update(c, rid, owner, "phase = :ph", {"ph": "implementing"})

        _approve(eng, dec)
        j2 = _resume_job(eng, project, rid)
        resume = _lease(eng, project, j2, "worker-c")
        assert resume["fence_epoch"] > 2, "a new job's epoch continues the run's, never restarts"
        with eng.begin() as c:
            assert claim_run(c, rid, hash_token(resume["lease_token"])) is True
            owner, epoch = c.execute(text(
                "SELECT owner_token, fence_epoch FROM code_task_runs WHERE id = :r"),
                {"r": rid}).one()
        assert owner == hash_token(resume["lease_token"])
        assert int(epoch) == resume["fence_epoch"]
    finally:
        _cleanup(eng, project)


def test_a_stale_owner_whose_lease_expired_is_taken_over(eng, project):  # noqa: F811
    """A run stamped by an older job (legacy epochs, or a redelivery left live with its lease
    lapsed) does not fence the current lease once nothing else is alive."""
    with eng.begin() as c:
        rid = _run(c, project)
        stale = enqueue_job(c, project, "code.task", {"run_id": rid}, priority=20)["id"]
        fresh = enqueue_job(c, project, "code.task", {"run_id": rid}, priority=20)["id"]
    try:
        _lease(eng, project, stale, "worker-a")
        current = _lease(eng, project, fresh, "worker-b")
        with eng.begin() as c:   # the run carries a newer epoch than the current lease
            c.execute(text("UPDATE code_task_runs SET fence_epoch = 9 WHERE id = :r"), {"r": rid})
            c.execute(text("UPDATE jobs SET fence_epoch = 1 WHERE id = :j"), {"j": fresh})
        token = hash_token(current["lease_token"])

        # While the other job's lease is live it is a real rival: the current one stands down.
        with eng.begin() as c:
            with pytest.raises(RunFenced):
                claim_run(c, rid, token)

        # Its lease lapses: it is a ghost, and the current lease takes the run over.
        _expire(eng, stale)
        with eng.begin() as c:
            assert claim_run(c, rid, token) is True
            owner, epoch = c.execute(text(
                "SELECT owner_token, fence_epoch FROM code_task_runs WHERE id = :r"),
                {"r": rid}).one()
        assert owner == token
        with eng.begin() as c:
            job_epoch = c.execute(text("SELECT fence_epoch FROM jobs WHERE id = :j"),
                                  {"j": fresh}).scalar()
        assert int(epoch) == 10 and int(job_epoch) == 10, (
            "a takeover is a handover: the epoch moves strictly forward on the lease and the run")
    finally:
        _cleanup(eng, project)


def test_the_shepherd_resumes_an_approved_run_instead_of_cancelling_it(eng, project):  # noqa: F811
    from boss_engine.services.shepherd_service import (
        APPROVED_RESUME_MAX_REQUEUES, sweep_abandoned_runs)

    with eng.begin() as c:
        _ceo(c, project)
        rid = _run(c, project)
    try:
        dec = _park_on_plan(eng, project, rid)
        _approve(eng, dec)
        with eng.begin() as c:   # the approval's resume was lost: no job, run quiet for an hour
            c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": project})
            c.execute(text("UPDATE code_task_runs SET updated_at = now() - interval '1 hour' "
                           "WHERE id = :r"), {"r": rid})

        sweep_abandoned_runs(eng, get_settings(), project)
        with eng.begin() as c:
            status, error = c.execute(text(
                "SELECT status, error FROM code_task_runs WHERE id = :r"), {"r": rid}).one()
            queued = c.execute(text(
                "SELECT count(*) FROM jobs WHERE project_id = :p AND kind = 'code.task' "
                "AND payload->>'run_id' = :r AND status = 'queued'"),
                {"p": project, "r": rid}).scalar()
        assert status == "running", error
        assert "abandoned" not in str(error or "")
        assert queued == 1, "the approved plan's resume is queued again"

        # At the cap it asks a person once — and still never cancels.
        with eng.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": project})
            c.execute(text(
                "UPDATE code_task_runs SET updated_at = now() - interval '1 hour', "
                "finalize_ctx = jsonb_build_object('approved_resume_requeues', CAST(:n AS INT)) "
                "WHERE id = :r"), {"r": rid, "n": APPROVED_RESUME_MAX_REQUEUES})
        sweep_abandoned_runs(eng, get_settings(), project)
        with eng.begin() as c:
            status = c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"),
                               {"r": rid}).scalar()
            asked = c.execute(text(
                "SELECT count(*) FROM shepherd_escalations WHERE task_ref = :r "
                "AND status = 'open'"), {"r": rid}).scalar()
        assert status != "cancelled"
        assert asked == 1

        # ONE question: the next sweep asks nothing more.
        with eng.begin() as c:
            c.execute(text("UPDATE code_task_runs SET updated_at = now() - interval '1 hour' "
                           "WHERE id = :r"), {"r": rid})
            esc = c.execute(text("SELECT id FROM shepherd_escalations WHERE task_ref = :r"),
                            {"r": rid}).scalar()
        sweep_abandoned_runs(eng, get_settings(), project)
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM shepherd_escalations WHERE task_ref = :r"),
                             {"r": rid}).scalar() == 1

        # "Retry" does what it says: the count starts over and the resume is queued.
        from boss_engine.services.system_doctor import act

        act(eng, get_settings(), f"shepherd:{esc}", "retry", actor="ceo")
        with eng.begin() as c:
            row = c.execute(text(
                "SELECT status, finalize_ctx FROM code_task_runs WHERE id = :r"),
                {"r": rid}).mappings().one()
            queued = c.execute(text(
                "SELECT count(*) FROM jobs WHERE project_id = :p AND kind = 'code.task' "
                "AND payload->>'run_id' = :r AND status = 'queued'"),
                {"p": project, "r": rid}).scalar()
        assert row["status"] == "running" and queued == 1
        assert row["finalize_ctx"]["approved_resume_requeues"] == 0
        assert row["finalize_ctx"]["approved_resume_asked"]["answer"] == "retry"

        # Answered once, never asked again — even at the cap.
        with eng.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": project})
            c.execute(text(
                "UPDATE code_task_runs SET updated_at = now() - interval '1 hour', "
                "finalize_ctx = finalize_ctx || jsonb_build_object("
                "'approved_resume_requeues', CAST(:n AS INT)) WHERE id = :r"),
                {"r": rid, "n": APPROVED_RESUME_MAX_REQUEUES})
        sweep_abandoned_runs(eng, get_settings(), project)
        with eng.begin() as c:
            assert c.execute(text(
                "SELECT count(*) FROM shepherd_escalations WHERE task_ref = :r AND "
                "status = 'open'"), {"r": rid}).scalar() == 0
            assert c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"),
                             {"r": rid}).scalar() != "cancelled"
    finally:
        _cleanup(eng, project)


def test_clean_up_never_cancels_an_approved_plan(eng, project):  # noqa: F811
    """Clean up's stale pass cancels a live build nobody waits on. An approved plan still at its
    gate is not that: a person said Yes, and the Shepherd resumes it."""
    from boss_engine.services.system_doctor import clean_up_old

    with eng.begin() as c:
        _ceo(c, project)
        rid = _run(c, project)
    try:
        dec = _park_on_plan(eng, project, rid)
        _approve(eng, dec)
        with eng.begin() as c:
            c.execute(text("UPDATE code_task_runs SET updated_at = now() - interval '30 days' "
                           "WHERE id = :r"), {"r": rid})
            c.execute(text(
                "INSERT INTO shepherd_escalations (id, project_id, task_ref, question, options, "
                "evidence, created_at) VALUES (:i, :p, :r, 'Stuck?', '[]'::jsonb, '{}'::jsonb, "
                "now() - interval '30 days')"),
                {"i": f"shesc_{uuid4().hex[:10]}", "p": project, "r": rid})
        clean_up_old(eng, get_settings(), project, actor="ceo")
        with eng.begin() as c:
            status = c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"),
                               {"r": rid}).scalar()
        assert status == "running"
    finally:
        _cleanup(eng, project)
