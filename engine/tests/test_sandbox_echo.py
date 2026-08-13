"""Freeze-safe echo exercise: works while coding claims are frozen.

No live Firecracker. Host HTTP is mocked.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from boss_engine.services.sandbox_provider import (
    ECHO_ENGINE,
    ECHO_EXERCISE_TASK_ID,
    SandboxHostUnavailable,
    build_echo_submit_body,
    ensure_microvm_execution_allowed,
    run_echo_exercise,
)


def _settings(**kwargs):
    base = dict(
        sandbox_provider="microvm",
        sandbox_agent_url="http://sandbox-host:8077",
        sandbox_token="tok",
        sandbox_agent_timeout_seconds=30,
        sandbox_poll_interval_seconds=0,
        microvm_claims_frozen=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        request = httpx.Request("GET", "http://sandbox-host:8077/v1/sandbox/tasks")
        self._request = request

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self._request)
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=self._request, response=response
            )


class _FakeHost:
    def __init__(self):
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers or {}})
        return _FakeResponse(202, {"sandbox_task_id": "sbx-echo-1"})

    def get(self, url, headers=None):
        self.gets.append(url)
        return _FakeResponse(
            200,
            {
                "status": "done",
                "result": {
                    "summary": "echo engine wrote SANDBOX_OK.txt",
                    "diff": [{"path": "SANDBOX_OK.txt", "content": "ok"}],
                },
            },
        )


def test_echo_submit_body_is_echo_only():
    body = build_echo_submit_body()
    assert body["engine"] == ECHO_ENGINE
    assert body["test_mode"] is True
    assert body["task_id"] == ECHO_EXERCISE_TASK_ID
    assert body["engine_env"] == {}
    assert "claude" not in str(body).lower()
    assert body["run_mutation"] is False


def test_echo_exercise_refuses_missing_host():
    with pytest.raises(SandboxHostUnavailable) as ei:
        run_echo_exercise(_settings(sandbox_agent_url=""))
    assert ei.value.status_code == 503
    assert ei.value.code == "sandbox_host_unavailable"
    assert "BOSS_SANDBOX_AGENT_URL" in ei.value.message


def test_echo_exercise_works_while_claims_frozen():
    """422 is for coding claims, not echo. Frozen must still succeed."""
    host = _FakeHost()
    ensure_microvm_execution_allowed  # freeze helper still exists for coding
    out = run_echo_exercise(_settings(microvm_claims_frozen=True), http_client=host)
    assert out["claims_frozen"] is True
    assert out["coding_claimed"] is False
    assert out["claude_code_claimed"] is False
    assert out["engine"] == "echo"
    assert out["test_mode"] is True
    assert out["sandbox_task_id"] == "sbx-echo-1"
    assert out["final_status"] == "done"
    assert out["diff_paths"] == ["SANDBOX_OK.txt"]
    assert out["reached"] is True
    submitted = host.posts[0]["json"]
    assert submitted["engine"] == "echo"
    assert submitted["test_mode"] is True
    assert submitted["engine"] != "claude_code"
    assert host.posts[0]["headers"]["X-Sandbox-Token"] == "tok"


def test_echo_exercise_does_not_raise_frozen():
    from boss_engine.services.sandbox_provider import MicroVMClaimsFrozen

    with pytest.raises(MicroVMClaimsFrozen):
        ensure_microvm_execution_allowed(_settings(microvm_claims_frozen=True))
    host = _FakeHost()
    out = run_echo_exercise(_settings(microvm_claims_frozen=True), http_client=host)
    assert out["reached"] is True
    assert out["coding_claimed"] is False


def test_echo_exercise_connection_error_is_503():
    class _Boom:
        def post(self, *a, **k):
            raise httpx.ConnectError("connection refused", request=MagicMock())

        def get(self, *a, **k):
            raise AssertionError("must not poll after submit failure")

    with pytest.raises(SandboxHostUnavailable) as ei:
        run_echo_exercise(_settings(), http_client=_Boom())
    assert ei.value.status_code == 503
    assert "submit echo" in ei.value.message


def test_echo_exercise_missing_task_id_is_503():
    class _NoSid:
        def post(self, *a, **k):
            return _FakeResponse(202, {})

        def get(self, *a, **k):
            raise AssertionError("must not poll without sandbox_task_id")

    with pytest.raises(SandboxHostUnavailable) as ei:
        run_echo_exercise(_settings(), http_client=_NoSid())
    assert ei.value.status_code == 503
    assert "sandbox_task_id" in ei.value.message


def test_echo_http_route_503_when_host_missing_and_200_when_frozen(monkeypatch):
    """Standalone router — no Postgres, no live Firecracker."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.api.app.routers.sandbox_echo import router

    app = fastapi.FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(
        "apps.api.app.routers.sandbox_echo.get_settings",
        lambda: _settings(sandbox_agent_url=""),
    )
    missing = client.post("/v1/diag/sandbox-echo")
    assert missing.status_code == 503
    assert missing.status_code != 422

    monkeypatch.setattr(
        "apps.api.app.routers.sandbox_echo.get_settings",
        lambda: _settings(microvm_claims_frozen=True),
    )
    monkeypatch.setattr(
        "apps.api.app.routers.sandbox_echo.run_echo_exercise",
        lambda settings, **kwargs: {
            "url": "http://sandbox-host:8077",
            "claims_frozen": True,
            "engine": "echo",
            "test_mode": True,
            "coding_claimed": False,
            "claude_code_claimed": False,
            "reached": True,
            "diff_paths": ["SANDBOX_OK.txt"],
        },
    )
    ok = client.post("/v1/diag/sandbox-echo")
    assert ok.status_code == 200
    body = ok.json()
    assert body["engine"] == "echo"
    assert body["coding_claimed"] is False
    assert body["claims_frozen"] is True


def test_platform_py_mounts_sandbox_echo_router():
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "routers" / "platform.py"
    text = source.read_text(encoding="utf-8")
    assert "from .sandbox_echo import router as sandbox_echo_router" in text
    assert "app.include_router(sandbox_echo_router)" in text
