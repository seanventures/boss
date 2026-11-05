"""A leased job survives the worker process that held it.

The engine already leases jobs with a TTL and a heartbeat (JobLeaseService). The lease is a
row, not a handle inside the worker. Stopping that process — the way a crash or a restart
does, with no chance to run a finally — must leave the job leased until the TTL, and the
next worker must reclaim that same job and finish it. A second lease system is not part of
this. No model or provider is called.

Set BOSS_RUN_RESTART_DATABASE_URL to an owned Postgres whose database name contains "test".
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from apps.worker.boss_worker import runner
from apps.worker.boss_worker.control_plane import LeasedJob
from boss_engine.config import Settings
from boss_engine.services.jobs import JobLeaseService, LeaseNotFoundError

pytestmark = pytest.mark.skipif(
    "test" not in os.environ.get("BOSS_RUN_RESTART_DATABASE_URL", ""),
    reason="requires BOSS_RUN_RESTART_DATABASE_URL naming an owned test database",
)
ROOT = Path(__file__).resolve().parents[3]
HOLDER = "worker-that-stops"
CONTINUER = "worker-that-continues"
TTL_SECONDS = 4


def _engine():
    return create_engine(
        os.environ["BOSS_RUN_RESTART_DATABASE_URL"],
        connect_args={"options": "-c statement_timeout=15000"},
    )


class _LeaseClient:
    """The worker's control plane, talking to the real lease service."""

    def __init__(self, engine, service):
        self.engine = engine
        self.service = service

    def poll(self, worker_id, capabilities, lease_ttl_seconds):
        with self.engine.begin() as db:
            row = self.service.claim_job(db, worker_id, list(capabilities), lease_ttl_seconds)
        if not row:
            print("IDLE", flush=True)
            return None
        return LeasedJob(**row)

    def heartbeat(self, worker_id, job_id, lease_token, lease_ttl_seconds, handing_back=False):
        with self.engine.begin() as db:
            return self.service.heartbeat(
                db, worker_id, job_id, lease_token, lease_ttl_seconds, handing_back=handing_back,
            )

    def complete(self, worker_id, job_id, lease_token, result):
        with self.engine.begin() as db:
            return self.service.complete(db, worker_id, job_id, lease_token, result)

    def fail(self, worker_id, job_id, lease_token, error, retryable, retry_delay_seconds):
        with self.engine.begin() as db:
            return self.service.fail(
                db, worker_id, job_id, lease_token, error, retryable, retry_delay_seconds,
            )


def hold_until_killed() -> int:
    """Claim one job through the real worker cycle and sit in it until this process is killed."""
    kind = os.environ["BOSS_RESTART_KIND"]
    engine = _engine()
    service = JobLeaseService(Settings(_env_file=None))
    client = _LeaseClient(engine, service)
    settings = SimpleNamespace(
        worker_id=HOLDER,
        lease_ttl_seconds=TTL_SECONDS,
        job_max_runtime_seconds=120,
        poll_interval_seconds=1,
    )
    runner._tick_ai_sre_overwatch = lambda *_args, **_kwargs: None
    runner.capabilities = lambda: [kind]
    runner.SHUTDOWN.clear()

    def hold(job, _client):
        print(f"HOLDING {job.id} {job.lease_token}", flush=True)
        time.sleep(3600)
        return {"held": True}

    runner.execute_job = hold
    try:
        runner.run_worker_cycle(client, settings)
    finally:
        engine.dispose()
    print("CYCLE_RETURNED", flush=True)
    return 0


def _seed(engine, project_id: str, job_id: str, kind: str) -> None:
    with engine.begin() as db:
        db.execute(text("INSERT INTO orgs (id, name) VALUES (:p, :p)"), {"p": project_id})
        db.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:p, :p, :p)"),
            {"p": project_id},
        )
        db.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:p, :p, :p, :p)"
            ),
            {"p": project_id},
        )
        db.execute(
            text(
                "INSERT INTO jobs (id, project_id, kind, payload, run_after) "
                "VALUES (:id, :p, :k, '{}'::jsonb, now())"
            ),
            {"id": job_id, "p": project_id, "k": kind},
        )


