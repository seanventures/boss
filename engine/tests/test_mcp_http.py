"""HTTP transport + Mission Control page (regressions for Plane #307 / #296).

Skipped when fastapi isn't installed — the HTTP transport is the optional
[mcp-http] extra; stdio MCP needs none of this.
"""
from __future__ import annotations

import json

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
    return TestClient(MCPServer(ctx=None, project_id="p", registry=reg).asgi_app())


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
