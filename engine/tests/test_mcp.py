"""MCP JSON-RPC server tests (no DB — uses a stub registry)."""
from __future__ import annotations

import io
import json

from boss_engine.mcp import MCPServer
from boss_engine.tools.registry import ToolRegistry, ToolSpec


def _stub_registry():
    reg = ToolRegistry()
    reg.register(ToolSpec("read_x", "r", {"type": "object"}, lambda ctx, **k: {"n": 1}, kind="read"))
    reg.register(ToolSpec("write_x", "w", {"type": "object"}, lambda ctx, **k: {"done": True}, kind="write"))
    return reg


def test_initialize_and_list():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry())
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"]
    assert init["result"]["serverInfo"]["name"] == "boss-engine"
    lst = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in lst["result"]["tools"]} == {"read_x", "write_x"}


def test_notifications_return_none():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry())
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry())
    resp = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert resp["error"]["code"] == -32601


def test_tools_call_executes():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry())
    resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "read_x", "arguments": {}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["n"] == 1 and resp["result"]["isError"] is False


def test_read_only_blocks_write_tool():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry(), read_only=True)
    names = {t["name"] for t in srv._tools()}
    assert names == {"read_x"}
    resp = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "write_x", "arguments": {}}})
    assert resp["result"]["isError"] is True


def test_catalog_only_context_hides_write_tools():
    ctx = type("CatalogOnlyContext", (), {"durable_workspace_configured": False})()
    srv = MCPServer(ctx=ctx, project_id="p", registry=_stub_registry())

    assert {tool["name"] for tool in srv._tools()} == {"read_x"}


def test_stdio_roundtrip():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry())
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    srv.serve_stdio(stdin=stdin, stdout=stdout)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    # two responses (initialize + tools/list); the notification produced none
    assert len(lines) == 2
    assert lines[0]["id"] == 1 and lines[1]["id"] == 2
