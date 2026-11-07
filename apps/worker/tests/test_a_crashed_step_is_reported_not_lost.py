"""A step the control plane crashed on is reported, in words, even over a dropped connection.

The laptop, 2026-11-07: /v1/code-tasks/step answered 500, the server closed the pooled
connection, and the worker's /v1/worker/fail went out on it -- "Connection reset by peer". The
failure was never recorded; the lease expired; the same crash ran again every five minutes for
hours (attempt 30 of 3). Now the report is retried once on a transport error, and the step's
error carries what the control plane said instead of "500 Internal Server Error".
"""
from __future__ import annotations

import httpx
import pytest

from boss_worker.control_plane import ControlPlaneClient, StepCrashed


def _client(handler) -> ControlPlaneClient:
    c = ControlPlaneClient.__new__(ControlPlaneClient)
    c._client = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    c._lease_timeout = 5
    return c


def test_the_failure_report_is_retried_once_over_a_reset_connection():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ReadError("Connection reset by peer", request=request)
        return httpx.Response(200, json={"status": "deadlettered"})

    out = _client(handler).fail("w", "job_1", "lease", {"message": "boom"}, retryable=False,
                                retry_delay_seconds=0)
    assert out == {"status": "deadlettered"} and calls == ["/v1/worker/fail"] * 2


def test_a_report_that_keeps_failing_still_raises():
    def handler(request):
        raise httpx.ReadError("reset", request=request)

    with pytest.raises(httpx.ReadError):
        _client(handler).fail("w", "j", "l", {}, retryable=False, retry_delay_seconds=0)


def test_a_crashed_step_says_what_crashed():
    def handler(request):
        return httpx.Response(500, json={"detail": "ObjectNotExecutableError: Not an executable "
                                                   "object (at person_wait.py:125 _load_run)"})

    with pytest.raises(StepCrashed) as got:
        _client(handler).run_code_task({"run_id": "crun_1"})
    assert "person_wait.py:125" in str(got.value) and "HTTP 500" in str(got.value)
