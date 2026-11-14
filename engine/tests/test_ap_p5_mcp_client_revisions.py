"""AP-P5 (BOSSAPP-1056, D10): BOSS's MCP client talks to servers on 2024-11-05, 2025-11-25 and
2026-07-28, and picks its OAuth client registration the 2026-07-28 way (Client ID Metadata
Documents before Dynamic Client Registration). No database: each server is an httpx fake that
speaks one revision.
"""
from __future__ import annotations

import json

import httpx
import pytest

from boss_engine.services import mcp_client, mcp_oauth
from boss_engine.services.mcp_client import (MCPClient, MCPInputRequired, MCPTaskPending,
                                             MCPToolError, forget_sessions)

TOOLS = [{"name": "zeta", "inputSchema": {"type": "object"}},
         {"name": "alpha", "inputSchema": {"type": "object"}}]
META_V = "io.modelcontextprotocol/protocolVersion"


def _rpc(rid, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": rid}
    body["error" if error is not None else "result"] = error if error is not None else result
    return httpx.Response(200, json=body)


class LegacyServer:
    """2024-11-05 (no sessions) or 2025-11-25 (sessions). Answers server/discover the way a
    legacy server does: method not found."""

    def __init__(self, version: str, *, sessions: bool):
        self.version, self.sessions = version, sessions
        self.seen: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        self.seen.append({"method": method, "headers": dict(request.headers), "body": body})
        if method == "initialize":
            asked = body["params"]["protocolVersion"]
            agreed = asked if asked == self.version else self.version
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": agreed, "capabilities": {"tools": {}}}},
                headers={"Mcp-Session-Id": "s-1"} if self.sessions else {})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if self.sessions and request.headers.get("mcp-session-id") != "s-1":
            return httpx.Response(400, json={"error": "no session"})
        if method == "tools/list":
            return _rpc(body["id"], {"tools": TOOLS})
        if method == "tools/call":
            return _rpc(body["id"], {"content": [{"type": "text", "text": json.dumps(
                {"said": body["params"]["arguments"]})}]})
        return _rpc(body.get("id"), error={"code": -32601, "message": f"no {method}"})