def _job(engine, job_id: str):
    with engine.connect() as db:
        return db.execute(
            text(
                "SELECT status, lease_owner, lease_expires_at, attempt "
                "FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).mappings().one()


def _read_holding(proc: subprocess.Popen, err_path: Path, timeout: float) -> tuple[str, str]:
    found: list[str] = []

    def read() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if raw.startswith("HOLDING "):
                found.append(raw.strip())
                return

    thread = threading.Thread(target=read)
    thread.start()
    thread.join(timeout)
    if not found:
        err = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
        raise AssertionError(
            f"worker never held a lease (exit {proc.poll()}): {err[-2000:]}"
        )
    _holding, job_id, token = found[0].split(" ", 2)
    return job_id, token


def test_a_leased_job_stays_leased_then_another_worker_continues_it(tmp_path):
    """SIGKILL the worker that holds the lease. The job stays leased, then is reclaimed
    and completed by a different worker once the TTL passes. Nothing updates the expiry
    by hand: the clock the lease was written with is the clock that frees it."""
    project_id = "restart_" + uuid4().hex[:12]
    job_id = "job_" + uuid4().hex[:12]
    kind = "restart.hold." + uuid4().hex[:12]
    engine = _engine()
    err_path = tmp_path / "worker.err"
    proc = None
    try:
        _seed(engine, project_id, job_id, kind)
        env = os.environ.copy()
        env["BOSS_RUN_RESTART_HOLDER"] = "1"
        env["BOSS_RESTART_KIND"] = kind
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "engine"), str(ROOT / "apps" / "worker"),
             env.get("PYTHONPATH", "")]
        )
        env.setdefault("BOSS_AUTHZ_MODE", "shadow")
        env.setdefault(
            "BOSS_IDENTITY_SERVER_KEY",
            "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=",
        )
        with err_path.open("w") as err:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                env=env,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
            )
        held_id, token = _read_holding(proc, err_path, 30)
        assert held_id == job_id
        held = _job(engine, job_id)
        assert held["status"] in {"leased", "running"}
        assert held["lease_owner"] == HOLDER
        assert held["attempt"] == 1
        assert held["lease_expires_at"] is not None

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)

        # The process is gone. The lease is not: it was never inside the process.
        still = _job(engine, job_id)
        assert still["status"] in {"leased", "running"}
        assert still["lease_owner"] == HOLDER
        assert still["attempt"] == 1
        assert still["lease_expires_at"] == held["lease_expires_at"]

        remaining = still["lease_expires_at"].timestamp() - time.time()
        if remaining > 0:
            time.sleep(remaining + 0.3)

        service = JobLeaseService(Settings(_env_file=None))
        with engine.begin() as db:
            reclaimed = service.claim_job(db, CONTINUER, [kind], 60)
        assert reclaimed is not None
        assert reclaimed["id"] == job_id
        assert reclaimed["attempt"] == 2
        assert reclaimed["lease_token"] != token

        with engine.begin() as db:
            with pytest.raises(LeaseNotFoundError):
                service.heartbeat(db, HOLDER, job_id, token, 60)
            finished = service.complete(db, CONTINUER, job_id, reclaimed["lease_token"], {"ok": True})
        assert finished["status"] == "succeeded"
        done = _job(engine, job_id)
        assert done["status"] == "succeeded"
        assert done["lease_owner"] is None
        with engine.connect() as db:
            assert db.execute(
                text(
                    "SELECT count(*) FROM events WHERE entity_id = :id "
                    "AND event_type = 'job.lease_reclaimed'"
                ),
                {"id": job_id},
            ).scalar_one() == 1
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
        engine.dispose()


if __name__ == "__main__":
    if os.environ.get("BOSS_RUN_RESTART_HOLDER") == "1":
        raise SystemExit(hold_until_killed())
