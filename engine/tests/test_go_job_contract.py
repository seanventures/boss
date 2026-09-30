"""BOSS-97 parent seam: Python ⇄ Go async job contract.

Covers enqueue / status / cancel / heartbeat, fail-closed timeouts, JSON Schema,
and the golden rule that the engine runs with Go unset (no Go service required).
Does not start Firecracker, spend tokens, or implement S1/S2/S3 binaries.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from boss_engine.services import go_job_contract as gj
from boss_engine.services.sandbox_contract import CONTRACT_VERSION
from boss_engine.services.sandbox_provider import (
    LocalSubprocessProvider,
    make_sandbox_provider,
)


def _settings(**kwargs):
    base = dict(
        sandbox_provider="local",
        sandbox_agent_url="",
        sandbox_token="",
        sandbox_agent_timeout_seconds=5,
        sandbox_poll_interval_seconds=0,
        microvm_claims_frozen=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


# --- engine works with Go unset ----------------------------------------------


def test_service_available_is_false_when_url_unset():
    assert gj.service_available(_settings(sandbox_agent_url="")) is False
    assert gj.service_available(_settings()) is False
    assert gj.service_available(SimpleNamespace()) is False
    assert gj.service_available(_settings(sandbox_agent_url="http://go:8077")) is True


def test_default_settings_select_local_provider_without_go(monkeypatch):
    for key in ("BOSS_SANDBOX_PROVIDER", "BOSS_SANDBOX_AGENT_URL"):
        monkeypatch.delenv(key, raising=False)
    from boss_engine.config import Settings

    # `_env_file=None` matters as much as delenv: this test asserts the DEFAULT, and the repo's
    # own .env sets BOSS_SANDBOX_PROVIDER=microvm. Clearing the environment variable does not
    # touch a value that arrives from the FILE, so the test failed for everyone running it from
    # the repo root and passed only from a checkout with no .env — which is how it looked like a
    # regression when it had been failing all along.
    settings = Settings(_env_file=None)
    assert settings.sandbox_provider == "local"
    provider = make_sandbox_provider(settings)
    assert isinstance(provider, LocalSubprocessProvider)
    sandbox = provider.acquire(task_id="crun_no_go")
    assert sandbox is not None


def test_go_job_client_fail_closed_when_go_unset():
    client = gj.GoJobClient(_settings(sandbox_agent_url=""))
    with pytest.raises(gj.GoServiceUnset) as ei:
        client.enqueue({"owner": "o", "repo": "r", "task_id": "t1"})
    assert ei.value.code == "go_service_unset"
    with pytest.raises(gj.GoServiceUnset):
        client.status("sid1")
    with pytest.raises(gj.GoServiceUnset):
        client.cancel("sid1", reason="stop")
    with pytest.raises(gj.GoServiceUnset):
        client.heartbeat("sid1")


# --- routes + request/response shape -----------------------------------------


def test_job_routes_do_not_collide_with_python_jobs_api():
    routes = gj.routes("sid-1")
    assert routes["enqueue"] == "/v1/sandbox/tasks"
    assert routes["status"] == "/v1/sandbox/tasks/sid-1"
    assert routes["cancel"] == "/v1/sandbox/tasks/sid-1/cancel"
    assert routes["heartbeat"] == "/v1/sandbox/tasks/sid-1/heartbeat"
    assert routes["release"] == "/v1/sandbox/tasks/sid-1"
    # Must never collide with the engine's durable /v1/jobs queue.
    for path in routes.values():
        assert not path.startswith("/v1/jobs")
        assert "/v1/jobs/" not in path


def test_build_enqueue_request_is_versioned_and_has_receipt_fields():
    body = gj.build_enqueue_request(
        {"owner": "example-user", "repo": "boss-sandbox", "task_id": "crun_9", "engine": "echo"},
        "tenant-1",
        idempotency_key="idem-1",
        timeout_s=30,
    )
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["op"] == "enqueue"
    assert body["task_id"] == "crun_9"
    assert body["tenant_id"] == "tenant-1"
    assert body["timeout_s"] == 30
    assert body["idempotency_key"] == "idem-1"
    assert body["dedupe_key"] == "go-job:enqueue:tenant-1:crun_9:idem-1"
    assert body["engine"] == "echo"
    assert gj.validate_enqueue_request(body) == []


def test_cancel_and_heartbeat_bodies():
    cancel = gj.build_cancel_request("sid-1", reason="operator_stop")
    assert cancel["op"] == "cancel"
    assert cancel["job_id"] == "sid-1"
    assert cancel["reason"] == "operator_stop"
    assert cancel["contract_version"] == CONTRACT_VERSION
    assert gj.validate_cancel_request(cancel) == []

    beat = gj.build_heartbeat_request("sid-1")
    assert beat["op"] == "heartbeat"
    assert beat["job_id"] == "sid-1"
    assert gj.validate_heartbeat_request(beat) == []


def test_parse_status_treats_cancelled_as_terminal_not_success():
    parsed = gj.parse_status({"status": "cancelled"})
    assert parsed["terminal"] is True
    assert parsed["succeeded"] is False
    assert parsed["status"] == "cancelled"


def test_parse_enqueue_response_is_defensive():
    ok = gj.parse_enqueue_response({"sandbox_task_id": "sid-9", "status": "queued"})
    assert ok["job_id"] == "sid-9"
    assert ok["status"] == "queued"
    assert ok["receipt"]["effect_kind"] == "go_job.enqueue"
    assert "go-job:enqueue:" in ok["receipt"]["dedupe_key"]
    assert gj.parse_enqueue_response("junk")["job_id"] is None


# --- JSON Schema -------------------------------------------------------------


def test_contract_json_schema_file_exists_and_has_required_defs():
    schema = gj.load_schema()
    assert schema["$id"].endswith("go-job/v1.json")
    for name in (
        "EnqueueRequest",
        "EnqueueResponse",
        "StatusResponse",
        "CancelRequest",
        "CancelResponse",
        "HeartbeatRequest",
        "HeartbeatResponse",
        "ErrorResponse",
    ):
        assert name in schema["$defs"], name


def test_contract_json_schema_accepts_valid_enqueue_and_rejects_bad():
    good = gj.build_enqueue_request(
        {"owner": "o", "repo": "r", "task_id": "t1"}, "default", timeout_s=10
    )
    assert gj.validate_against_schema(good, "EnqueueRequest") == []
    bad = {"contract_version": "9.9", "op": "enqueue"}
    problems = gj.validate_against_schema(bad, "EnqueueRequest")
    assert problems
    assert any("contract_version" in p or "task_id" in p or "repo" in p for p in problems)


def test_contract_json_schema_validates_cancel_heartbeat_status():
    assert gj.validate_against_schema(
        gj.build_cancel_request("sid-1", reason="stop"), "CancelRequest"
    ) == []
    assert gj.validate_against_schema(
        gj.build_heartbeat_request("sid-1"), "HeartbeatRequest"
    ) == []
    status = {
        "contract_version": CONTRACT_VERSION,
        "job_id": "sid-1",
        "status": "running",
        "phase": "implement",
    }
    assert gj.validate_against_schema(status, "StatusResponse") == []
    bad_status = {"contract_version": CONTRACT_VERSION, "job_id": "sid-1", "status": "nope"}
    assert gj.validate_against_schema(bad_status, "StatusResponse")


# --- fail-closed timeouts + HTTP client --------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("POST", "http://go:8077/v1/sandbox/tasks")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _FakeHost:
    def __init__(self, *, timeout=False, payload=None, status_code=200):
        self.timeout = timeout
        self.payload = payload if payload is not None else {"sandbox_task_id": "sid-1", "status": "queued"}
        self.status_code = status_code
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.timeout:
            raise httpx.TimeoutException("simulated timeout")
        return _FakeResponse(self.status_code, self.payload)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


def test_enqueue_timeout_is_fail_closed_not_success():
    host = _FakeHost(timeout=True)
    client = gj.GoJobClient(
        _settings(sandbox_agent_url="http://go:8077"),
        http_client=host,
        timeouts_s={"enqueue": 0.01, "status": 0.01, "cancel": 0.01, "heartbeat": 0.01},
    )
    with pytest.raises(gj.GoJobTimeout) as ei:
        client.enqueue({"owner": "o", "repo": "r", "task_id": "t1"})
    assert ei.value.code == "go_job_timeout"
    assert ei.value.op == "enqueue"


def test_status_cancel_heartbeat_round_trip_on_fake_host():
    host = _FakeHost(payload={"sandbox_task_id": "sid-1", "status": "queued"})
    client = gj.GoJobClient(
        _settings(sandbox_agent_url="http://go:8077", sandbox_token="tok"),
        http_client=host,
    )
    enq = client.enqueue({"owner": "o", "repo": "r", "task_id": "t1"})
    assert enq["job_id"] == "sid-1"
    assert host.calls[0]["headers"]["X-Sandbox-Token"] == "tok"

    host.payload = {"status": "running", "phase": "clone"}
    st = client.status("sid-1")
    assert st["status"] == "running" and st["terminal"] is False

    host.payload = {"status": "cancelled", "job_id": "sid-1"}
    cancelled = client.cancel("sid-1", reason="stop")
    assert cancelled["status"] == "cancelled"
    assert cancelled["terminal"] is True

    host.payload = {"status": "running", "job_id": "sid-1", "heartbeat_ok": True}
    beat = client.heartbeat("sid-1")
    assert beat["heartbeat_ok"] is True
    assert any(c["url"].endswith("/cancel") for c in host.calls)
    assert any(c["url"].endswith("/heartbeat") for c in host.calls)


def test_default_timeouts_are_finite_and_named():
    timeouts = gj.DEFAULT_TIMEOUTS_S
    for op in ("enqueue", "status", "cancel", "heartbeat"):
        assert op in timeouts
        assert 0 < timeouts[op] <= 60
