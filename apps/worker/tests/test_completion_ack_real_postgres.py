"""BOSS-472: controlled acknowledgment ordering against owned real PostgreSQL.

Only the scheduling clock and control-plane transport are instrumented. Claim,
heartbeat, competing reclaim and completion use the real durable lease service.
No model or provider is called. Opt in explicitly; only owned Docker data is removed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text

from apps.worker.boss_worker import runner
from apps.worker.boss_worker.control_plane import LeasedJob
from boss_engine.config import Settings
from boss_engine.services import jobs as job_service

pytestmark = pytest.mark.skipif(os.environ.get("BOSS_RUN_WORKER_ACK_TEST") != "1",
                                reason="requires an owned disposable Docker Postgres")
REPO = Path(__file__).resolve().parents[3]


def docker(*args, input=None):
    return subprocess.run(["docker", *args], input=input, text=True, capture_output=True,
                          check=True, timeout=90).stdout.strip()


@pytest.fixture(scope="module")
def database():
    docker("image", "inspect", "postgres:16-alpine")
    name = "boss-worker-ack-" + uuid4().hex[:10]
    volume = name + "-data"
    docker("volume", "create", "--label", "boss.qualification=" + name, volume)
    engine = None
    try:
        docker("run", "-d", "--name", name, "--pull", "never", "--label", "boss.qualification=" + name,
               "-e", "POSTGRES_PASSWORD=owned-fixture", "-e", "POSTGRES_USER=boss",
               "-e", "POSTGRES_DB=boss", "-p", "127.0.0.1::5432", "-v", volume + ":/var/lib/postgresql/data",
               "postgres:16-alpine")
        for _ in range(100):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "boss"], capture_output=True).returncode == 0:
                break
            threading.Event().wait(.1)  # service startup only; ordering tests use barriers
        else:
            pytest.fail("owned PostgreSQL did not become ready")
        docker("exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "boss", "-d", "boss",
               input=(REPO / "db/schema.sql").read_text())
        port = docker("port", name, "5432").rsplit(":", 1)[1]
        engine = create_engine(f"postgresql+psycopg://boss:owned-fixture@127.0.0.1:{port}/boss",
                               connect_args={"options": "-c statement_timeout=10000"})
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        docker("rm", "-f", name)
        docker("volume", "rm", volume)
        print("owned_ack_resources_removed", name, volume)


@pytest.fixture()
def authority(database, monkeypatch):
    engine = database
    project = "ack_" + uuid4().hex
    kind = "owned.ack." + project
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(job_service, "now_utc", lambda: clock[0])
    monkeypatch.setattr(runner, "_tick_ai_sre_overwatch", lambda *_: None)
    runner.SHUTDOWN.clear()
    with engine.begin() as db:
        db.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": project})
        db.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": project})
        db.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"), {"p": project})
        db.execute(text("INSERT INTO jobs(id,project_id,kind,run_after) VALUES(:p,:p,:k,:now)"),
                   {"p": project, "k": kind, "now": clock[0]})
    service = job_service.JobLeaseService(Settings(_env_file=None))
    settings = SimpleNamespace(worker_id="owned-worker", lease_ttl_seconds=30,
                               job_max_runtime_seconds=120, poll_interval_seconds=1)
    yield engine, project, kind, clock, service, settings
    runner.SHUTDOWN.clear()
    assert not any(t.name == "lease-hb-" + project and t.is_alive() for t in threading.enumerate())


def conflict(error):
    request = httpx.Request("POST", "http://owned.test/lease")
    return httpx.HTTPStatusError("409 Conflict", request=request,
                                 response=httpx.Response(409, request=request), )


class DurableClient:
    def __init__(self, authority):
        self.engine, self.project, self.kind, self.clock, self.service, self.settings = authority
        self.job = None
        self.renewed = 0
        self.complete_hook = lambda: None
        self.after_renew = lambda: None

    def poll(self, worker, capabilities, ttl):
        with self.engine.begin() as db:
            row = self.service.claim_job(db, worker, [self.kind], ttl)
        self.job = LeasedJob(**row)
        return self.job

    def heartbeat(self, worker, job, token, ttl, handing_back=False):
        try:
            with self.engine.begin() as db:
                result = self.service.heartbeat(db, worker, job, token, ttl, handing_back=handing_back)
        except job_service.LeaseNotFoundError as exc:
            raise conflict(exc) from exc
        if threading.current_thread().name.startswith("lease-hb-"):
            self.renewed += 1
            self.after_renew()
        return result

    def complete(self, worker, job, token, result):
        self.complete_hook()
        try:
            with self.engine.begin() as db:
                return self.service.complete(db, worker, job, token, result)
        except job_service.LeaseNotFoundError as exc:
            raise conflict(exc) from exc


def test_ack_wait_keeps_real_lease_unclaimable(authority, monkeypatch):
    engine, project, kind, clock, service, settings = authority
    client = DurableClient(authority)
    ack_entered, renewal_examined = threading.Event(), threading.Event()
    competitor = []
    calls = []

    def controlled_wait(stop, _seconds, **_kwargs):
        assert ack_entered.wait(5), "completion did not enter"
        if stop.is_set():
            renewal_examined.set()
            return True
        if not client.renewed:
            return False
        return stop.wait(5)

    def delayed_ack():
        clock[0] += timedelta(seconds=29)
        ack_entered.set()
        assert renewal_examined.wait(5), "renewal thread did not respond"
        clock[0] += timedelta(seconds=2)  # beyond the original lease, inside a renewed lease
        with engine.begin() as db:
            competitor.append(service.claim_job(db, "owned-competitor", [kind], 30))

    client.after_renew = renewal_examined.set
    client.complete_hook = delayed_ack
    monkeypatch.setattr(runner, "_wait_in_slices", controlled_wait)
    monkeypatch.setattr(runner, "execute_job", lambda *_: calls.append("owned work finished") or {"ok": True})
    assert runner.run_worker_cycle(client, settings)
    assert competitor == [None], "job became reclaimable while its completion acknowledgment waited"
    assert client.renewed == 1 and len(calls) == 1
    with engine.begin() as db:
        row = db.execute(text("SELECT status,attempt,lease_owner FROM jobs WHERE id=:p"), {"p": project}).mappings().one()
        assert dict(row) == {"status": "succeeded", "attempt": 1, "lease_owner": None}
        assert db.execute(text("SELECT count(*) FROM events WHERE entity_id=:p AND event_type='job.succeeded'"),
                          {"p": project}).scalar_one() == 1


def test_committed_completion_with_lost_response_stays_terminal_once(authority, monkeypatch):
    engine, project, kind, _, service, settings = authority
    client = DurableClient(authority)
    committed, heartbeat_seen = threading.Event(), threading.Event()
    executed = []
    heartbeat = client.heartbeat

    def observe_heartbeat(*args, **kwargs):
        try:
            return heartbeat(*args, **kwargs)
        finally:
            if threading.current_thread().name.startswith("lease-hb-"):
                heartbeat_seen.set()

    def controlled_wait(stop, _seconds, **_kwargs):
        assert committed.wait(5)
        return stop.is_set()

    def lose_response(worker, job, token, result):
        with engine.begin() as db:
            service.complete(db, worker, job, token, result)
        committed.set()
        assert heartbeat_seen.wait(5)
        raise httpx.ReadTimeout("owned lost acknowledgment")

    client.heartbeat = observe_heartbeat
    client.complete = lose_response
    monkeypatch.setattr(runner, "_wait_in_slices", controlled_wait)
    monkeypatch.setattr(runner, "execute_job", lambda *_: executed.append(1) or {"ok": True})
    with pytest.raises(httpx.ReadTimeout, match="owned lost"):
        runner.run_worker_cycle(client, settings)
    assert len(executed) == 1 and client.renewed == 0
    with engine.begin() as db:
        assert service.claim_job(db, "owned-competitor", [kind], 30) is None
        assert db.execute(text("SELECT status FROM jobs WHERE id=:p"), {"p": project}).scalar_one() == "succeeded"
        assert db.execute(text("SELECT count(*) FROM events WHERE entity_id=:p AND event_type='job.succeeded'"),
                          {"p": project}).scalar_one() == 1
        with pytest.raises(job_service.LeaseNotFoundError):
            service.complete(db, settings.worker_id, project, client.job.lease_token, {"ok": True})


@pytest.mark.parametrize("transition", ["other_owner", "rotated_token", "cancelled"])
def test_pending_completion_cannot_resurrect_lost_or_cancelled_authority(authority, monkeypatch, transition):
    engine, project, kind, clock, service, settings = authority
    client = DurableClient(authority)
    changed = []
    monkeypatch.setattr(runner, "_wait_in_slices", lambda stop, *_a, **_k: stop.wait(5))
    monkeypatch.setattr(runner, "execute_job", lambda *_: {"ok": True})

    def transfer():
        with engine.begin() as db:
            if transition == "cancelled":
                service.cancel_queued_job(db, project, project_id=project, force=True,
                                          actor="owned-operator", reason="owned cancellation test")
            else:
                clock[0] += timedelta(seconds=31)
                changed.append(service.claim_job(db, settings.worker_id if transition == "rotated_token"
                                                  else "owned-competitor", [kind], 30))

    client.complete_hook = transfer
    assert runner.run_worker_cycle(client, settings)
    with engine.begin() as db:
        row = db.execute(text("SELECT status,lease_owner,lease_token_hash FROM jobs WHERE id=:p"),
                         {"p": project}).mappings().one()
        if transition == "cancelled":
            assert row["status"] == "cancelled"
        else:
            assert row["status"] == "leased"
            assert row["lease_token_hash"] == service._hash_token(changed[0]["lease_token"])
            assert changed[0]["lease_token"] != client.job.lease_token
        assert db.execute(text("SELECT count(*) FROM events WHERE entity_id=:p AND event_type='job.succeeded'"),
                          {"p": project}).scalar_one() == 0


@pytest.mark.parametrize("limit", ["execution", "acknowledgment"])
def test_ack_renewal_obeys_execution_and_separate_settlement_ceiling(authority, monkeypatch, caplog, limit):
    engine, project, kind, clock, service, settings = authority
    client = DurableClient(authority)
    ack_entered, renewal_ready = threading.Event(), threading.Event()
    monotonic = [0.0]
    if limit == "execution":
        settings.job_max_runtime_seconds = 1
    else:
        settings.job_max_runtime_seconds = 0  # acknowledgment still has its independent bound
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: monotonic[0]))

    def controlled_wait(stop, _seconds, **_kwargs):
        renewal_ready.set()
        assert ack_entered.wait(5)
        return stop.is_set()

    def delay_past_ceiling():
        monotonic[0] = 2.0 if limit == "execution" else 31.0
        ack_entered.set()
        thread = next(t for t in threading.enumerate() if t.name == "lease-hb-" + project)
        thread.join(5)
        assert not thread.is_alive()
        raise httpx.ReadTimeout("owned bounded acknowledgment failure")

    client.complete_hook = delay_past_ceiling
    monkeypatch.setattr(runner, "_wait_in_slices", controlled_wait)
    def finish_execution(*_args):
        assert renewal_ready.wait(5)
        return {"ok": True}
    monkeypatch.setattr(runner, "execute_job", finish_execution)
    with pytest.raises(httpx.ReadTimeout):
        runner.run_worker_cycle(client, settings)
    assert client.renewed == 0
    assert ("runtime_exceeded" if limit == "execution" else "ack_deadline") in caplog.text
    clock[0] += timedelta(seconds=31)
    with engine.begin() as db:
        reclaimed = service.claim_job(db, "owned-competitor", [kind], 30)
        assert reclaimed["id"] == project and reclaimed["attempt"] == 2


def test_shutdown_during_ack_hands_back_without_consuming_an_attempt(authority, monkeypatch):
    engine, project, kind, clock, service, settings = authority
    client = DurableClient(authority)
    ack_entered = threading.Event()

    def controlled_wait(stop, _seconds, **_kwargs):
        assert ack_entered.wait(5)
        return stop.is_set()

    def shutdown_ack():
        runner.SHUTDOWN.set()
        ack_entered.set()
        thread = next(t for t in threading.enumerate() if t.name == "lease-hb-" + project)
        thread.join(5)
        assert not thread.is_alive()
        raise httpx.ReadTimeout("owned shutdown lost acknowledgment")

    client.complete_hook = shutdown_ack
    monkeypatch.setattr(runner, "_wait_in_slices", controlled_wait)
    monkeypatch.setattr(runner, "execute_job", lambda *_: {"ok": True})
    with pytest.raises(httpx.ReadTimeout):
        runner.run_worker_cycle(client, settings)
    with engine.begin() as db:
        row = db.execute(text("SELECT attempt,lease_expires_at FROM jobs WHERE id=:p"), {"p": project}).mappings().one()
        assert row["attempt"] == 0
        assert row["lease_expires_at"] == clock[0] + timedelta(seconds=runner.SHUTDOWN_LEASE_TTL_SECONDS)
        clock[0] += timedelta(seconds=runner.SHUTDOWN_LEASE_TTL_SECONDS + 1)
        reclaimed = service.claim_job(db, "owned-competitor", [kind], 30)
        assert reclaimed["id"] == project and reclaimed["attempt"] == 1
