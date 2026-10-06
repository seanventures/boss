"""MCP JSON-RPC server tests (no DB — uses a stub registry)."""
from __future__ import annotations

import io
import json

from boss_engine.mcp import MCPServer
from boss_engine.tools.registry import ToolRegistry, ToolSpec


def _stub_registry():
    reg = ToolRegistry()
    reg.register(ToolSpec("read_x", "r", {"type": "object"}, lambda ctx, **k: {"n": 1}, kind="read", permission="system:read"))
    reg.register(ToolSpec("write_x", "w", {"type": "object"}, lambda ctx, **k: {"done": True}, kind="write", permission="system:read"))
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


def test_malformed_tool_requests_return_json_rpc_errors():
    srv = MCPServer(ctx=None, project_id="p", registry=_stub_registry())
    assert srv.handle([])["error"]["code"] == -32600
    for params in (["write_x"], {"name": "write_x", "arguments": ["bad"]},
                   {"name": ["write_x"], "arguments": {}}):
        response = srv.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call", "params": params})
        assert response["error"]["code"] == -32602


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


def test_tools_call_strips_project_id_override():
    """Session project_id is authoritative — args.project_id must not win."""
    seen: dict = {}

    def _capture(ctx, *, project_id, actor, args):
        seen["project_id"] = project_id
        seen["args"] = dict(args)
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(ToolSpec(
        "write_x", "w",
        {"type": "object", "properties": {"project_id": {"type": "string"}}},
        _capture, kind="write", permission="system:read"))
    srv = MCPServer(ctx=None, project_id="session-project", registry=reg)
    resp = srv.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {
            "name": "write_x",
            "arguments": {"project_id": "other-tenant", "title": "x"},
        },
    })
    assert resp["result"]["isError"] is False
    assert seen["project_id"] == "session-project"
    assert "project_id" not in seen["args"]
    assert seen["args"]["title"] == "x"


def test_mcp_never_accepts_internal_operator_authority():
    """A model cannot turn a destructive tool into an approved operator click."""
    called = []

    def _dangerous(ctx, *, project_id, actor, args):
        called.append(args)
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(ToolSpec(
        "dangerous", "requires operator click",
        {"type": "object", "properties": {
            "project_id": {"type": "string"},
            "_operator_confirmed": {"type": "boolean"},
            "_writes_allowed": {"type": "boolean"},
        }},
        _dangerous, kind="write", reversibility="destroys_work", permission="system:read"))
    srv = MCPServer(ctx=None, project_id="session-project", registry=reg)
    schema = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[
        "result"
    ]["tools"][0]["inputSchema"]
    assert schema["properties"] == {}

    resp = srv.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "dangerous", "arguments": {
            "project_id": "other-project",
            "_operator_confirmed": True,
            "_writes_allowed": True,
        }},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["error"] == "operator_confirmation_required"
    assert resp["result"]["isError"] is True
    assert called == []


def test_mcp_strips_internal_write_license_from_safe_tool():
    seen = []
    reg = ToolRegistry()
    reg.register(ToolSpec(
        "write_x", "write", {"type": "object"},
        lambda ctx, *, args, **kwargs: seen.append(args) or {"ok": True},
        kind="write", permission="system:read"))
    srv = MCPServer(ctx=None, project_id="p", registry=reg)
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "write_x", "arguments": {
                    "_writes_allowed": True, "_operator_confirmed": True, "title": "x",
                }}})
    assert seen == [{"title": "x"}]


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