class ModernServer:
    """2026-07-28: stateless, server/discover, the tasks extension; `slow` returns a task that
    is working twice then completed; `ask` asks for input; `ask_task` is a task that waits for
    input; `state_only` wants its requestState echoed once."""

    def __init__(self):
        self.seen: list[dict] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        params = body.get("params") or {}
        self.seen.append({"method": method, "headers": dict(request.headers), "body": body})
        meta = params.get("_meta") or {}
        if meta.get(META_V) != "2026-07-28":
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": body.get("id"), "error": {
                "code": -32022, "message": "Unsupported protocol version",
                "data": {"supported": ["2026-07-28"], "requested": meta.get(META_V)}}})
        caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        rid = body["id"]
        if method == "server/discover":
            return _rpc(rid, {"resultType": "complete", "supportedVersions": ["2026-07-28"],
                              "capabilities": {"tools": {}, "extensions": {
                                  "io.modelcontextprotocol/tasks": {}}},
                              "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "m"}}})
        if method == "tools/list":
            return _rpc(rid, {"resultType": "complete", "tools": TOOLS, "ttlMs": 0,
                              "cacheScope": "private"})
        if method == "tools/call":
            name = params["name"]
            tasks_ok = "io.modelcontextprotocol/tasks" in (caps.get("extensions") or {})
            if name in ("slow", "ask_task", "fails") and tasks_ok:
                return _rpc(rid, {"resultType": "task", "taskId": f"t-{name}",
                                  "status": "working", "createdAt": "x", "lastUpdatedAt": "x",
                                  "ttlMs": None, "pollIntervalMs": 1})
            if name == "ask":
                return _rpc(rid, {"resultType": "input_required", "inputRequests": {
                    "confirm": {"method": "elicitation/create", "params": {
                        "mode": "form", "message": "Approve?",
                        "requestedSchema": {"type": "object"}}}}, "requestState": "st-1"})
            if name == "state_only":
                if params.get("requestState") == "opaque":
                    return _rpc(rid, {"resultType": "complete", "content": [
                        {"type": "text", "text": "\"second time\""}]})
                return _rpc(rid, {"resultType": "input_required", "requestState": "opaque"})
            return _rpc(rid, {"resultType": "complete", "content": [
                {"type": "text", "text": json.dumps({"said": params.get("arguments")})}]})
        if method == "tasks/get":
            tid = params["taskId"]
            self.polls += 1
            base = {"resultType": "complete", "taskId": tid, "createdAt": "x",
                    "lastUpdatedAt": "x", "ttlMs": None, "pollIntervalMs": 1}
            if tid == "t-ask_task":
                return _rpc(rid, {**base, "status": "input_required", "inputRequests": {
                    "plan": {"method": "elicitation/create", "params": {
                        "mode": "form", "message": "Approve the plan?",
                        "requestedSchema": {"type": "object"}}}}})
            if tid == "t-fails":
                return _rpc(rid, {**base, "status": "failed",
                                  "error": {"code": -32603, "message": "build broke"}})
            if tid == "t-forever":
                return _rpc(rid, {**base, "status": "working"})
            if self.polls < 3:
                return _rpc(rid, {**base, "status": "working"})
            return _rpc(rid, {**base, "status": "completed", "result": {
                "content": [{"type": "text", "text": json.dumps({"pr": 7})}]}})
        if method == "tasks/cancel":
            return _rpc(rid, {"resultType": "complete"})
        return _rpc(rid, error={"code": -32601, "message": "nope"})


@pytest.fixture()
def serve(monkeypatch):
    forget_sessions()
    servers: dict[str, object] = {}

    def route(request: httpx.Request) -> httpx.Response:
        return servers[request.url.host](request)

    transport = httpx.MockTransport(route)

    def post(url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(mcp_client.httpx, "post", post)
    yield servers
    forget_sessions()


def test_a_2024_11_05_server_without_sessions(serve):
    server = serve["old.example"] = LegacyServer("2024-11-05", sessions=False)
    client = MCPClient(None)
    url = "https://old.example/mcp"
    assert [t["name"] for t in client.list_tools(url)] == ["zeta", "alpha"]
    assert client.call_tool(url, "zeta", {"x": 1}) == {"said": {"x": 1}}
    assert client.era(url) == {"modern": False, "version": None}
    methods = [s["method"] for s in server.seen]
    assert methods.count("server/discover") == 1, "the era is probed once and cached"
    assert methods.count("initialize") == 1
    init = next(s for s in server.seen if s["method"] == "initialize")
    assert init["body"]["params"]["protocolVersion"] == "2025-11-25", "asks the newest legacy"
    call = next(s for s in server.seen if s["method"] == "tools/call")
    assert call["headers"]["mcp-protocol-version"] == "2024-11-05", "speaks what it agreed"


def test_a_2025_11_25_server_with_sessions(serve):
    server = serve["mid.example"] = LegacyServer("2025-11-25", sessions=True)
    client = MCPClient(None)
    url = "https://mid.example/mcp"
    assert len(client.list_tools(url)) == 2
    assert client.call_tool(url, "alpha", {}) == {"said": {}}
    call = next(s for s in server.seen if s["method"] == "tools/call")
    assert call["headers"]["mcp-session-id"] == "s-1"
    assert call["headers"]["mcp-protocol-version"] == "2025-11-25"
    assert "_meta" not in call["body"]["params"], "legacy requests carry no modern _meta"


def test_a_2026_07_28_server_is_stateless(serve):
    server = serve["new.example"] = ModernServer()
    client = MCPClient(None)
    url = "https://new.example/mcp"
    assert [t["name"] for t in client.list_tools(url)] == ["zeta", "alpha"]
    assert client.call_tool(url, "zeta", {"q": "x"}) == {"said": {"q": "x"}}
    assert client.era(url) == {"modern": True, "version": "2026-07-28"}
    methods = [s["method"] for s in server.seen]
    assert "initialize" not in methods and "notifications/initialized" not in methods
    assert methods.count("server/discover") == 1
    for seen in server.seen:
        assert "mcp-session-id" not in seen["headers"]
        assert seen["headers"]["mcp-protocol-version"] == "2026-07-28"
        assert seen["headers"]["mcp-method"] == seen["method"]
        meta = seen["body"]["params"]["_meta"]
        assert meta[META_V] == "2026-07-28"
        caps = meta["io.modelcontextprotocol/clientCapabilities"]
        assert caps == {"extensions": {"io.modelcontextprotocol/tasks": {}}}, \
            "no elicitation, sampling or roots: deprecated / never answered by BOSS"
    call = next(s for s in server.seen if s["method"] == "tools/call")
    assert call["headers"]["mcp-name"] == "zeta"


def test_a_task_is_polled_to_completion(serve):
    server = serve["new.example"] = ModernServer()
    client = MCPClient(None, task_timeout=10)
    assert client.call_tool("https://new.example/mcp", "slow", {}) == {"pr": 7}
    gets = [s for s in server.seen if s["method"] == "tasks/get"]
    assert len(gets) == 3 and all(g["body"]["params"]["taskId"] == "t-slow" for g in gets)


def test_a_failed_task_is_a_tool_error_and_a_slow_one_hands_back_its_handle(serve):
    serve["new.example"] = ModernServer()
    client = MCPClient(None, task_timeout=0.05)
    with pytest.raises(MCPToolError, match="build broke"):
        client.call_tool("https://new.example/mcp", "fails", {})
    with pytest.raises(MCPTaskPending) as pending:
        client._await_task("https://new.example/mcp", {"taskId": "t-forever",
                                                         "status": "working",
                                                         "pollIntervalMs": 1}, None)
    assert pending.value.task_id == "t-forever"
    assert client.cancel_task("https://new.example/mcp", "t-forever") == {"resultType": "complete"}


def test_input_required_is_raised_with_its_requests(serve):
    serve["new.example"] = ModernServer()
    client = MCPClient(None)
    with pytest.raises(MCPInputRequired) as asked:
        client.call_tool("https://new.example/mcp", "ask", {})
    assert asked.value.request_state == "st-1" and asked.value.task_id is None
    assert asked.value.input_requests["confirm"]["method"] == "elicitation/create"
    with pytest.raises(MCPInputRequired) as waiting:
        client.call_tool("https://new.example/mcp", "ask_task", {})
    assert waiting.value.task_id == "t-ask_task" and "plan" in waiting.value.input_requests


def test_a_bare_request_state_is_echoed_once(serve):
    server = serve["new.example"] = ModernServer()
    client = MCPClient(None)
    assert client.call_tool("https://new.example/mcp", "state_only", {}) == "second time"
    calls = [s for s in server.seen if s["method"] == "tools/call"]
    assert [c["body"]["params"].get("requestState") for c in calls] == [None, "opaque"]
    assert calls[0]["body"]["id"] != calls[1]["body"]["id"], "a retry is a new request id"


def test_a_server_that_rejects_the_version_lists_another(serve):
    class Picky(ModernServer):
        def __call__(self, request):
            body = json.loads(request.content)
            if body["method"] == "server/discover":
                self.seen.append({"method": "server/discover"})
                return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                    "code": -32022, "message": "Unsupported protocol version",
                    "data": {"supported": ["2027-01-01"], "requested": "2026-07-28"}}})
            return LegacyServer("2025-11-25", sessions=False)(request)

    serve["picky.example"] = Picky()
    client = MCPClient(None)
    assert len(client.list_tools("https://picky.example/mcp")) == 2
    assert client.era("https://picky.example/mcp")["modern"] is False


