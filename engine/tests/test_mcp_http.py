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
