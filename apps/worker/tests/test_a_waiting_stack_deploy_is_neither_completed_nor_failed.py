"""A stack deployment waiting on its target is handed back by the API, not failed by the worker.

The API re-queues the job itself without spending an attempt (JobLeaseService.defer_leased),
so the worker must neither complete it (that would mark waiting work done) nor fail it (that
would spend attempts and deadletter a deploy that was only queued behind another).
"""
from types import SimpleNamespace

from apps.worker.boss_worker.control_plane import LeasedJob
from apps.worker.boss_worker.runner import run_worker_cycle
from apps.worker.tests.test_worker import FakeControlPlaneClient


class StackClient(FakeControlPlaneClient):
    def __init__(self, job, answer):
        super().__init__(job)
        self.answer = answer

    def run_stack_deploy(self, payload):
        self.calls.append(("run_stack_deploy", payload["deployment_id"]))
        return self.answer


def _cycle(answer):
    job = LeasedJob(id="job_stack", project_id="boss-dogfood", task_id=None,
                    kind="deployment.stack_apply", payload={"deployment_id": "sdep_1"},
                    attempt=1, max_attempts=5, lease_token="lease", lease_expires_at="future")
    client = StackClient(job, answer)
    settings = SimpleNamespace(worker_id="w", lease_ttl_seconds=60, poll_interval_seconds=1)
    assert run_worker_cycle(client, settings) is True
    return [call[0] for call in client.calls]


def test_a_deferred_stack_deploy_is_left_to_the_queue():
    calls = _cycle({"status": "waiting", "job_deferred": True, "retry": False,
                    "error": "Waiting for deployment sdep_0 to finish."})
    assert "complete" not in calls and "fail" not in calls, calls
    assert "run_stack_deploy" in calls


def test_a_finished_stack_deploy_completes_its_job():
    calls = _cycle({"status": "healthy", "retry": False})
    assert calls[-1] == "complete", calls
