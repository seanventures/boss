"""KG P6b: the reference "bank's agent" stdlib server behaves exactly as docs/community/
OUTSIDE_AGENTS.md describes — ping always 2xx, no-model-configured refuses plainly, and a real
step call reads/writes through MCP using the exact token it was handed."""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import urllib.error
import urllib.request

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sample_external_agent.py"
_SPEC = importlib.util.spec_from_file_location("sample_external_agent", _MODULE_PATH)
sample_external_agent = importlib.util.module_from_spec(_SPEC)
sys.modules["sample_external_agent"] = sample_external_agent
_SPEC.loader.exec_module(sample_external_agent)  # type: ignore[union-attr]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(port: int, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # noqa: PERF203 — the agent's error path is the thing under test
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def agent_port():
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), sample_external_agent._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ping_is_always_2xx(agent_port):
    status, body = _request(agent_port, {"ping": True})
    assert status == 200
    assert body == {"pong": True}


def test_a_real_step_with_no_model_configured_refuses_plainly(agent_port, monkeypatch):
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    status, body = _request(agent_port, {
        "run_id": "wr_1", "step_id": "bank_review", "input": {},
        "contract": {"writes": [{"kind": "agent_decision"}]},
        "kg": {"mcp_url": "http://127.0.0.1:1/mcp", "token": "boss_ext_x", "expires_at": "x"},
    })
    assert status != 200
    assert "error" in body


class _ModelStub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        payload = json.dumps({
            "choices": [{"message": {"content": "statement: clean"}}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


class _MCPStub(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _MCPStub.calls.append({
            "authorization": self.headers.get("Authorization"),
            "method": body.get("method"),
            "name": (body.get("params") or {}).get("name"),
        })
        payload = json.dumps({
            "jsonrpc": "2.0", "id": body.get("id"),
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


def test_a_real_step_with_a_model_writes_the_contract_through_the_given_token(agent_port, monkeypatch):
    model_port, mcp_port = _free_port(), _free_port()
    model_server = HTTPServer(("127.0.0.1", model_port), _ModelStub)
    mcp_server = HTTPServer(("127.0.0.1", mcp_port), _MCPStub)
    for server in (model_server, mcp_server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    _MCPStub.calls.clear()
    monkeypatch.setenv("MODEL_BASE_URL", f"http://127.0.0.1:{model_port}")
    monkeypatch.setenv("MODEL_API_KEY", "sk-test")
    try:
        status, body = _request(agent_port, {
            "run_id": "wr_1", "step_id": "bank_review", "input": {"evidence": "x"},
            "contract": {"writes": [{"kind": "agent_decision", "decision_type": "assessment"}]},
            "kg": {"mcp_url": f"http://127.0.0.1:{mcp_port}/mcp",
                   "token": "boss_ext_the_exact_token", "expires_at": "x"},
        })
        assert status == 200, body
        assert body["ok"] is True
        assert len(_MCPStub.calls) == 1
        call = _MCPStub.calls[0]
        assert call["name"] == "kg_write_agent_decision"
        assert call["authorization"] == "Bearer boss_ext_the_exact_token"
    finally:
        model_server.shutdown()
        mcp_server.shutdown()
