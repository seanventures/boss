import subprocess
import sys
from types import SimpleNamespace

from apps.worker.boss_worker.control_plane import LeasedJob
from apps.worker.boss_worker.runner import build_plan_result, run_worker_cycle


class FakeControlPlaneClient:
    def __init__(self, leased_job=None):
        self.leased_job = leased_job
        self.calls = []

    def poll(self, worker_id, capabilities, lease_ttl_seconds):
        self.calls.append(("poll", worker_id, tuple(capabilities), lease_ttl_seconds))
        return self.leased_job

    def heartbeat(self, worker_id, job_id, lease_token, lease_ttl_seconds):
        self.calls.append(
            ("heartbeat", worker_id, job_id, lease_token, lease_ttl_seconds)
        )
        return {"job_id": job_id, "status": "running", "lease_expires_at": "future"}

    def complete(self, worker_id, job_id, lease_token, result):
        self.calls.append(("complete", worker_id, job_id, lease_token, result))
        return {
            "job_id": job_id,
            "status": "succeeded",
            "artifact_id": "art_test",
            "decision_id": "dec_test",
        }

    def fail(
        self, worker_id, job_id, lease_token, error, retryable, retry_delay_seconds
    ):
        self.calls.append(
            (
                "fail",
                worker_id,
                job_id,
                lease_token,
                error,
                retryable,
                retry_delay_seconds,
            )
        )
        return {"job_id": job_id, "status": "deadlettered", "deadletter_id": "dlq_test"}


def test_worker_starts_and_exits_cleanly():
    result = subprocess.run(
        [sys.executable, "-m", "apps.worker.boss_worker.main", "--once"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "worker_started" in result.stderr


def test_build_plan_result_contains_command_context():
    leased_job = LeasedJob(
        id="job_test",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="plan.command",
        payload={"command": "Ship the lease worker.", "constraints": {"deadline": "today"}},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )

    result = build_plan_result(leased_job)

    assert result["summary"] == "Ship the lease worker."
    assert "Ship the lease worker." in result["objectives"]
    assert any("today" in step for step in result["steps"])


def test_run_worker_cycle_completes_leased_job():
    leased_job = LeasedJob(
        id="job_test",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="plan.command",
        payload={"command": "Ship the lease worker.", "constraints": {}},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == ["poll", "heartbeat", "complete"]


def test_run_worker_cycle_deadletters_unsupported_job():
    leased_job = LeasedJob(
        id="job_test",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="unsupported.kind",
        payload={},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == ["poll", "heartbeat", "fail"]
