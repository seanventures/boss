"""Fencing epochs: a reclaimed worker's late writes are impossible (BOSSAPP-986).

A lease timeout alone does not stop an old worker. run_fencing's owner token fenced its
WRITES, but the claim itself was unconditional — a stale executor whose claim landed late took
the token back from the legitimate reclaimer (the two-executions race measured on
job_2fbe88e6). Ownership now carries a monotonically increasing epoch, granted with the lease
(JobLeaseService.claim_job / lease_owned_job) and stamped onto the run; a commit from a stale
epoch matches 0 rows at the database boundary.

Kill-the-worker shape: worker A leases and starts, dies (lease expires), worker B reclaims.
Every write A attempts afterwards — the claim, the phase move, the status commit — must be
rejected by the database, not by A's goodwill.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.core import now_utc
from boss_engine.services.jobs import JobLeaseService, enqueue_job
from boss_engine.services.run_fencing import RunFenced, claim_run, guarded_update, hash_token

from ._ambient_support import eng, make_project, project, purge  # noqa: F401


def _run(c, pid) -> str:
    rid = f"crun_fe_{uuid4().hex[:12]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title) "
                   "VALUES (:id, :p, 'running', 'queued', 'Fencing epochs')"),
              {"id": rid, "p": pid})
    return rid


def _lease(eng, pid, jid, worker, ttl=60):
    jobs = JobLeaseService(get_settings())
    with eng.begin() as c:
        return jobs.lease_owned_job(c, project_id=pid, job_id=jid, worker_id=worker,
                                    lease_ttl_seconds=ttl)


def _expire(eng, jid):
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET lease_expires_at = :exp, heartbeat_at = :exp "
                       "WHERE id = :j"), {"j": jid, "exp": now_utc() - timedelta(minutes=6)})


def _reclaim(eng, worker):
    jobs = JobLeaseService(get_settings())
    with eng.begin() as c:
        return jobs.claim_job(c, worker, ["code.task"], 60)


def _cleanup(eng, pid):
    with eng.begin() as c:
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": pid})


def test_every_lease_handover_increments_the_epoch_and_stamps_the_run(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = _run(c, project)
        jid = enqueue_job(c, project, "code.task", {"run_id": rid}, priority=20,
                          max_attempts=5)["id"]
    try:
        first = _lease(eng, project, jid, "worker-a")
        assert first["fence_epoch"] == 1

        _expire(eng, jid)
        # The reclaim is the handover: claim_job takes expired leased jobs.
        reclaimed = None
        for _ in range(15):
            reclaimed = _reclaim(eng, "worker-b")
            if reclaimed and reclaimed["id"] == jid:
                break
        assert reclaimed and reclaimed["id"] == jid
        assert reclaimed["fence_epoch"] == 2

        with eng.begin() as c:
            job_epoch, run_epoch = c.execute(
                text("SELECT j.fence_epoch, r.fence_epoch FROM jobs j, code_task_runs r "
                     "WHERE j.id = :j AND r.id = :r"), {"j": jid, "r": rid}).one()
        assert int(job_epoch) == 2
        assert int(run_epoch) == 2, "the reclaim must stamp the run with the new epoch"
    finally:
        _cleanup(eng, project)


def test_a_stale_executor_cannot_take_the_run_back_or_write_to_it(eng, project):  # noqa: F811
    """THE kill-the-worker test: A dies mid-run, B reclaims, A wakes up and tries everything."""
    with eng.begin() as c:
        rid = _run(c, project)
        jid = enqueue_job(c, project, "code.task", {"run_id": rid}, priority=20,
                          max_attempts=5)["id"]
    try:
        lease_a = _lease(eng, project, jid, "worker-a")
        token_a = hash_token(lease_a["lease_token"])
        with eng.begin() as c:
            assert claim_run(c, rid, token_a) is True

        # Worker A dies: its lease expires; worker B reclaims the job.
        _expire(eng, jid)
        reclaimed = None
        for _ in range(15):
            reclaimed = _reclaim(eng, "worker-b")
            if reclaimed and reclaimed["id"] == jid:
                break
        assert reclaimed and reclaimed["id"] == jid
        token_b = hash_token(reclaimed["lease_token"])
        with eng.begin() as c:
            assert claim_run(c, rid, token_b) is True

        # A wakes up. Its late claim is refused — it can NOT take the run back.
        with eng.begin() as c:
            with pytest.raises(RunFenced):
                claim_run(c, rid, token_a)

        # Its writes are rejected at the database boundary: 0 rows, RunFenced.
        with eng.begin() as c:
            with pytest.raises(RunFenced):
                guarded_update(c, rid, token_a, "phase = :ph", {"ph": "implementing"},
                               where="stale write")
        with eng.begin() as c:
            phase, owner = c.execute(
                text("SELECT phase, owner_token FROM code_task_runs WHERE id = :r"),
                {"r": rid}).one()
        assert phase == "queued", "the stale worker's write must not land"
        assert owner == token_b, "ownership stays with the reclaiming worker"

        # And B, the current owner, still writes freely.
        with eng.begin() as c:
            guarded_update(c, rid, token_b, "phase = :ph", {"ph": "implementing"},
                           where="owner write")
        with eng.begin() as c:
            assert c.execute(text("SELECT phase FROM code_task_runs WHERE id = :r"),
                             {"r": rid}).scalar() == "implementing"
    finally:
        _cleanup(eng, project)


def test_a_late_control_plane_stamp_cannot_move_the_epoch_backwards(eng, project):  # noqa: F811
    """The run-stamp itself is monotonic: `WHERE fence_epoch < :epoch` at the write."""
    with eng.begin() as c:
        rid = _run(c, project)
        c.execute(text("UPDATE code_task_runs SET fence_epoch = 5 WHERE id = :r"), {"r": rid})
        # A delayed stamp from an older claim (epoch 3) arrives late: it must match 0 rows.
        moved = c.execute(text("UPDATE code_task_runs SET fence_epoch = :e "
                               "WHERE id = :r AND fence_epoch < :e"),
                          {"r": rid, "e": 3}).rowcount
        assert moved == 0
        assert c.execute(text("SELECT fence_epoch FROM code_task_runs WHERE id = :r"),
                         {"r": rid}).scalar() == 5
    with eng.begin() as c:
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})


def test_a_run_with_no_live_lease_keeps_the_old_claim_behavior(eng, project):  # noqa: F811
    """Dev and direct-API paths (no job) stay unfenced rather than breaking."""
    with eng.begin() as c:
        rid = _run(c, project)
    try:
        token = hash_token(f"lease_{uuid4().hex}")
        with eng.begin() as c:
            assert claim_run(c, rid, token) is True
            assert c.execute(text("SELECT owner_token FROM code_task_runs WHERE id = :r"),
                             {"r": rid}).scalar() == token
    finally:
        _cleanup(eng, project)


def test_a_stale_lease_token_is_refused_before_any_work(eng, project):  # noqa: F811
    """An executor whose job has been re-leased is fenced at the CLAIM, not mid-stage."""
    with eng.begin() as c:
        rid = _run(c, project)
        jid = enqueue_job(c, project, "code.task", {"run_id": rid}, priority=20,
                          max_attempts=5)["id"]
    try:
        lease_a = _lease(eng, project, jid, "worker-a")
        token_a = hash_token(lease_a["lease_token"])
        _expire(eng, jid)
        reclaimed = None
        for _ in range(15):
            reclaimed = _reclaim(eng, "worker-b")
            if reclaimed and reclaimed["id"] == jid:
                break
        assert reclaimed and reclaimed["id"] == jid
        # A's execution starts only now (slow boot). Its very first claim is refused.
        with eng.begin() as c:
            with pytest.raises(RunFenced):
                claim_run(c, rid, token_a)
    finally:
        _cleanup(eng, project)
