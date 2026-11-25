"""Every time cap counts from when the work STARTED, and a credential that lapses mid-run says
what lapsed and what to do (R3, BOSSAPP-1431).

An enterprise partner's 4-core / 8 GiB host ran one job slot and queued work for 300+ minutes.
A self-repair was then refused after "311 minutes (the most is 60)", all of it spent in the
queue. Our hosts never queue for long, so no cap had a long-queue case. Each cap below has:

- QUEUED: the work waited N > cap minutes before it started, and is not refused for it;
- LAPSED: what it holds (a lease, a hold, a token, a handle) ended while it ran, and the
  refusal names what lapsed and the next step.

Caps covered here (the self-fix's own are in test_a_self_fix_keeps_its_clock_and_hold_through_a_long_queue.py):
deployments (fail_hung_deployments), investigations (open_investigation's abandon rule),
step recovery's window (budget_left), sandbox tasks (sandbox_agent_timeout_seconds), job
leases (claim_job / heartbeat), and the run tokens and handles of the MCP gateway, the model
proxy, the model gateway and egress. Caps that bound a WAIT by design (approval deadlines,
the Infra hold, agent_wait_max_seconds, an MCP ask) are not execution caps and are not here.

The follow-ups PR #315 filed are here too: a model-lane slot wait against the implement budget
and a review seat's deadline (BOSSAPP-1450), the MCP gateway token's lifetime against the
build's queue (1451), the build reservation through one long stage (1452), the CI wait against
time GitHub runners spend queued (1453), and the Task Shepherd's deadline against its remedies'
queue time (1454).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.services import delivery_pipeline, lapsed, step_recovery

from ._small_host import LONG_QUEUE, now_utc
from .test_workflow_capability_runner import eng, project  # noqa: F401

LONG = int(LONG_QUEUE.total_seconds())


# ── deployments ─────────────────────────────────────────────────────────────────────────────

def _target(c, pid: str) -> str:
    tid = f"dt_{uuid.uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        "target_kind, target_ref, status, created_by) VALUES (:t, :p, 'app', 'acme/app', "
        "'staging', 'ssh', 'deploy@203.0.113.9:22', 'active', 'test')"), {"t": tid, "p": pid})
    return tid


def _deployment(c, pid: str, tid: str, *, status: str, planned_ago: timedelta,
                steps_began_ago=None, started_ago=None, changeset=None) -> str:
    did = f"deprun_{uuid.uuid4().hex[:8]}"
    steps = []
    if steps_began_ago is not None:
        steps = [{"step": "resolve", "status": "ok",
                  "started_at": (now_utc() - steps_began_ago).isoformat()}]
    planned = now_utc() - planned_ago
    c.execute(text(
        "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
        "environment, repo, target_kind, target_ref, status, created_by, created_at, "
        "started_at, steps, changeset_id) VALUES (:d, :p, :t, 'app', 'staging', 'acme/app', "
        "'ssh', 'deploy@203.0.113.9:22', :s, 'test', :planned, :started, CAST(:steps AS JSONB), "
        ":cs)"),
        {"d": did, "p": pid, "t": tid, "s": status, "planned": planned, "cs": changeset,
         # started_at is stamped at PLAN time on insert (resource_plane), and again when
         # execution reaches `running`.
         "started": now_utc() - started_ago if started_ago is not None else planned,
         "steps": json.dumps(steps)})
    return did


@pytest.fixture()
def deployments(eng, project):  # noqa: F811
    yield eng, project
    with eng.begin() as c:
        c.execute(text("DELETE FROM deployment_runs WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM deployment_targets WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM changesets WHERE project_id = :p"), {"p": project})


def test_a_deployment_queued_past_the_cap_is_not_failed_as_hung(deployments):
    eng, pid = deployments
    cap = delivery_pipeline.DEPLOYMENT_MAX_RUNTIME_SECONDS
    with eng.begin() as c:
        tid = _target(c, pid)
        # App 3 of a sequential fan-out: planned 311 minutes ago, never started (no step yet).
        waiting = _deployment(c, pid, tid, status="created", planned_ago=LONG_QUEUE)
        # Planned 311 minutes ago, started 10 minutes ago: 10 minutes of runtime.
        began = _deployment(c, pid, tid, status="created", planned_ago=LONG_QUEUE,
                            steps_began_ago=timedelta(minutes=10))
        # Planned 311 minutes ago, started 50 minutes ago (cap 45): hung.
        hung = _deployment(c, pid, tid, status="created", planned_ago=LONG_QUEUE,
                           steps_began_ago=timedelta(minutes=50))
        # Running 50 minutes by its own started_at: hung, as before.
        running = _deployment(c, pid, tid, status="running", planned_ago=LONG_QUEUE,
                              started_ago=timedelta(minutes=50))
    out = delivery_pipeline.fail_hung_deployments(eng, max_seconds=cap)
    assert hung in out["failed"] and running in out["failed"]
    assert waiting not in out["failed"] and began not in out["failed"]
    assert waiting not in out["never_started"]
    with eng.begin() as c:
        status = dict(c.execute(text("SELECT id, status FROM deployment_runs WHERE project_id=:p"),
                                {"p": pid}).all())
    assert status[waiting] == "created" and status[began] == "created"


def test_a_deployment_planned_but_never_started_for_a_day_says_so(deployments):
    eng, pid = deployments
    with eng.begin() as c:
        tid = _target(c, pid)
        stranded = _deployment(c, pid, tid, status="created", planned_ago=timedelta(hours=25))
    out = delivery_pipeline.fail_hung_deployments(eng)
    assert stranded in out["never_started"] and stranded not in out["failed"]
    with eng.begin() as c:
        reason = c.execute(text("SELECT failure_reason FROM deployment_runs WHERE id = :d"),
                           {"d": stranded}).scalar()
    assert "never started" in reason and "deploy it again" in reason
    assert "ran longer than the maximum" not in reason


def _changeset(c, pid: str, status: str) -> str:
    cid = f"cs_{uuid.uuid4().hex[:8]}"
    c.execute(text("INSERT INTO changesets (id, project_id, title, summary, status, created_by) "
                   "VALUES (:i, :p, 'deploy', 'deploy', :s, 'test')"),
              {"i": cid, "p": pid, "s": status})
    return cid


def test_a_deployment_waits_for_its_changesets_person_but_not_for_a_rejected_one(deployments):
    eng, pid = deployments
    with eng.begin() as c:
        tid = _target(c, pid)
        waiting = _deployment(c, pid, tid, status="created", planned_ago=timedelta(hours=30),
                              changeset=_changeset(c, pid, "pending_approval"))
        rejected = _deployment(c, pid, tid, status="created", planned_ago=timedelta(hours=30),
                               changeset=_changeset(c, pid, "rejected"))
    out = delivery_pipeline.fail_hung_deployments(eng)
    assert rejected in out["never_started"]
    assert waiting not in out["never_started"] and waiting not in out["failed"]


# ── investigations ──────────────────────────────────────────────────────────────────────────

def _investigation(c, pid: str, *, status: str, ago: timedelta, job_status) -> str:
    iid = f"inv_{uuid.uuid4().hex[:8]}"
    job_id = None
    if job_status:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, created_at) "
                       "VALUES (:j, :p, 'investigation.run', '{}'::jsonb, :s, :at)"),
                  {"j": job_id, "p": pid, "s": job_status, "at": now_utc() - ago})
    c.execute(text(
        "INSERT INTO investigations (id, project_id, question, actor, status, job_id, "
        "created_at, updated_at) VALUES (:i, :p, 'why', 'test', :s, :j, :at, :at)"),
        {"i": iid, "p": pid, "s": status, "j": job_id, "at": now_utc() - ago})
    return iid


@pytest.fixture()
def investigations(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import debug_session

    monkeypatch.setattr(debug_session, "require_investigation_runtime", lambda *a, **k: None)
    yield eng, project
    with eng.begin() as c:
        c.execute(text("DELETE FROM investigations WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": project})


def test_an_investigation_queued_past_25_minutes_is_still_the_live_one(investigations):
    from boss_engine.services import debug_session

    eng, pid = investigations
    with eng.begin() as c:
        waiting = _investigation(c, pid, status="queued", ago=LONG_QUEUE, job_status="queued")
    with eng.begin() as c:
        out = debug_session.open_investigation(c, project_id=pid, question="again",
                                               actor="test")
        row = c.execute(text("SELECT status FROM investigations WHERE id = :i"),
                        {"i": waiting}).scalar()
    assert row == "queued"
    assert out.get("already_running") == waiting


def test_an_investigation_whose_job_ended_before_it_started_says_so(investigations):
    from boss_engine.services import debug_session

    eng, pid = investigations
    with eng.begin() as c:
        lost = _investigation(c, pid, status="queued", ago=LONG_QUEUE, job_status="failed")
        stale = _investigation(c, pid, status="running", ago=timedelta(minutes=40),
                               job_status="running")
    with eng.begin() as c:
        debug_session.open_investigation(c, project_id=pid, question="again", actor="test")
        rows = {r["id"]: r for r in c.execute(text(
            "SELECT id, status, error FROM investigations WHERE id = ANY(:i)"),
            {"i": [lost, stale]}).mappings()}
    assert rows[lost]["status"] == "abandoned"
    assert "ended before it started" in rows[lost]["error"]
    assert "start the investigation again" in rows[lost]["error"]
    assert rows[stale]["status"] == "abandoned"
    assert "stopped" in rows[stale]["error"]


def test_an_investigation_never_waits_in_the_queue_forever(investigations):
    from boss_engine.services import debug_session

    eng, pid = investigations
    with eng.begin() as c:
        stuck = _investigation(c, pid, status="queued", ago=timedelta(hours=25),
                               job_status="queued")
    with eng.begin() as c:
        out = debug_session.open_investigation(c, project_id=pid, question="again", actor="test")
        row = c.execute(text("SELECT status, error FROM investigations WHERE id = :i"),
                        {"i": stuck}).mappings().first()
    assert row["status"] == "abandoned" and "never started" in row["error"]
    assert out.get("already_running") != stuck


# ── step recovery's window ──────────────────────────────────────────────────────────────────

def test_recovery_time_does_not_count_its_repair_builds_queue(eng, project):  # noqa: F811
    pid = project
    caps = {"budget_usd_micros": 10**9, "max_seconds": 120 * 60}
    run_id = f"crun_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    opened = now_utc() - LONG_QUEUE - timedelta(minutes=30)     # 341 minutes ago
    try:
        with eng.begin() as c:
            # The repair build was created when the window opened, and waited 311 minutes for
            # a worker on the one-slot host; it has been running 30 minutes.
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, created_at) "
                           "VALUES (:r, :p, 'repair', :at)"), {"r": run_id, "p": pid, "at": opened})
            c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, created_at) "
                           "VALUES (:j, :p, 'code.task', CAST(:pl AS JSONB), 'queued', :at)"),
                      {"j": job_id, "p": pid, "pl": json.dumps({"run_id": run_id}), "at": opened})
            state = {**step_recovery.open_window({}), "started_at": opened.isoformat(),
                     "build_ids": [run_id]}
            # Still queued: none of the 341 minutes waiting is recovery time.
            ok, why = step_recovery.budget_left(c, state, caps)
            assert ok, why
            # Leased after 311 minutes: 30 minutes of recovery, inside the 120-minute cap.
            c.execute(text("UPDATE jobs SET status = 'running' WHERE id = :j"), {"j": job_id})
            c.execute(text("INSERT INTO events (id, project_id, event_type, actor, entity_type, "
                           "entity_id, data, created_at) VALUES (:e, :p, 'job.leased', 'w', "
                           "'job', :j, '{}'::jsonb, :at)"),
                      {"e": f"evt_{uuid.uuid4().hex[:8]}", "p": pid, "j": job_id,
                       "at": opened + LONG_QUEUE})
            assert 300 * 60 <= step_recovery.builds_queued_seconds(c, state) <= 312 * 60
            ok, why = step_recovery.budget_left(c, state, caps)
            assert ok, why
            # A window that has truly worked past its cap still stops, and says so.
            long_ago = {**state, "started_at": (opened - timedelta(minutes=200)).isoformat()}
            ok, why = step_recovery.budget_left(c, long_ago, caps)
            assert not ok and "recovering this step for" in why
            # A window opened after the build was created counts only the wait since it
            # opened, never more than the window's own age.
            later = {**state, "started_at": (opened + timedelta(minutes=300)).isoformat()}
            assert step_recovery.builds_queued_seconds(c, later) <= 12 * 60
            # A build whose lease is not on record (events pruned) and is not queued counts
            # no wait: an unknown wait is never subtracted.
            c.execute(text("DELETE FROM events WHERE entity_id = :j"), {"j": job_id})
            assert step_recovery.builds_queued_seconds(c, state) == 0.0
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM events WHERE entity_id = :j"), {"j": job_id})
            c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
            c.execute(text("DELETE FROM code_task_runs WHERE id = :r"), {"r": run_id})


def test_a_run_whose_authority_ended_mid_run_is_told_what_and_what_to_do():
    said = step_recovery.AUTHORITY_ENDED
    assert "ended" in said and "renew" in said
    assert step_recovery.authority_message(said)
    signature, pattern = step_recovery.cause_signature("deploy", {"why": said})
    assert pattern["pattern"] == "authority_ended"


# ── job leases ──────────────────────────────────────────────────────────────────────────────

def test_a_job_queued_for_hours_gets_a_full_lease_and_a_lapsed_lease_is_said(eng, project):  # noqa: F811
    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService, LeaseNotFoundError

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, created_at, run_after, priority) "
                       "VALUES (:j, :p, 'r3.long_queue_probe', '{}'::jsonb, :at, :at, 1)"),
                  {"j": job_id, "p": project, "at": now_utc() - LONG_QUEUE})
    svc = JobLeaseService(Settings())
    try:
        with eng.begin() as c:
            leased = svc.claim_job(c, "w_r3", ["r3.long_queue_probe"], 300)
        assert leased is not None and leased["id"] == job_id
        with eng.begin() as c:
            expires = c.execute(text("SELECT lease_expires_at FROM jobs WHERE id = :j"),
                                {"j": job_id}).scalar()
        assert expires - now_utc() > timedelta(minutes=4), "the lease counts from the lease"
        # The lease lapses mid-run (a frozen worker): heartbeat says so, plainly.
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' "
                           "WHERE id = :j"), {"j": job_id})
        with pytest.raises(LeaseNotFoundError, match="Lease has expired"):
            with eng.begin() as c:
                svc.heartbeat(c, "w_r3", job_id, leased["lease_token"], 300)
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM events WHERE entity_id = :j"), {"j": job_id})
            c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})


# ── sandbox tasks ───────────────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _sandbox(monkeypatch, statuses):
    """A MicroVMProvider whose sandbox-service answers `statuses(t_since_submit)`."""
    import httpx

    from boss_engine.services import sandbox_provider as sp

    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(sp, "ensure_microvm_execution_allowed", lambda *_: None)
    submitted = {}

    class Resp:
        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._b

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            submitted["at"] = clock.t
            return Resp({"sandbox_task_id": "sbx_1"})

        def get(self, *a, **k):
            return Resp({"status": statuses(clock.t - submitted["at"])})

        def delete(self, url, *a, **k):
            submitted["withdrawn"] = url

    monkeypatch.setattr(httpx, "Client", Client)
    provider = object.__new__(sp.MicroVMProvider)
    provider.settings = SimpleNamespace(sandbox_token="", sandbox_agent_timeout_seconds=1500,
                                        sandbox_queue_timeout_seconds=7200,
                                        sandbox_poll_interval_seconds=5)
    monkeypatch.setattr(provider, "_require_base", lambda: "http://sandbox.invalid", raising=False)
    monkeypatch.setattr(provider, "_submit_body", lambda *a, **k: {}, raising=False)
    return provider, clock, submitted


def test_a_sandbox_task_queued_past_its_timeout_still_gets_its_full_timeout(monkeypatch):
    queued_for = 2000                           # longer than the 1500s agent timeout, queued

    def statuses(since):
        return "queued" if since < queued_for else "running"

    provider, clock, submitted = _sandbox(monkeypatch, statuses)
    with pytest.raises(RuntimeError) as err:
        provider.run_code_task({})
    ran_for = clock.t - submitted["at"] - queued_for
    assert ran_for >= 1500 - 10, "the timeout counted the queue"
    assert "within 1500s of starting" in str(err.value)


def test_a_sandbox_task_that_never_starts_says_it_never_started(monkeypatch):
    provider, _clock, _ = _sandbox(monkeypatch, lambda since: "queued")
    with pytest.raises(RuntimeError) as err:
        provider.run_code_task({})
    said = str(err.value)
    # 7200 asked, capped so queue + run fit the guest's handle (4500 - 1500 - 300).
    assert "never started" in said and "queued for 2700s" in said
    assert "BOSS_SANDBOX_QUEUE_TIMEOUT_SECONDS" in said and "retry the step" in said
    assert _["withdrawn"].endswith("/v1/sandbox/tasks/sbx_1"), "withdrawn, never a ghost run"


# ── tokens and handles that lapse mid-run ───────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["token_expired", "handle_expired", "token_revoked",
                                  "handle_revoked"])
def test_every_lapsed_credential_says_what_and_what_to_do(code):
    said = lapsed.say(code)
    assert said.startswith(code + ": ")
    assert "retry the step" in said or "start the work again" in said


def test_the_mcp_gateway_says_its_token_lapsed(monkeypatch):
    from boss_engine.services import mcp_gateway as gw

    gateway = gw.McpGateway()
    token, _ = gateway.issue("p_r3", {}, ttl=30)
    clock = time.time() + 31
    monkeypatch.setattr(gw.time, "time", lambda: clock)
    status, reason, _ = gateway.authorise(token)
    assert (status, reason) == (401, "token_expired")
    body = json.loads(gw._refusal(status, reason))
    assert "token_expired: " in body["error"]["message"]
    assert "BOSS_CODE_AGENT_TIMEOUT_SECONDS" in body["error"]["message"]


def test_the_model_proxy_says_its_token_lapsed():
    from boss_engine.services import model_proxy as mp

    body = json.loads(mp._refusal("anthropic", 401, "token_expired"))
    assert body["error"]["message"].startswith("boss model proxy: token_expired: ")
    assert "retry the step" in body["error"]["message"]


def test_a_model_or_tool_handle_that_lapsed_mid_run_says_so():
    import datetime as dt

    from boss_engine.services import mcp_gateway as gw

    from .test_p8b_mcp_gateway import HANDLE, FakeConn, _caller, _gateway, _row

    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    with pytest.raises(gw.Refused) as refused:
        _gateway().host_lane.resolve(FakeConn(_row(expires_at=past)), _caller(), HANDLE)
    assert refused.value.reason == "handle_expired"
    assert "reached the end of its lifetime" in str(refused.value)


# ── model-lane slot waits: the implement budget and a review seat's deadline (BOSSAPP-1450) ──

class _OneSlotLane:
    """A provider lane with one slot that another build holds for `wait` seconds."""

    def __init__(self, clock, wait):
        self.clock, self.wait = clock, wait

    def slot(self, provider):
        import contextlib

        @contextlib.contextmanager
        def held():
            self.clock["t"] += self.wait          # queued behind the other build
            yield SimpleNamespace(waited_seconds=self.wait)
        return held()

    def report_ok(self, provider):
        pass

    def report_rate_limited(self, provider):
        pass

    def lane_state(self, provider):
        return {"in_flight": 1, "limit": 1}


def test_a_model_slot_wait_is_not_spent_from_the_implement_budget(monkeypatch):
    from boss_engine.services import code_agent, stage_clock
    from boss_engine.services.llm_telemetry import get_llm_context, set_llm_context

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    run_id = f"crun_{uuid.uuid4().hex[:8]}"
    budget = 25 * 60
    began = clock["t"]
    seen = {}

    class Inner:
        engine = "claude_cli"

        def run_role(self, *a, **k):
            # What a writing session reads when it starts: its stage's deadline and the
            # timeout cut to it.
            seen["deadline"] = code_agent._stage_deadline()
            seen["timeout"] = code_agent._cap_to_deadline(3600, read_only=False)
            return {"summary": "ok"}

    before = dict(get_llm_context())
    try:
        set_llm_context(run_id=run_id, stage_deadline=began + budget,
                        stage_deadline_since=began)
        agent = code_agent.PacedCodeAgent(Inner(), _OneSlotLane(clock, LONG))
        agent.run_role("write it", "/tmp")
        # 311 minutes in the lane's queue, then the session starts with its 25 minutes whole.
        assert seen["deadline"] >= began + budget + LONG - 1
        assert seen["timeout"] >= budget - 1
        assert stage_clock.queued_between(run_id, began) >= LONG - 1
        assert not stage_clock.is_parked(run_id), "a slot wait is not a park"
        # A stage that truly WORKED past its budget is still out of time.
        clock["t"] += budget + 60
        assert time.time() >= code_agent._stage_deadline()
    finally:
        stage_clock.forget(run_id)
        set_llm_context(**{k: None for k in get_llm_context()})
        set_llm_context(**before)


def test_a_review_seat_is_not_charged_for_its_model_slot_wait():
    from boss_engine.services import review_seats, stage_clock

    clock = {"t": 0.0}
    deadline = 900

    def queued_then_quick():
        with stage_clock.waiting_for_a_slot(None):
            clock["t"] += 2 * deadline             # the one slot is another seat's
        clock["t"] += 30                           # 30 seconds of its own work
        return "reviewed"

    out = review_seats.run_seats([("review_code", queued_then_quick)], max_workers=1,
                                 seat_deadline_s=deadline, owner_id=lambda lb: f"r:{lb}",
                                 poll_s=0.01, clock=lambda: clock["t"])
    assert out.timed_out == [] and out.results == {"review_code": "reviewed"}
    assert out.slot_waits["review_code"] >= 2 * deadline


def test_a_review_seat_that_works_past_its_deadline_still_times_out():
    import threading

    from boss_engine.services import review_seats

    clock = {"t": 0.0}
    release = threading.Event()

    def slow():
        clock["t"] += 901                          # working, not waiting
        release.wait(2.0)
        return "late"

    try:
        out = review_seats.run_seats([("review_code", slow)], max_workers=1,
                                     seat_deadline_s=900, owner_id=lambda lb: f"r:{lb}",
                                     poll_s=0.01, clock=lambda: clock["t"])
    finally:
        release.set()
    assert out.timed_out == ["review_code"]


# ── the MCP gateway token's lifetime starts with the session (BOSSAPP-1451) ────────────────

def test_an_mcp_token_queued_past_its_lifetime_is_whole_when_the_session_starts(monkeypatch):
    from boss_engine.services import mcp_gateway as gw

    gateway = gw.McpGateway()
    now = {"t": time.time()}
    monkeypatch.setattr(gw.time, "time", lambda: now["t"])
    token, _ = gateway.issue("p_r3", {}, ttl=75 * 60)
    now["t"] += LONG                                # the build sat in the queue 311 minutes
    assert gateway.authorise(token)[1] == "token_expired"
    assert gateway.session_started(token, "crun_r3") is True
    status, reason, grant = gateway.authorise(token)
    assert (status, reason) == (200, "")
    assert grant.expires_at - now["t"] >= 75 * 60 - 1 and grant.run_id == "crun_r3"
    # A second session later in the run renews it again; a revoked token is never revived.
    now["t"] += 60 * 60
    gateway.session_started(token, "crun_r3")
    assert gateway.authorise(token)[0] == 200
    gateway.revoke(token)
    assert gateway.session_started(token, "crun_r3") is False
    assert gateway.authorise(token)[1] == "token_revoked"
    assert "from when the session started" in lapsed.say("token_expired")


def test_the_harness_renews_its_token_when_a_session_starts(monkeypatch):
    from boss_engine.services import code_task_mcp, mcp_gateway as gw

    gateway = gw.McpGateway()
    monkeypatch.setattr(gw, "get", lambda: gateway)
    now = {"t": time.time()}
    monkeypatch.setattr(gw.time, "time", lambda: now["t"])
    token, _ = gateway.issue("p_r3", {}, ttl=60)
    now["t"] += 600
    code_task_mcp.HarnessMcp(path="", servers=[], gateway_token=token).argv()
    assert gateway.authorise(token)[0] == 200


# ── a build's budget hold lives as long as its job does (BOSSAPP-1452) ──────────────────────

def test_a_long_stage_keeps_its_hold_on_every_heartbeat(eng, project):  # noqa: F811
    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    held, released = f"bres_{uuid.uuid4().hex[:8]}", f"bres_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, created_at, run_after, priority) "
                       "VALUES (:j, :p, 'r3.long_stage_probe', '{}'::jsonb, :at, :at, 1)"),
                  {"j": job_id, "p": project, "at": now_utc() - LONG_QUEUE})
    svc = JobLeaseService(Settings())
    try:
        with eng.begin() as c:
            leased = svc.claim_job(c, "w_r3", ["r3.long_stage_probe"], 300)
            # 44 minutes into one stage: the hold made for this job has a minute left, and a
            # hold the build released on purpose is not to be revived.
            for rid, status in ((held, "active"), (released, "released")):
                c.execute(text(
                    "INSERT INTO budget_reservations (id, project_id, job_id, category, "
                    "amount_usd_micros, status, expires_at) VALUES (:r, :p, :j, 'code_task', "
                    "1000000, :s, now() + interval '1 minute')"),
                    {"r": rid, "p": project, "j": job_id, "s": status})
        with eng.begin() as c:
            svc.heartbeat(c, "w_r3", job_id, leased["lease_token"], 300)
        with eng.begin() as c:
            rows = dict(c.execute(text(
                "SELECT id, expires_at - now() FROM budget_reservations WHERE id = ANY(:i)"),
                {"i": [held, released]}).all())
        assert rows[held] > timedelta(minutes=40), "the heartbeat renewed the hold"
        assert rows[released] < timedelta(minutes=2), "a released hold is never revived"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM budget_reservations WHERE job_id = :j"), {"j": job_id})
            c.execute(text("DELETE FROM events WHERE entity_id = :j"), {"j": job_id})
            c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})


# ── the CI wait counts from when a check starts running (BOSSAPP-1453) ──────────────────────

class _Ci:
    """GitHub checks for one commit: `script(t)` -> the check runs at `t` seconds."""

    def __init__(self, script, clock):
        self.script, self.clock = script, clock

    def check_runs(self, sha):
        return self.script(self.clock["t"])

    def statuses(self, sha):
        return []

    def has_workflows(self, sha):
        return True


def _check(status, conclusion=None):
    return [{"id": 1, "name": "e2e", "status": status, "conclusion": conclusion,
             "html_url": "https://github.test/acme/app/runs/1"}]


def _wait(script, timeout=1800):
    from boss_engine.services import ci_gate

    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    out = ci_gate.wait_for_ci(_Ci(script, clock), "a" * 40, timeout_seconds=timeout,
                              sleep=sleep, clock=lambda: clock["t"], repository="acme/app")
    return out, clock["t"]


def test_ci_queued_for_a_runner_past_the_timeout_still_gets_its_full_run():
    # Queued 311 minutes on a one-runner organisation, then 20 minutes running, then green.
    def script(t):
        if t < LONG:
            return _check("queued")
        return _check("in_progress") if t < LONG + 20 * 60 else _check("completed", "success")

    out, _ = _wait(script)
    assert out.state == "passed", out.summary


def test_ci_that_runs_past_the_timeout_says_it_counted_from_the_start_of_the_run():
    def script(t):
        return _check("queued") if t < 3600 else _check("in_progress")

    out, at = _wait(script)
    assert out.state == "timed_out"
    assert out.summary.startswith("CI did not finish in 30 min: 'e2e' still running")
    assert "count from when the first check started running" in out.summary
    assert re.search(r"the \d+ min it waited for a GitHub runner before that are not "
                     r"counted", out.summary), out.summary
    assert at >= 3600 + 1800 - 130


def test_ci_that_never_leaves_the_runner_queue_says_it_never_started():
    from boss_engine.services import ci_gate

    out, at = _wait(lambda t: _check("queued"))
    assert out.state == "timed_out" and at >= ci_gate.QUEUE_TIMEOUT_SECONDS
    assert out.summary.startswith("CI never started:")
    assert "Settings › Actions › Runners" in out.summary and "Re-check" in out.summary


def test_a_recorded_ci_wait_starts_its_clock_when_a_check_runs(eng, project):  # noqa: F811
    from boss_engine.services import ci_gate

    clock = {"t": 0.0}
    phase = {"status": "queued"}
    reader = _Ci(lambda t: _check(phase["status"]), clock)
    try:
        with eng.begin() as c:
            wait = ci_gate.start_wait(c, project_id=project, repository="acme/app",
                                      head_sha="b" * 40, timeout_seconds=1800, has_ci=True)
            c.execute(text("UPDATE ci_waits SET created_at = now() - interval '311 minutes', "
                           "deadline_at = now() - interval '281 minutes' WHERE id = :i"),
                      {"i": wait["id"]})
        with eng.begin() as c:   # queued for 311 minutes: still waiting, never "did not finish"
            got = ci_gate.poll_wait(c, wait["id"], reader, force=True)
        assert got["status"] == "waiting" and got["running_since"] is None
        phase["status"] = "in_progress"
        with eng.begin() as c:   # a runner took it: the 30 minutes start now
            got = ci_gate.poll_wait(c, wait["id"], reader, force=True)
        assert got["status"] == "waiting" and got["running_since"]
        with eng.begin() as c:
            c.execute(text("UPDATE ci_waits SET running_since = now() - interval '31 minutes' "
                           "WHERE id = :i"), {"i": wait["id"]})
            got = ci_gate.poll_wait(c, wait["id"], reader, force=True)
        assert got["status"] == "timed_out"
        assert "count from when the first check started running" in got["summary"]
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM ci_waits WHERE project_id = :p"), {"p": project})


# ── the Task Shepherd's deadline does not count its remedies' queue (BOSSAPP-1454) ──────────

def test_the_shepherds_deadline_does_not_count_its_remedies_queue_time(eng, project):  # noqa: F811
    from boss_engine.services import shepherd_service, task_shepherd

    failed, retry = f"crun_{uuid.uuid4().hex[:8]}", f"crun_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    began = now_utc() - LONG_QUEUE - timedelta(minutes=30)     # 341 minutes ago
    try:
        with eng.begin() as c:
            c.execute(text("INSERT INTO shepherd_reasoning (id, project_id, task_ref, step_no, "
                           "observe, created_at) VALUES (:i, :p, :t, 1, 'failed', :at)"),
                      {"i": f"shr_{uuid.uuid4().hex[:8]}", "p": project, "t": failed,
                       "at": began})
            # Its first remedy, a retry, waited 311 minutes for the one slot, then ran 30.
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, created_at) "
                           "VALUES (:r, :p, 'retry', :at)"), {"r": retry, "p": project,
                                                              "at": began})
            c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, created_at) "
                           "VALUES (:j, :p, 'code.task', CAST(:pl AS JSONB), 'running', :at)"),
                      {"j": job_id, "p": project, "pl": json.dumps({"run_id": retry}),
                       "at": began})
            for entity, kind, data, at in (
                    (job_id, "job.leased", {}, began + LONG_QUEUE),
                    (failed, "shepherd.acted", {"successor_run": retry, "outcome": "started"},
                     began)):
                c.execute(text("INSERT INTO events (id, project_id, event_type, actor, "
                               "entity_type, entity_id, data, created_at) VALUES (:e, :p, :k, "
                               "'shepherd', 'x', :i, CAST(:d AS JSONB), :at)"),
                          {"e": f"evt_{uuid.uuid4().hex[:8]}", "p": project, "k": kind,
                           "i": entity, "d": json.dumps(data), "at": at})
        with eng.begin() as c:
            minutes = shepherd_service._shepherd_minutes(c, failed)
        assert 25 <= minutes <= 40, minutes
        state = task_shepherd.ShepherdState(task_ref=failed, budget_usd=5.0,
                                            shepherd_minutes=minutes)
        assert task_shepherd.next_step(state, 1).stopped_because != "deadline"
        # A shepherd that truly worked past its deadline stops, and says what it counted.
        state.shepherd_minutes = state.deadline_minutes + 1
        said = task_shepherd.next_step(state, 1)
        assert said.stopped_because == "deadline"
        assert "not counted" in said.reason and "this run's card" in said.reason
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM events WHERE entity_id = ANY(:i)"),
                      {"i": [failed, job_id]})
            c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
            c.execute(text("DELETE FROM code_task_runs WHERE id = :r"), {"r": retry})
            c.execute(text("DELETE FROM shepherd_reasoning WHERE task_ref = :t"), {"t": failed})


def test_a_build_blocked_on_ci_runners_stops_waiting_within_its_own_bound(eng, project):  # noqa: F811
    """A build that waits for CI holds a worker; queued checks end it in an hour, in words."""
    from boss_engine.services import ci_gate

    reader = _Ci(lambda t: _check("queued"), {"t": 0.0})
    try:
        with eng.begin() as c:
            wait = ci_gate.start_wait(c, project_id=project, repository="acme/app",
                                      head_sha="c" * 40, timeout_seconds=1800, has_ci=True)
            c.execute(text("UPDATE ci_waits SET created_at = now() - interval '61 minutes', "
                           "deadline_at = now() - interval '31 minutes' WHERE id = :i"),
                      {"i": wait["id"]})
        got = ci_gate.run_blocking(eng.begin, wait["id"], reader, sleep=lambda s: None)
        assert got["status"] == "timed_out" and got["summary"].startswith("CI never started")
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM ci_waits WHERE project_id = :p"), {"p": project})