def test_one_era_per_credential_and_a_401_is_raised(serve):
    class Guarded(ModernServer):
        def __call__(self, request):
            if request.headers.get("authorization") != "Bearer good-token":
                return httpx.Response(401, json={"error": "unauthorized"})
            return super().__call__(request)

    serve["auth.example"] = Guarded()
    client = MCPClient(None)
    url = "https://auth.example/mcp"
    assert len(client.list_tools(url, headers={"Authorization": "Bearer good-token"})) == 2
    with pytest.raises(httpx.HTTPStatusError) as refused:
        client.list_tools(url, headers={"Authorization": "Bearer bad-token"})
    assert refused.value.response.status_code == 401


def test_traceparent_rides_in_meta_on_a_modern_call(serve):
    server = serve["new.example"] = ModernServer()
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    MCPClient(None).call_tool("https://new.example/mcp", "zeta", {},
                              call_headers={"traceparent": tp})
    call = next(s for s in server.seen if s["method"] == "tools/call")
    assert call["body"]["params"]["_meta"]["traceparent"] == tp
    assert call["headers"]["traceparent"] == tp


# ── Client ID Metadata Documents (2026-07-28 client registration) ─────────────────────────────

def test_the_client_metadata_url_needs_https_and_a_path(monkeypatch):
    monkeypatch.delenv("BOSS_MCP_OAUTH_CLIENT_METADATA_URL", raising=False)
    cb = "https://boss.example.com/v1/mcp/oauth/callback"
    assert mcp_oauth.client_metadata_url(cb) == \
        "https://boss.example.com/.well-known/boss-mcp-client.json"
    assert mcp_oauth.client_metadata_url("http://localhost:3000/v1/mcp/oauth/callback") is None
    monkeypatch.setenv("BOSS_MCP_OAUTH_CLIENT_METADATA_URL", "https://cdn.example/boss.json")
    assert mcp_oauth.client_metadata_url(cb) == "https://cdn.example/boss.json"
    monkeypatch.setenv("BOSS_MCP_OAUTH_CLIENT_METADATA_URL", "https://cdn.example")
    assert mcp_oauth.client_metadata_url(cb) is None, "a client id URL must have a path"


