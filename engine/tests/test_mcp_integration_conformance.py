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
from boss_engine.services.mcp_client import MCPClient, MCPTimeout, MCPToolError

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


def test_the_http_sse_transport_connects_lists_and_calls(fake):
    """Row: the 2024-11-05 HTTP+SSE transport (`https://host/sse`), still what many servers ship.

    FAILED BEFORE: BOSS only POSTed to the URL, so registering an SSE server read
    "405 Method Not Allowed" and it could never be used. Fixed: the spec's backwards-compatible
    probe (a 4xx to POST, then a GET whose first event is `endpoint`) and an SSE session."""
    server = fake("sse")
    _connect_list_call(server.url)
    assert MCPClient(None).era(server.url) == {"modern": False, "version": None}


def test_the_http_sse_transport_carries_the_sign_in(fake):
    server = fake("sse", auth=("bearer", "sse-tok"))
    _connect_list_call(server.url, {"Authorization": "Bearer sse-tok"})
    with pytest.raises(Exception) as refused:
        MCPClient(None).list_tools(server.url, headers={"Authorization": "Bearer wrong"})
    assert "401" in str(refused.value)


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


# --------------------------------------------------------------------------- long calls

@pytest.mark.parametrize("mode", ["streamable", "streamable_sse", "sse"])
def test_a_slow_tool_times_out_in_words_and_is_cancelled(fake, mode):
    """Row: a long-running call past the client's patience.

    FAILED BEFORE: the call died as httpx `ReadTimeout('timed out')` after the 20s handshake
    timeout (a tool call had no longer budget of its own), the server was never told to stop,
    and the registry recorded the whole server unreachable (a transport failure). Fixed: a
    separate call timeout (BOSS_MCP_CALL_TIMEOUT, default 120s), notifications/cancelled with
    the call's id, and MCPTimeout - a sentence, not a transport failure."""
    server = fake(mode)
    client = MCPClient(None, timeout=5, call_timeout=1)
    with pytest.raises(MCPTimeout) as slow:
        client.call_tool(server.url, "slow", {"seconds": 3})
    said = str(slow.value)
    assert "slow did not answer within 1s" in said and "timed out" in said
    assert "BOSS_MCP_CALL_TIMEOUT" in said
    from boss_engine.services.mcp_registry import _is_transport_failure

    assert not _is_transport_failure(slow.value), "a slow tool must not mark the server down"
    for _ in range(50):
        if server.cancelled:
            break
        import time

        time.sleep(0.05)
    called = [s for s in server.seen if s["method"] == "tools/call"]
    assert server.cancelled, "the server was never told BOSS stopped waiting"
    assert len(called) == 1


def test_a_call_gets_more_time_than_a_handshake(fake):
    """The call budget is its own: a 2s tool under a 1s handshake timeout still answers."""
    server = fake("streamable")
    client = MCPClient(None, timeout=1, call_timeout=10)
    assert client.call_tool(server.url, "slow", {"seconds": 2}) == {"slept": True}


# =========================================================================== with a database
# The rows below go through the registry (receipts, reachability, sign-in from the store) and
# the stdio supervisor (templates live in the database). They need BOSS_TEST_DATABASE_URL.

PROJECT = "proj_mcp_conf"
TENANT = ("org_p29b", "ws_org_p29b")


@pytest.fixture(scope="module")
def db():
    from boss_engine.services import mcp_stdio_templates as templates

    from ._p29b_support import drop, echo_template, fresh_engine, make_project

    engine, admin = fresh_engine("boss_test_mcp_conformance")
    make_project(engine, PROJECT)
    with engine.begin() as c:
        templates.create_template(c, PROJECT, echo_template("echo"), actor="op")
    yield engine
    drop(engine, admin)


@pytest.fixture
def settings(db):
    from boss_engine.config import get_settings

    return get_settings().model_copy(update={"database_url": db.url.render_as_string(
        hide_password=False)})


@pytest.fixture
def stdio(monkeypatch):
    from ._p29b_support import stdio_runtime

    yield from stdio_runtime(monkeypatch)


def _register(db, settings, name: str, url: str, **kwargs) -> dict:
    from boss_engine.services import mcp_registry

    with db.begin() as c:
        server = mcp_registry.register_mcp_server(
            c, PROJECT, name, url, transport=kwargs.pop("transport", "http"), risk_tier=0,
            actor="op", settings=settings, **kwargs)
        mcp_registry.review_mcp_server(c, PROJECT, server["id"], actor="op")
        return mcp_registry.get_mcp_server(c, PROJECT, server["id"])


def test_stdio_connects_lists_and_calls_through_the_bridge(db, settings, stdio):
    url = f"stdio://{PROJECT}/echo"
    client = MCPClient(settings)
    assert "echo" in [t["name"] for t in client.list_tools(url)]
    assert client.call_tool(url, "echo", {"text": "hi"}) == {"echo": "hi"}


def test_a_slow_stdio_tool_is_cancelled_by_the_id_the_server_knows(db, settings, stdio):
    """Row: cancellation through the stdio bridge.

    FAILED BEFORE: the bridge renumbers request ids, but forwarded a caller's
    notifications/cancelled untouched, so the server was told to cancel an id it never saw
    (and the bridge's own timeout cancelled nothing). Fixed: the bridge maps the caller's id
    to its own, and cancels on its own timeout too."""
    url = f"stdio://{PROJECT}/echo"
    with pytest.raises(MCPTimeout):
        MCPClient(settings, timeout=10, call_timeout=1).call_tool(url, "sleep", {"seconds": 2})
    import time

    time.sleep(1.5)   # the server finishes its sleep, then reads the cancellation
    ids = MCPClient(settings).call_tool(url, "cancelled", {})["ids"]
    assert ids and all(isinstance(i, int) and i > 1000 for i in ids), ids


def test_a_slow_tool_through_the_registry_does_not_mark_the_server_down(db, settings, fake,
                                                                         monkeypatch):
    """FAILED BEFORE: a timed-out call was a transport failure, so the registry wrote the
    server unreachable and screens and runs skipped a healthy server for a minute."""
    from boss_engine.services import mcp_client as client_module, mcp_registry

    server_http = fake("streamable")
    server = _register(db, settings, "slow-srv", server_http.url)
    monkeypatch.setattr(client_module, "default_call_timeout", lambda: 1.0)
    with db.begin() as c:
        with pytest.raises(MCPTimeout):
            mcp_registry.call_tool_with_receipt(
                c, settings, PROJECT, server, "slow", {"seconds": 3},
                dedupe_key="conf:slow", tenant_scope=TENANT)
    with db.begin() as c:
        after = mcp_registry.get_mcp_server(c, PROJECT, server["id"])
    assert after.get("reachable") is not False
