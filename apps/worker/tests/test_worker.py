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

    def create_openai_plan(self, payload):
        self.calls.append(("create_openai_plan", payload))
        return {
            "status": "created",
            "artifact_id": "art_plan",
            "receipt_id": "rcpt_plan",
        }

    def create_pull_request_proposal(self, payload):
        self.calls.append(("create_pull_request_proposal", payload))
        return {
            "status": "created",
            "artifact_id": "art_review",
            "diff_artifact_id": "art_diff",
            "test_report_artifact_id": "art_test_report",
            "receipt_id": "rcpt_pr",
            "decision_id": "dec_pr",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/1",
            "branch_name": payload["branch_name"],
        }

    def merge_pull_request(self, payload):
        self.calls.append(("merge_pull_request", payload))
        return {
            "status": "created",
            "artifact_id": "art_merge",
            "receipt_id": "rcpt_merge",
            "decision_id": payload["decision_id"],
            "pull_request_url": payload["pull_request_url"],
            "branch_name": payload.get("branch_name"),
            "merged_sha": "abc123",
        }

    def create_deployment(self, payload):
        self.calls.append(("create_deployment", payload))
        return {
            "status": "created",
            "artifact_id": "art_deploy",
            "receipt_id": "rcpt_deploy",
            "decision_id": payload["decision_id"],
            "environment": payload["environment"],
            "deployment_id": 12345,
            "deployment_status": "success",
            "deployment_url": "https://api.github.com/repos/seanheiney/boss-sandbox/deployments/12345",
            "merged_sha": payload["merged_sha"],
        }

    def deliver_outbox_message(self, message_id, payload):
        self.calls.append(("deliver_outbox_message", message_id, payload))
        return {
            "status": "created",
            "message_id": message_id,
            "receipt_id": "rcpt_comms",
            "provider_ref": "slack-webhook:slack:#ops",
        }


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


def test_run_worker_cycle_executes_queued_openai_plan_job():
    leased_job = LeasedJob(
        id="job_openai",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="openai.plan",
        payload={
            "reservation_id": "res_test",
            "dedupe_key": "plan-test",
            "prompt": "Create a plan.",
            "system_prompt": "Return markdown.",
        },
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
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_openai_plan",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_pr_proposal_job():
    leased_job = LeasedJob(
        id="job_pr",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="software_factory.propose_pr",
        payload={
            "reservation_id": "res_test",
            "dedupe_key": "pr-test",
            "title": "Sandbox PR",
            "branch_name": "boss/pr-test",
            "summary": "Create a sandbox PR proposal.",
            "files": [{"path": "README.md", "content": "hello"}],
        },
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
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_pull_request_proposal",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_outbox_delivery_job():
    leased_job = LeasedJob(
        id="job_comms",
        project_id="boss-dogfood",
        task_id=None,
        kind="comms.outbox.deliver",
        payload={
            "outbox_message_id": "out_test",
            "reservation_id": "res_test",
            "dedupe_key": "comms-test",
        },
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
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "deliver_outbox_message",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_pr_merge_job():
    leased_job = LeasedJob(
        id="job_merge",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="github.pull_request.merge",
        payload={
            "decision_id": "dec_merge",
            "dedupe_key": "merge-test",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Merge Demo",
            "branch_name": "boss/demo-pr",
        },
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
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "merge_pull_request",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_deployment_job():
    leased_job = LeasedJob(
        id="job_deploy",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="github.deployment.create",
        payload={
            "decision_id": "dec_deploy",
            "dedupe_key": "deploy-test",
            "merged_sha": "abc123",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Deploy Demo",
            "branch_name": "boss/demo-pr",
            "environment": "staging",
        },
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
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_deployment",
        "complete",
    ]