def test_the_document_names_itself_and_the_callback():
    doc = mcp_oauth.client_metadata_document(
        "https://boss.example.com/.well-known/boss-mcp-client.json",
        ["https://boss.example.com/v1/mcp/oauth/callback"])
    assert doc["client_id"] == "https://boss.example.com/.well-known/boss-mcp-client.json"
    assert doc["client_name"] == "BOSS" and doc["token_endpoint_auth_method"] == "none"
    assert doc["redirect_uris"] == ["https://boss.example.com/v1/mcp/oauth/callback"]
    assert doc["grant_types"] == ["authorization_code", "refresh_token"]
    assert doc["response_types"] == ["code"]


class _Begin:
    def __call__(self):
        import contextlib

        @contextlib.contextmanager
        def cm():
            yield object()
        return cm()


@pytest.fixture()
def prepare_world(monkeypatch):
    """mcp_oauth.prepare with its storage and discovery replaced: what it decides is the test."""
    state: dict = {"auth": {"type": "oauth", "oauth": {}}, "saved": None, "registered": 0,
                   "meta": {}}
    monkeypatch.setattr(mcp_oauth, "_server_and_auth",
                        lambda c, p, s: ({"name": "linear", "url": "https://mcp.linear.app/sse"},
                                         state["auth"]))
    monkeypatch.setattr(mcp_oauth, "discover", lambda url: dict(state["meta"]))

    def register(meta, redirect_uri):
        state["registered"] += 1
        return {"client_id": "dyn-1", "client_secret": None, "token_endpoint_auth_method": "none"}

    monkeypatch.setattr(mcp_oauth, "register_client", register)

    def save(c, p, s, oauth, *, secret_ref=None):
        state["saved"] = (oauth, secret_ref)

    monkeypatch.setattr(mcp_oauth, "_save_oauth", save)
    monkeypatch.setattr(mcp_oauth, "emit_event", lambda *a, **k: None)
    monkeypatch.delenv("BOSS_MCP_OAUTH_CLIENT_METADATA_URL", raising=False)
    base = {"resource": "https://mcp.linear.app/sse",
            "authorization_server": "https://mcp.linear.app",
            "authorization_endpoint": "https://mcp.linear.app/authorize",
            "token_endpoint": "https://mcp.linear.app/token",
            "registration_endpoint": "https://mcp.linear.app/register",
            "token_endpoint_auth_methods": ["none"]}
    state["base"] = base
    return state


CB = "https://boss.example.com/v1/mcp/oauth/callback"


def test_cimd_is_preferred_over_registration_when_advertised(prepare_world):
    prepare_world["meta"] = {**prepare_world["base"], "client_id_metadata_document_supported": True}
    out = mcp_oauth.prepare(_Begin(), None, "p", "srv", redirect_uri=CB, actor="ceo")
    assert out["client_source"] == "cimd"
    assert out["client_id"] == "https://boss.example.com/.well-known/boss-mcp-client.json"
    assert prepare_world["registered"] == 0, "nothing is registered with a metadata document"


def test_without_cimd_support_registration_is_used_as_before(prepare_world):
    prepare_world["meta"] = dict(prepare_world["base"])
    out = mcp_oauth.prepare(_Begin(), None, "p", "srv", redirect_uri=CB, actor="ceo")
    assert out["client_source"] == "dynamic" and prepare_world["registered"] == 1


def test_an_http_install_falls_back_to_registration(prepare_world):
    prepare_world["meta"] = {**prepare_world["base"], "client_id_metadata_document_supported": True}
    out = mcp_oauth.prepare(_Begin(), None, "p", "srv",
                            redirect_uri="http://localhost:3000/v1/mcp/oauth/callback",
                            actor="ceo")
    assert out["client_source"] == "dynamic"


def test_a_typed_client_still_wins(prepare_world):
    prepare_world["auth"] = {"type": "oauth", "oauth": {"client_id": "typed",
                                                         "client_source": "manual"}}
    prepare_world["meta"] = {**prepare_world["base"], "client_id_metadata_document_supported": True}
    out = mcp_oauth.prepare(_Begin(), None, "p", "srv", redirect_uri=CB, actor="ceo")
    assert out["client_source"] == "manual" and out["client_id"] == "typed"
