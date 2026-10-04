"""BOSS's MCP client speaks Streamable HTTP, and still speaks plain JSON (skills-mcp E1, test 1).

THE GAP. MCPClient POSTed JSON with `Accept: application/json` only, sent no session id, could
not read an SSE answer and took no auth — so not one hosted MCP server (GitHub, Linear, Sentry,
Context7, Exa) could be used. These fakes are servers of both kinds, driven through httpx's own
transport so the client's real request and parsing code runs.
"""
from __future__ import annotations

import json

import httpx
import pytest

from boss_engine.services import mcp_client
from boss_engine.services.mcp_client import MCPClient, MCPToolError, forget_sessions

TOOLS = [{"name": "get-library-docs", "description": "docs", "inputSchema": {"type": "object"}}]


class StreamableServer:
    """Answers every request as an SSE stream and insists on its session after initialize."""

    def __init__(self, *, token: str | None = None):
        self.seen: list[dict] = []
        self.token = token
        self.session = "sess-42"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.seen.append({"method": body.get("method"), "headers": dict(request.headers)})
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        assert "text/event-stream" in request.headers.get("accept", "")
        method = body.get("method")
        if method == "initialize":
            return self._sse(body["id"], {"protocolVersion": "2025-06-18", "capabilities": {}},
                             {"Mcp-Session-Id": self.session})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if request.headers.get("mcp-session-id") != self.session:
            return httpx.Response(400, json={"error": "no session"})
        if method == "tools/list":
            return self._sse(body["id"], {"tools": TOOLS})
        if method == "tools/call":
            return self._sse(body["id"], {"content": [
                {"type": "text", "text": json.dumps({"echo": body["params"]["arguments"]})}]})
        return httpx.Response(404)

    @staticmethod
    def _sse(rid, result, headers=None) -> httpx.Response:
        stream = (": keep-alive\n\nevent: message\n"
                  f"data: {json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': result})}\n\n")
        return httpx.Response(200, text=stream,
                              headers={"content-type": "text/event-stream", **(headers or {})})


class JsonOnlyServer:
    """BOSS's own tools: plain JSON, no sessions."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2024-11-05", "capabilities": {}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": TOOLS}})
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": "plain words"}], "isError": False}})
        return httpx.Response(404)


@pytest.fixture()
def serve(monkeypatch):
    forget_sessions()

    def install(handler):
        transport = httpx.MockTransport(handler)

        def post(url, **kwargs):
            kwargs.pop("timeout", None)
            with httpx.Client(transport=transport) as client:
                return client.post(url, **kwargs)

        monkeypatch.setattr(mcp_client.httpx, "post", post)
        return handler

    yield install
    forget_sessions()


def test_a_streamable_server_lists_its_tools_and_keeps_the_session(serve):
    server = serve(StreamableServer())
    client = MCPClient(None)
    assert [t["name"] for t in client.list_tools("https://mcp.example/mcp")] == \
        ["get-library-docs"]
    out = client.call_tool("https://mcp.example/mcp", "get-library-docs", {"id": "react"})
    assert out == {"echo": {"id": "react"}}
    call = next(s for s in server.seen if s["method"] == "tools/call")
    assert call["headers"]["mcp-session-id"] == "sess-42", "the session was not echoed"
    assert call["headers"]["mcp-protocol-version"] == "2025-06-18"
    # One initialize for both calls: the session is reused.
    assert [s["method"] for s in server.seen].count("initialize") == 1


def test_auth_headers_are_sent_and_a_wrong_token_is_a_401(serve):
    server = serve(StreamableServer(token="t0ken-abcd"))
    client = MCPClient(None)
    tools = client.list_tools("https://mcp.example/mcp",
                              headers={"Authorization": "Bearer t0ken-abcd"})
    assert tools and all(s["headers"].get("authorization") == "Bearer t0ken-abcd"
                         for s in server.seen)
    forget_sessions()
    with pytest.raises(httpx.HTTPStatusError) as error:
        client.list_tools("https://mcp.example/mcp", headers={"Authorization": "Bearer nope"})
    assert error.value.response.status_code == 401


def test_a_json_only_server_still_works(serve):
    serve(JsonOnlyServer())
    client = MCPClient(None)
    assert [t["name"] for t in client.list_tools("http://mcp-tools:8770/mcp")] == \
        ["get-library-docs"]
    assert client.call_tool("http://mcp-tools:8770/mcp", "get-library-docs", {}) == \
        {"text": "plain words"}


def test_a_tool_that_reports_an_error_raises_in_its_own_words(serve):
    def handler(request):
        body = json.loads(request.content or b"{}")
        if body.get("method") == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": "rate limited"}], "isError": True}})
        return JsonOnlyServer()(request)

    serve(handler)
    with pytest.raises(MCPToolError, match="rate limited"):
        MCPClient(None).call_tool("http://x/mcp", "t", {})
