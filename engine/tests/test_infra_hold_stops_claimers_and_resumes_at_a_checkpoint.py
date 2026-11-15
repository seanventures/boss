"""The install-wide work hold (LLD §4.2, §9): with the hold set, claim_job, the trigger clock,
ambient admit_start and admission all say no (exempt kinds still claim); a running step hands its
job back at a checkpoint with NO attempt spent; release resumes oldest first, exactly once, and
the stale lease is fenced out (BOSSAPP-986); the hold never kills.

Runs against BOSS_TEST_DATABASE_URL (the bench DB). The hold is always released on the way out:
other suites share that database.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import work_hold

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
P = "p_infra_hold"


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_ih', 'ih') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_ih', 'org_ih', 'ih') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_ih', 'ih', 'p-infra-hold') ON CONFLICT DO NOTHING"), {"p": P})
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": work_hold.KEY})
    work_hold.invalidate_cache()
    yield eng
    with eng.begin() as c:
        work_hold.release(c, actor="test", reason="suite done")
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": work_hold.KEY})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": P})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": P})
    work_hold.invalidate_cache()
    eng.dispose()


@pytest.fixture()
def released(pg):
    """Every test starts and ends with no hold (and a cold cache)."""
    with pg.begin() as c:
        work_hold.release(c, actor="test", reason="reset")
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": work_hold.KEY})
    work_hold.invalidate_cache()
    yield
    with pg.begin() as c:
        work_hold.release(c, actor="test", reason="reset")
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": work_hold.KEY})
    work_hold.invalidate_cache()


def _job(c, kind: str = "code.task", payload: dict | None = None) -> str:
    import json

    job_id = f"job_ih_{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, run_after, created_at) "
                   "VALUES (:id, :p, :k, CAST(:pl AS JSONB), 'queued', now(), clock_timestamp())"),
              {"id": job_id, "p": P, "k": kind, "pl": json.dumps(payload or {})})
    return job_id


def _svc():
    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    return JobLeaseService(Settings())


def _id(lease) -> str:
    return str(lease.get("job_id") or lease.get("id"))


def _row(c, job_id):
    return c.execute(text("SELECT status, attempt, run_after, lease_owner, fence_epoch, last_error "
                          "FROM jobs WHERE id = :id"), {"id": job_id}).mappings().one()


# ── every claimer ───────────────────────────────────────────────────────────────────────────

def test_work_hold_stops_all_claimers(pg, released, monkeypatch):
    from boss_engine.services import admission, agent_triggers
    from boss_engine.services.ambient import guards

    with pg.begin() as c:
        held_job = _job(c, "meeting.step")          # light: admission never holds it; the hold must
        exempt_job = _job(c, "infra.probe")         # a kind on the exempt list still runs
        work_hold.set_hold(c, incident_id="si_test", host_id="h_test", actor="agent:infra", project_id=P)
    work_hold.invalidate_cache()
    with pg.begin() as c:
        assert work_hold.active(c) and work_hold.active(c, "meeting.step")
        assert not work_hold.active(c, "infra.probe"), "infra.* is exempt"
        assert not work_hold.active(c, "agent.turn.interactive"), "chat is exempt"
        # 1. claim_job: the held kind stays queued, stamped; the exempt kind leases.
        assert _svc().claim_job(c, "w_ih", ["meeting.step"], 60) is None
        row = _row(c, held_job)
        assert row["status"] == "queued" and row["attempt"] == 0
        assert row["last_error"]["code"] == work_hold.REASON
        assert row["last_error"]["incident_id"] == "si_test"
        assert "Infra Agent" in row["last_error"]["message"]
        leased = _svc().claim_job(c, "w_ih", ["infra.probe"], 60)
        assert leased is not None and _id(leased) == exempt_job
        # 3. the trigger clock fires nothing and advances nothing.
        before = c.execute(text("SELECT count(*) FROM agent_triggers WHERE status = 'on' "
                                "AND next_fire_at <= now()")).scalar()
        got = agent_triggers.tick(c, project_id=P)
        assert got.get("held") is True and got["fired"] == []
        after = c.execute(text("SELECT count(*) FROM agent_triggers WHERE status = 'on' "
                               "AND next_fire_at <= now()")).scalar()
        assert after >= before, "next_fire_at is not advanced under the hold"
        # 4. ambient admit_start refuses with the hold's own guard.
        verdict = guards.admit_start(c, project_id=P)
        assert verdict["ok"] is False and verdict["guard"] == "infra_hold"
        assert verdict["incident_id"] == "si_test"
        # 5. admission refuses a heavy job with its own code.
        decision = admission.admit_job(c, "code.task")
        assert not decision.admitted and decision.code == "infra_hold"
    # 2. claim_step consults the hold before leasing (its run needs a whole workflow; the door is
    #    pinned by source, as the admission suite pins its own gate).
    from boss_engine.services import workflows

    src = inspect.getsource(workflows.claim_step)
    assert "work_hold.active(connection" in src and src.index("work_hold.active") < src.index("FOR UPDATE SKIP LOCKED")
    with pg.begin() as c:
        work_hold.release(c, actor="test", reason="done", project_id=P)
    work_hold.invalidate_cache()
    with pg.begin() as c:
        assert not work_hold.active(c)
        leased = _svc().claim_job(c, "w_ih", ["meeting.step"], 60)
        assert leased is not None and _id(leased) == held_job
        assert _row(c, held_job)["attempt"] == 1, "the refused polls spent no attempt"


# ── a running step hands back at its checkpoint, no attempt spent, no duplicate ────────────

def test_running_step_hands_back_at_checkpoint_without_attempt(pg, released):
    from boss_engine.services.jobs import LeaseNotFoundError

    with pg.begin() as c:
        job_id = _job(c, "meeting.step")
        lease = _svc().claim_job(c, "w_ih", ["meeting.step"], 60)
        assert _id(lease) == job_id
        token, epoch_1 = lease["lease_token"], _row(c, job_id)["fence_epoch"]
        assert _row(c, job_id)["attempt"] == 1
        work_hold.set_hold(c, incident_id="si_cp", host_id="h", project_id=P)
    work_hold.invalidate_cache()
    with pg.begin() as c:
        out = work_hold.hand_back(c, _svc(), worker_id="w_ih", job_id=job_id, lease_token=token)
        assert out["handed_back"] and out["incident_id"] == "si_cp"
        row = _row(c, job_id)
        assert row["status"] == "queued" and row["attempt"] == 0, "no attempt spent"
        assert row["lease_owner"] is None
        assert row["last_error"]["code"] == work_hold.REASON and row["last_error"]["handed_back"]
        # the old lease is dead: a stale writer cannot touch the job on it
        with pytest.raises(LeaseNotFoundError):
            _svc().require_active_lease(c, "w_ih", job_id, token)
        # while the hold is on, nobody claims it
        assert _svc().claim_job(c, "w_ih", ["meeting.step"], 60) is None
    with pg.begin() as c:
        out = work_hold.release(c, actor="agent:infra", reason="target reached", project_id=P)
        assert out["resumed_jobs"] >= 1 and not out["hold"]["active"]
        assert _row(c, job_id)["run_after"] <= c.execute(text("SELECT now()")).scalar()
    work_hold.invalidate_cache()
    with pg.begin() as c:
        again = _svc().claim_job(c, "w_ih", ["meeting.step"], 60)
        assert again is not None and _id(again) == job_id
        row = _row(c, job_id)
        assert row["attempt"] == 1, "the resumed attempt is the first real one, not a second"
        assert row["fence_epoch"] > epoch_1, "reclaim bumps the fence: the paused lease is fenced out"
        with pytest.raises(LeaseNotFoundError):
            _svc().require_active_lease(c, "w_ih", job_id, token)


def test_release_resumes_oldest_first_once(pg, released):
    with pg.begin() as c:
        first = _job(c, "meeting.step")
        second = _job(c, "meeting.step")
        work_hold.set_hold(c, incident_id="si_order", host_id="h", project_id=P)
    work_hold.invalidate_cache()
    with pg.begin() as c:
        assert _svc().claim_job(c, "w_ih", ["meeting.step"], 60) is None
        stamped = c.execute(text("SELECT count(*) FROM jobs WHERE id IN (:a, :b) AND "
                                 "last_error->>'code' = :code"), {"a": first, "b": second,
                                                                   "code": work_hold.REASON}).scalar()
        assert stamped == 2
        assert work_hold.paused_count(c) >= 2
        out = work_hold.release(c, actor="test", reason="done", project_id=P)
        assert out["resumed_jobs"] >= 2
        assert _row(c, first)["run_after"] < _row(c, second)["run_after"], "oldest first"
        assert _row(c, first)["last_error"]["resumed_at"]
    work_hold.invalidate_cache()
    with pg.begin() as c:
        a = _svc().claim_job(c, "w_ih", ["meeting.step"], 60)
        b = _svc().claim_job(c, "w_ih", ["meeting.step"], 60)
        assert (_id(a), _id(b)) == (first, second)
        # a second release is a no-op: nothing is queued twice, nothing runs twice
        again = work_hold.release(c, actor="test", reason="again", project_id=P)
        assert again["resumed_jobs"] == 0
        assert _row(c, first)["status"] == "leased" and _row(c, first)["attempt"] == 1


def test_hold_never_kills():
    """No terminate/cancel anywhere in the hold; a build pauses only at its own stage boundary."""
    import re

    src = inspect.getsource(work_hold)
    assert not re.search(r"terminate|cancel_run|kill\(", src)
    from boss_engine.services import governed_code_task_service as g

    gsrc = inspect.getsource(g)
    raise_sites = [m.start() for m in re.finditer(r"raise _PauseAtCheckpoint", gsrc)]
    assert len(raise_sites) == 1
    boundary = gsrc.index("def _mark_stage_done(stage: str) -> None:")
    assert boundary < raise_sites[0] < boundary + 900, "raised only right after a stage is marked done"
    assert "mark_stage_done(engine, project_id, run_id, stage)" in gsrc[boundary:raise_sites[0]], \
        "the stage's work and its completed_stages marker are written BEFORE the hand-back"


def test_the_kernel_drain_hook_folds_the_hold_in_and_settle_tells_them_apart():
    from boss_engine.services import agent_run_worker as w

    src = inspect.getsource(w)
    assert "work_hold.drain_or_hold(engine, drain, kind, run_id)" in src
    settle = inspect.getsource(w._settle)
    assert "work_hold.hand_back(" in settle and 'reason="interrupted_by_deploy"' in settle
    assert settle.index("work_hold.hand_back(") < settle.index('reason="interrupted_by_deploy"')


def test_a_hold_expires_after_its_maximum_and_the_approve_body_reaches_the_card():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
    hold = {"active": True, "since": (now - timedelta(hours=1)).isoformat()}
    assert not work_hold.expired(hold, 7200, now=now)
    assert work_hold.expired(hold, 1800, now=now)
    assert not work_hold.expired({**hold, "active": False}, 60, now=now)
    assert not work_hold.expired({"active": True}, 60, now=now), "no since: never expired by time"
    from boss_engine.config import Settings

    assert Settings.model_fields["storage_max_hold_seconds"].default == 7200
    # the watch applies it; the approve body's {make_rule, args} reaches the storage card hook
    from boss_engine.services import decisions, storage_watch

    assert "work_hold.expired(hold, max_seconds)" in inspect.getsource(storage_watch.tick)
    hook = inspect.getsource(decisions.DecisionService._handle_approved_follow_on)
    assert "make_rule=body.get(\"make_rule\")" in hook and "answer" in hook
