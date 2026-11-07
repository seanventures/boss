"""A job whose every attempt is lost ends, in words; a lease nobody will ever take back is reaped.

The laptop, 2026-11-07: reclaiming an expired lease added an attempt and never compared it with
max_attempts. Four CVE builds whose step endpoint crashed before the worker could report were
re-leased every five minutes -- attempt 30 of 3 -- and the console said "Queued" for hours.
The jobs table also held 18 `connection.proof` jobs `running` since October (their API thread
long gone) and an `architecture.draft` `leased` to a worker id that no longer exists: kinds no
worker leases, so claim_job never saw them and nothing else looked.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.core import now_utc
from boss_engine.services.jobs import JobLeaseService

from ._ambient_support import eng, make_project, project, purge  # noqa: F401


def _run(c, pid) -> str:
    rid = f"crun_ll_{uuid4().hex[:12]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title) "
                   "VALUES (:id, :p, 'running', 'queued', 'Bump esbuild')"), {"id": rid, "p": pid})
    return rid


def _job(c, pid, *, kind="code.task", run_id=None, status="running", attempt=1, max_attempts=3,
         expired_minutes=6, owner="community-worker-1-s2") -> str:
    jid = f"job_ll_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, payload, attempt, max_attempts, "
        "lease_owner, lease_expires_at, heartbeat_at, on_behalf_of, chain, on_behalf_system) "
        "VALUES (:j, :p, :k, :s, CAST(:pl AS jsonb), :a, :m, :o, :exp, :exp, 'sys_worker', "
        "'[]'::jsonb, true)"),
        {"j": jid, "p": pid, "k": kind, "s": status, "a": attempt, "m": max_attempts, "o": owner,
         "pl": json.dumps({"run_id": run_id} if run_id else {}),
         "exp": now_utc() - timedelta(minutes=expired_minutes)})
    return jid


def _cleanup(eng, pid):
    with eng.begin() as c:
        c.execute(text("DELETE FROM deadletters WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": pid})


def test_a_reclaim_past_its_last_attempt_ends_the_job_and_says_why_on_the_build(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = _run(c, project)
        jid = _job(c, project, run_id=rid, attempt=30, max_attempts=3)
    try:
        with eng.begin() as c:
            got = JobLeaseService(get_settings()).claim_job(c, "worker-test", ["code.task"], 300)
        assert got is None or got["id"] != jid, "a spent job is never leased again"
        with eng.begin() as c:
            job = c.execute(text("SELECT status, last_error FROM jobs WHERE id = :j"),
                            {"j": jid}).mappings().one()
            run = c.execute(text("SELECT status, error FROM code_task_runs WHERE id = :r"),
                            {"r": rid}).mappings().one()
        assert job["status"] == "deadlettered"
        assert job["last_error"]["code"] == "lease_lost"
        assert run["status"] == "failed"
        assert "stopped without reporting back" in run["error"] and "30 of 3" in run["error"]
    finally:
        _cleanup(eng, project)


def test_a_lease_nobody_will_take_back_is_reaped_spent_ends_unspent_requeues(eng, project):  # noqa: F811
    with eng.begin() as c:
        proof = _job(c, project, kind="connection.proof", attempt=1, max_attempts=1,
                     expired_minutes=60 * 24 * 20, owner="setup:ceo:abc")
        draft = _job(c, project, kind="architecture.draft", status="leased", attempt=1,
                     max_attempts=3, expired_minutes=120, owner="boss-worker")
        fresh = _job(c, project, kind="connection.proof", attempt=1, max_attempts=1,
                     expired_minutes=5, owner="setup:ceo:def")
    try:
        with eng.begin() as c:
            out = JobLeaseService(get_settings()).reap_lost_leases(c)
        assert out["retired"] >= 1 and out["requeued"] >= 1
        with eng.begin() as c:
            st = dict(c.execute(text("SELECT id, status FROM jobs WHERE id IN (:a, :b, :c)"),
                                {"a": proof, "b": draft, "c": fresh}).all())
            reason = c.execute(text("SELECT reason FROM deadletters WHERE source_id = :j"),
                               {"j": proof}).scalar()
        assert st[proof] == "deadlettered"
        assert "lost its holder" in reason and "setup:ceo:abc" in reason
        assert st[draft] == "queued"                 # attempts left: back in the queue
        assert st[fresh] == "running"                # a lease just expired is claim_job's, not ours
    finally:
        _cleanup(eng, project)


def test_the_reaper_tick_runs_the_job_pass(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import resource_reaper

    with eng.begin() as c:
        jid = _job(c, project, kind="connection.proof", attempt=1, max_attempts=1,
                   expired_minutes=600, owner="setup:x")
    try:
        assert resource_reaper.reap_jobs(eng, get_settings())["retired"] >= 1
        with eng.begin() as c:
            assert c.execute(text("SELECT status FROM jobs WHERE id = :j"),
                             {"j": jid}).scalar() == "deadlettered"
    finally:
        _cleanup(eng, project)
