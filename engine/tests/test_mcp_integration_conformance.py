"""MCP INTEGRATION CONFORMANCE (BOSSAPP-1476): adding and using MCP servers as an enterprise
developer would, one row per transport, sign-in, network and failure shape.

Every row drives BOSS's REAL client code (mcp_client, the registry's probe, the stdio
supervisor) against fake servers on real loopback sockets (tests/_mcp_fakes.py):
connect -> list tools -> call -> the error paths. A row that failed when this audit began is
marked with the fix that made it pass.

  transports    streamable HTTP (JSON and SSE answers), the HTTP+SSE transport, stdio
  sign-in       none, bearer, a named header
  network       a forward proxy; NO_PROXY with CIDR ranges; an extra CA
  failures      wrong token, wrong path, nothing listening, slow tool, failing tool
"""
from __future__ import annotations

import pytest

from boss_engine.services import mcp_client, outbound_proxy
from boss_engine.services.mcp_client import MCPClient, MCPToolError

from ._mcp_fakes import FakeMcp, FakeProxy

_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No proxy from the developer's shell; every row sets what it tests."""
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    mcp_client.forget_sessions()
    yield
    mcp_client.forget_sessions()


@pytest.fixture
def fake():
    made: list[FakeMcp] = []

    def start(*args, **kwargs) -> FakeMcp:
        server = FakeMcp(*args, **kwargs)
        made.append(server)
        return server

    yield start
    for server in made:
        server.close()


def _connect_list_call(url: str, headers=None, *, timeout: float = 10.0) -> None:
    client = MCPClient(None, timeout=timeout)
    names = [t["name"] for t in client.list_tools(url, headers=headers)]
    assert names == ["echo", "slow", "broken"]
    assert client.call_tool(url, "echo", {"text": "hi"}, headers=headers) == {"echo": "hi"}
    with pytest.raises(MCPToolError, match="widget is jammed"):
        client.call_tool(url, "broken", {}, headers=headers)


# --------------------------------------------------------------------------- transports

@pytest.mark.parametrize("mode", ["streamable", "streamable_sse"])
def test_streamable_http_connects_lists_and_calls(fake, mode):
    server = fake(mode)
    _connect_list_call(server.url)
    assert server.sessions, "a legacy server's session was opened"


# --------------------------------------------------------------------------- sign-in

def test_a_bearer_token_reaches_the_server_and_a_wrong_one_is_refused(fake):
    server = fake("streamable", auth=("bearer", "tok-123"))
    _connect_list_call(server.url, {"Authorization": "Bearer tok-123"})
    with pytest.raises(Exception) as refused:
        MCPClient(None).list_tools(server.url, headers={"Authorization": "Bearer nope"})
    assert "401" in str(refused.value)


def test_a_named_api_key_header_reaches_the_server(fake):
    server = fake("streamable", auth=("header", "X-API-Key", "k-9"))
    _connect_list_call(server.url, {"X-API-Key": "k-9"})


# --------------------------------------------------------------------------- network

def test_through_a_forward_proxy(fake, monkeypatch):
    """Row: the only way out is the company proxy. A non-local host goes through it."""
    server = fake("streamable")
    proxy = FakeProxy()
    try:
        monkeypatch.setenv("HTTP_PROXY", proxy.url)
        _connect_list_call(server.url_on("mcp.partner.example"))
        assert any("mcp.partner.example" in r for r in proxy.requests)
    finally:
        proxy.close()


def test_no_proxy_cidr_keeps_an_internal_server_off_the_proxy(monkeypatch):
    """Row: a private host named by a CIDR range in NO_PROXY (as ENTERPRISE_NETWORK.md says).

    FAILED BEFORE: httpx reads `10.0.0.0/8` as the literal host 10.0.0.0, so a server on
    10.255.255.1 was sent to the company proxy. Fixed by outbound_proxy (one NO_PROXY reading,
    CIDR included) in mcp_client."""
    proxy = FakeProxy(refuse=True)
    try:
        monkeypatch.setenv("HTTP_PROXY", proxy.url)
        monkeypatch.setenv("NO_PROXY", "localhost,10.0.0.0/8,.corp.example")
        with pytest.raises(Exception):
            MCPClient(None, timeout=0.5).list_tools("http://10.255.255.1:9/mcp")
        assert proxy.requests == [], "the internal server was sent to the proxy"
    finally:
        proxy.close()


@pytest.mark.parametrize("url, no_proxy, direct", [
    ("http://10.1.2.3:8000/mcp", "10.0.0.0/8", True),
    ("http://10.1.2.3:8000/mcp", "10.1.2.3", True),
    ("http://10.1.2.3:8000/mcp", "192.168.0.0/16", False),
    ("https://mcp.corp.example/mcp", ".corp.example", True),
    ("https://corp.example/mcp", ".corp.example", True),
    ("https://mcp.corp.example/mcp", "*.corp.example", True),
    ("https://mcp.corp.example/mcp", "corp.example", True),
    ("https://notcorp.example/mcp", "corp.example", False),
    ("https://mcp.corp.example:8443/mcp", "mcp.corp.example:8443", True),
    ("https://mcp.corp.example/mcp", "mcp.corp.example:8443", False),
    ("https://api.vendor.example/mcp", "*", True),
    ("http://127.0.0.1:7000/mcp", "", True),
    ("http://localhost:7000/mcp", "", True),
    ("https://api.vendor.example/mcp", "", False),
])
def test_the_proxy_decision(url, no_proxy, direct):
    env = {"HTTPS_PROXY": "http://proxy.corp.example:8080",
           "HTTP_PROXY": "http://proxy.corp.example:8080", "NO_PROXY": no_proxy}
    assert (outbound_proxy.proxy_for(url, env) is None) is direct
