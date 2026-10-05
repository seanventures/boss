"""A 409 from the control plane releases the job and keeps the worker loop alive.

`POST /v1/worker/complete` answering 409 used to raise out of `run_worker_cycle`: the job stayed
leased to this worker for a full lease TTL (~300s) before anything could run it again, and a
worker that ran one cycle at a time simply exited, leaving the lease to a dead process.
"""
from types import SimpleNamespace

import httpx

from apps.worker.boss_worker.control_plane import LeasedJob
from apps.worker.boss_worker.runner import SHUTDOWN_LEASE_TTL_SECONDS, run_worker_cycle
from apps.worker.tests.test_worker import FakeControlPlaneClient


def _conflict(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"http://api{path}")
    response = httpx.Response(409, request=request, json={"detail": "Lease has expired."})
    return httpx.HTTPStatusError("409 Conflict", request=request, response=response)


class RefusingClient(FakeControlPlaneClient):
    def __init__(self, job, *, refuse: set[str], hand_back_refused: bool = False):
        super().__init__(job)
        self.refuse = refuse
        self.hand_back_refused = hand_back_refused

    def heartbeat(self, worker_id, job_id, lease_token, lease_ttl_seconds, handing_back=False):
        self.calls.append(("heartbeat", job_id, lease_ttl_seconds, handing_back))
        if handing_back and self.hand_back_refused:
            raise _conflict("/v1/worker/heartbeat")
        if not handing_back and "heartbeat" in self.refuse:
            raise _conflict("/v1/worker/heartbeat")
        return {"job_id": job_id, "status": "running", "lease_expires_at": "future"}

    def complete(self, worker_id, job_id, lease_token, result):
        self.calls.append(("complete", job_id))
        if "complete" in self.refuse:
            raise _conflict("/v1/worker/complete")
        return {"job_id": job_id, "status": "succeeded"}

    def fail(self, worker_id, job_id, lease_token, error, retryable, retry_delay_seconds):
        self.calls.append(("fail", job_id))
        if "fail" in self.refuse:
            raise _conflict("/v1/worker/fail")
        return {"job_id": job_id, "status": "failed_retryable"}

    def run_stack_deploy(self, payload):
        self.calls.append(("run_stack_deploy", payload["deployment_id"]))
        if getattr(self, "boom", False):
            raise RuntimeError("deploy exploded")
        return {"status": "healthy", "retry": False}


def _job(**payload):
    return LeasedJob(id="job_1", project_id="boss-dogfood", task_id=None,
                     kind="deployment.stack_apply",
                     payload={"deployment_id": "sdep_1", **payload},
                     attempt=1, max_attempts=3, lease_token="lease", lease_expires_at="future")


def _settings():
    return SimpleNamespace(worker_id="w", lease_ttl_seconds=60, poll_interval_seconds=1)


def _hand_backs(client):
    return [c for c in client.calls if c[0] == "heartbeat" and c[3]]


def test_a_refused_completion_hands_the_lease_back_and_the_cycle_returns():
    client = RefusingClient(_job(), refuse={"complete"})
    assert run_worker_cycle(client, _settings()) is True  # did not raise
    assert ("complete", "job_1") in client.calls
    assert _hand_backs(client) == [("heartbeat", "job_1", SHUTDOWN_LEASE_TTL_SECONDS, True)]


def test_a_refused_failure_hands_the_lease_back_too():
    client = RefusingClient(_job(), refuse={"fail"})
    client.boom = True
    assert run_worker_cycle(client, _settings()) is True
    assert ("fail", "job_1") in client.calls
    assert len(_hand_backs(client)) == 1


def test_a_lease_lost_before_work_starts_runs_nothing():
    client = RefusingClient(_job(), refuse={"heartbeat"})
    assert run_worker_cycle(client, _settings()) is True
    names = [c[0] for c in client.calls]
    assert "run_stack_deploy" not in names, names
    assert "complete" not in names


def test_a_job_that_is_no_longer_ours_is_left_alone_without_crashing():
    client = RefusingClient(_job(), refuse={"complete"}, hand_back_refused=True)
    assert run_worker_cycle(client, _settings()) is True


def test_other_errors_still_surface():
    class Broken(RefusingClient):
        def complete(self, *a, **k):
            request = httpx.Request("POST", "http://api/v1/worker/complete")
            raise httpx.HTTPStatusError(
                "500", request=request, response=httpx.Response(500, request=request))

    client = Broken(_job(), refuse=set())
    try:
        run_worker_cycle(client, _settings())
    except httpx.HTTPStatusError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a 500 must not be mistaken for a lost lease")
