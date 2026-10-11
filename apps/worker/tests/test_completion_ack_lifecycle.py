"""Lease-control transport bounds and heartbeat ownership, without external APIs."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import httpx
import pytest

from apps.worker.boss_worker import runner
from apps.worker.boss_worker.control_plane import ControlPlaneClient, LeasedJob
from apps.worker.tests.test_worker import FakeControlPlaneClient


@pytest.mark.parametrize("operation", ["heartbeat", "complete", "fail"])
def test_lease_control_times_out_before_delayed_local_ack(operation):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            entered.set()
            release.wait(5)  # only a stuck-test backstop; controller releases after timeout
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            except (BrokenPipeError, ConnectionResetError):
                pass  # expected: the owned client timed out before this response

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ControlPlaneClient(f"http://127.0.0.1:{server.server_port}", timeout=.1)
    try:
        with pytest.raises(httpx.ReadTimeout):
            if operation == "heartbeat":
                client.heartbeat("owned", "job", "owned-token", 30)
            elif operation == "complete":
                client.complete("owned", "job", "owned-token", {})
            else:
                client.fail("owned", "job", "owned-token", {}, True, 1)
        assert entered.is_set() and not release.is_set()
    finally:
        release.set()
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_execution_timeout_is_separate_and_lease_timeout_cannot_be_unbounded():
    requests = []
    client = ControlPlaneClient("http://owned.test", timeout=100000)
    client._client.close()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    client._client = httpx.Client(base_url=client.base_url,
                                 timeout=httpx.Timeout(connect=10, read=7200, write=30, pool=30),
                                 transport=httpx.MockTransport(respond))
    try:
        client.heartbeat("owned", "job", "token", 30)
        client.complete("owned", "job", "token", {})
        client.fail("owned", "job", "token", {}, True, 1)
        client.run_code_task({"run_id": "owned-fixture"})
    finally:
        client.close()
    assert all(set(r.extensions["timeout"].values()) == {30.0} for r in requests[:3])
    assert requests[3].extensions["timeout"]["read"] == 7200.0


@pytest.mark.parametrize("exit_path", ["complete", "failed_execution", "deferred", "reconciler", "lost_ack"])
def test_every_exit_stops_and_closes_the_owned_heartbeat_client(monkeypatch, exit_path):
    runner.SHUTDOWN.clear()
    closed = []

    class HeartbeatClient:
        def __init__(self, _url, *, timeout, api_key):
            assert timeout <= 3

        def heartbeat(self, *_a, **_k):
            raise AssertionError("immediate fixture must not need renewal")

        def close(self):
            closed.append(threading.current_thread().name)

    job = LeasedJob(id="owned-lifecycle", project_id="owned", task_id=None,
                    kind="github.repository.reconcile" if exit_path == "reconciler" else "owned.local",
                    payload={}, attempt=1, max_attempts=3, lease_token="fixture", lease_expires_at="future")
    client = FakeControlPlaneClient(job)
    settings = SimpleNamespace(worker_id="owned", lease_ttl_seconds=60, job_max_runtime_seconds=120,
                               control_plane_url="http://owned.test", api_key=None)
    monkeypatch.setattr(runner, "ControlPlaneClient", HeartbeatClient)
    monkeypatch.setattr(runner, "_tick_ai_sre_overwatch", lambda *_: None)

    def execute(*_a):
        if exit_path == "failed_execution":
            raise RuntimeError("owned execution failure")
        return {"job_deferred": exit_path == "deferred", "job": {"status": "succeeded"}}

    monkeypatch.setattr(runner, "execute_job", execute)
    if exit_path == "lost_ack":
        def lost(*_a):
            raise httpx.ReadTimeout("owned acknowledgment timeout")
        client.complete = lost
        with pytest.raises(httpx.ReadTimeout):
            runner.run_worker_cycle(client, settings)
    else:
        assert runner.run_worker_cycle(client, settings)
    assert closed == ["lease-hb-owned-lifecycle"]
    assert not any(t.name == "lease-hb-owned-lifecycle" and t.is_alive() for t in threading.enumerate())
    if exit_path in ("deferred", "reconciler"):
        assert not any(c[0] in ("complete", "fail") for c in client.calls)


def test_sliced_wait_observes_settlement_deadline_without_waiting_full_interval():
    stop = threading.Event()
    assert runner._wait_in_slices(stop, 60, slice_s=.01, deadline=lambda: 0.0) is False
    assert not stop.is_set()
