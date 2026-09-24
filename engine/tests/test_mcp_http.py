"""HTTP transport + Mission Control page (regressions for Plane #307 / #296).

Skipped when fastapi isn't installed — the HTTP transport is the optional
[mcp-http] extra; stdio MCP needs none of this.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from boss_engine.mcp import MCPServer  # noqa: E402
from boss_engine.tools.registry import ToolRegistry, ToolSpec  # noqa: E402


def _client() -> TestClient:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        "read_x", "r", {"type": "object"}, lambda ctx, **k: {"n": 1}, kind="read"
    ))
    return TestClient(MCPServer(
        ctx=None, project_id="p", registry=reg, read_only=True
    ).asgi_app())


def test_mcp_post_is_not_a_query_param():
    # Plane #307: with postponed annotations FastAPI failed to resolve the
    # lazily-imported `Request` type, treated the body as a missing query
    # parameter, and returned 422 for every JSON-RPC POST.
    resp = _client().post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "boss-engine"


def test_tools_call_over_http():
    resp = _client().post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "read_x", "arguments": {}}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["isError"] is False
    assert json.loads(body["result"]["content"][0]["text"])["n"] == 1


def test_malformed_http_rpc_is_reported_without_server_error():
    client = _client()
    assert client.post(
        "/mcp", content="{", headers={"Content-Type": "application/json"}
    ).json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[]).json()["error"]["code"] == -32600


def test_health_lists_tools():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["tools"] == ["read_x"]


def test_mission_control_page_served_at_root():
    resp = _client().get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert "Mission Control" in html
    # the section manifest is injected as real JSON, not the placeholder
    assert "__SECTIONS__" not in html
    assert "project_status" in html and "list_running_work" in html


def test_mission_control_inline_script_has_valid_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    html = _client().get("/").text
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert script is not None
    result = subprocess.run(
        [node, "--check", "-"], input=script.group(1), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_http_cli_defaults_to_read_only():
    """Documented `serve-mcp --http --host 0.0.0.0` must not expose writes."""
    from boss_engine.__main__ import _serve_mcp_read_only, build_parser

    parser = build_parser()
    http = parser.parse_args(["serve-mcp", "--http", "--host", "0.0.0.0"])
    assert _serve_mcp_read_only(http) is True

    opted_in = parser.parse_args(
        ["serve-mcp", "--http", "--allow-writes", "--host", "127.0.0.1"]
    )
    assert _serve_mcp_read_only(opted_in) is False

    stdio = parser.parse_args(["serve-mcp"])
    assert _serve_mcp_read_only(stdio) is False

    stdio_ro = parser.parse_args(["serve-mcp", "--read-only"])
    assert _serve_mcp_read_only(stdio_ro) is True


def test_http_read_only_server_hides_write_tools():
    reg = ToolRegistry()
    reg.register(ToolSpec(
        "read_x", "r", {"type": "object"}, lambda ctx, **k: {"n": 1}, kind="read"
    ))
    reg.register(ToolSpec(
        "write_x", "w", {"type": "object"}, lambda ctx, **k: {"done": True}, kind="write"
    ))
    client = TestClient(
        MCPServer(ctx=None, project_id="p", registry=reg, read_only=True).asgi_app()
    )
    listed = client.get("/health").json()["tools"]
    assert listed == ["read_x"]
    blocked = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
              "params": {"name": "write_x", "arguments": {}}},
    ).json()
    assert blocked["result"]["isError"] is True


def test_write_capable_http_requires_token_and_authenticates_every_rpc():
    reg = ToolRegistry()
    reg.register(ToolSpec(
        "write_x", "w", {"type": "object"},
        lambda ctx, **k: {"done": True}, kind="write",
    ))
    with pytest.raises(ValueError, match="BOSS_MCP_HTTP_TOKEN"):
        MCPServer(ctx=None, project_id="p", registry=reg).asgi_app()

    client = TestClient(MCPServer(
        ctx=None, project_id="p", registry=reg, http_token="secret-token"
    ).asgi_app())
    rpc = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "write_x", "arguments": {}}}
    assert client.post("/mcp", json=rpc).status_code == 401
    assert client.post(
        "/mcp", json=rpc, headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    assert client.get("/health").status_code == 401
    response = client.post(
        "/mcp", json=rpc, headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


def test_write_http_cli_refuses_missing_token_and_public_bind(monkeypatch, capsys):
    from boss_engine.__main__ import _cmd_serve_mcp, build_parser

    monkeypatch.setattr(
        "boss_engine.__main__._ctx",
        lambda: type("Context", (), {"durable_workspace_configured": True})(),
    )
    parser = build_parser()
    monkeypatch.delenv("BOSS_MCP_HTTP_TOKEN", raising=False)
    args = parser.parse_args(["serve-mcp", "--http", "--allow-writes"])
    assert _cmd_serve_mcp(args) == 2
    assert "BOSS_MCP_HTTP_TOKEN" in capsys.readouterr().err

    monkeypatch.setenv("BOSS_MCP_HTTP_TOKEN", "secret-token")
    args = parser.parse_args([
        "serve-mcp", "--http", "--allow-writes", "--host", "0.0.0.0"
    ])
    assert _cmd_serve_mcp(args) == 2
    assert "loopback" in capsys.readouterr().err
