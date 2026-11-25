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

from ._mcp_fakes import DEFAULT_TOOLS, FakeConnectProxy, FakeMcp, FakeProxy, make_tls

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


def test_an_extra_ca_lets_boss_trust_an_internal_https_server(fake, monkeypatch, tmp_path):
    """Row: an HTTPS server whose certificate only the company's CA vouches for. with-extra-ca
    exports the combined bundle as SSL_CERT_FILE; both roads (httpx's own, and outbound_proxy's
    when a proxy is configured) must honour it. Without it, the words name BOSS_EXTRA_CA_FILE."""
    tls, ca = make_tls(tmp_path)
    server = fake("streamable", tls=tls)
    with pytest.raises(Exception) as untrusted:
        MCPClient(None).list_tools(server.url)
    words = mcp_client.describe_failure(untrusted.value, server.url)
    assert "BOSS_EXTRA_CA_FILE" in words and "could not connect" in words
    monkeypatch.setenv("SSL_CERT_FILE", ca)
    _connect_list_call(server.url)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:8080")
    mcp_client.forget_sessions()
    _connect_list_call(server.url)


def test_https_through_an_inspecting_proxy_with_the_company_ca(fake, monkeypatch, tmp_path):
    """Row: an outside HTTPS MCP server, reached only through the company proxy, presenting a
    certificate the company CA signed (what a TLS-inspecting gateway does)."""
    tls, ca = make_tls(tmp_path, names=("mcp.partner.example",))
    server = fake("streamable", tls=tls)
    proxy = FakeConnectProxy()
    try:
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        monkeypatch.setenv("SSL_CERT_FILE", ca)
        _connect_list_call(server.url_on("mcp.partner.example"))
        assert proxy.connects and proxy.connects[0].startswith("mcp.partner.example:")
    finally:
        proxy.close()


def test_oauth_discovery_and_refresh_go_through_the_proxy(fake, monkeypatch, tmp_path):
    """Row: OAuth (sign-in discovery, token exchange, refresh) behind the company proxy.

    FAILED BEFORE: identity.http (the SSRF-safe client every OAuth request uses) only ever
    connected directly to the address it resolved itself, so where the proxy is the only way
    out an outside authorization server answered "cannot resolve host" / "connection failed
    (TimeoutError)" while the MCP calls themselves worked. Fixed: HTTPS through the proxy with
    CONNECT, by NO_PROXY; TLS still verified against the host name."""
    import json as _json

    from boss_engine.identity import http as safe_http

    tls, ca = make_tls(tmp_path, names=("auth.partner.example",))
    server = fake("streamable", tls=tls)
    proxy = FakeConnectProxy()

    def no_dns(host, port):
        raise OSError("this machine cannot resolve outside names")

    try:
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        monkeypatch.setenv("SSL_CERT_FILE", ca)
        monkeypatch.setattr(safe_http, "_resolve", no_dns)
        body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        answer = safe_http.fetch("POST", server.url_on("auth.partner.example"),
                                 headers={"Content-Type": "application/json"}, data=body,
                                 purpose="mcp.oauth")
        assert answer.status == 200 and answer.json()["result"]["tools"]
        assert proxy.connects == [f"auth.partner.example:{server.port}"]
        from boss_engine.services import mcp_oauth

        assert mcp_oauth.guard_url(server.url_on("auth.partner.example"), what="token endpoint")
        # NO_PROXY keeps an internal authorization server off the proxy, and then it must
        # resolve here as before.
        monkeypatch.setenv("NO_PROXY", ".partner.example")
        with pytest.raises(safe_http.OutboundRefused, match="cannot resolve"):
            safe_http.fetch("GET", server.url_on("auth.partner.example"), purpose="mcp.oauth")
        # A proxy that refuses the tunnel says so.
        monkeypatch.delenv("NO_PROXY")
        proxy.refuse = True
        with pytest.raises(safe_http.OutboundRefused, match="proxy refused the tunnel"):
            safe_http.fetch("GET", server.url_on("auth.partner.example"), purpose="mcp.oauth")
    finally:
        proxy.close()


# --------------------------------------------------------------------------- plain words

def _probe_error(url: str, headers=None) -> str:
    from boss_engine.services import mcp_registry

    found = mcp_registry.probe(None, url, timeout=3, headers=headers)
    assert found["reachable"] is False
    return found["error"]


def test_failures_read_as_words_with_the_next_step(fake, monkeypatch):
    """Row: every failure an operator meets on Save & test, in words and with what to do.

    FAILED BEFORE: "401 Unauthorized" (no next step), "405 Method Not Allowed",
    "ConnectError: [Errno 61] Connection refused", "ConnectError: [Errno 8] nodename nor
    servname provided", "Timed out" - and a failed CALL reached the agent as httpx's own text
    ("Client error '401 Unauthorized' for url ... developer.mozilla.org ...")."""
    server = fake("streamable", auth=("bearer", "right"))
    said = _probe_error(server.url, {"Authorization": "Bearer wrong"})
    assert said.startswith("401") and "Next:" in said and "token" in said
    wrong_path = server.url.replace("/mcp", "/nope")
    said = _probe_error(wrong_path, {"Authorization": "Bearer right"})
    assert said.startswith("404") and "/mcp" in said and "/sse" in said
    import socket

    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        free = spare.getsockname()[1]
    said = _probe_error(f"http://127.0.0.1:{free}/mcp")
    assert "connection refused" in said and "host.docker.internal" in said
    said = _probe_error("http://no-such-host.invalid/mcp")
    assert "cannot resolve" in said and "no-such-host.invalid" in said
    proxy = FakeProxy(refuse=True)
    try:
        monkeypatch.setenv("HTTP_PROXY", proxy.url)
        said = _probe_error(server.url_on("mcp.partner.example"))
        assert said.startswith("403")
    finally:
        proxy.close()


def test_a_failed_call_is_raised_in_words_and_not_retried_blind():
    import httpx

    request = httpx.Request("POST", "https://mcp.vendor.example/mcp")
    refused = httpx.HTTPStatusError("x", request=request,
                                    response=httpx.Response(401, request=request))
    plain = mcp_client.failed_call(refused)
    assert isinstance(plain, mcp_client.MCPCallFailed) and plain.retryable is False
    assert "developer.mozilla.org" not in str(plain) and "Next:" in str(plain)
    timeout = MCPTimeout("slow", 1)
    assert mcp_client.failed_call(timeout) is timeout and timeout.retryable is False
    from boss_engine.services.durable_operations import ambiguous_outcome

    assert ambiguous_outcome({"type": "MCPTimeout", "message": str(timeout)})
    assert not ambiguous_outcome({"type": "MCPCallFailed", "message": str(plain)})


# --------------------------------------------------------------------------- schema quirks

QUIRKY_TOOLS = [
    {"name": "no_description", "inputSchema": {"type": "object"}},
    {"name": "null_description", "description": None, "inputSchema": {}},
    {"name": "no_type_root", "description": "properties only",
     "inputSchema": {"properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "refs", "description": "pydantic style",
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"filter": {"$ref": "#/$defs/Filter"},
                                    "tree": {"$ref": "#/definitions/Node"}},
                     "$defs": {"Filter": {"type": "object", "description": "a filter",
                                          "properties": {"field": {"type": "string"},
                                                         "op": {"oneOf": [
                                                             {"const": "eq"},
                                                             {"const": "ne"}]}}}},
                     "definitions": {"Node": {"type": "object", "properties": {
                         "child": {"$ref": "#/definitions/Node"}}}}}},
    {"name": "root_union", "description": "one of two shapes",
     "inputSchema": {"oneOf": [
         {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
         {"type": "object", "properties": {"name": {"type": "string"}},
          "required": ["name"]}]}},
    {"name": "huge", "description": "x" * 30000, "inputSchema": {
        "type": "object", "properties": {f"p{i}": {"type": "string", "description": "d" * 200}
                                         for i in range(400)}}},
]


def _offered(schema: dict) -> None:
    """What every provider requires of a function's parameters."""
    import json

    assert schema["type"] == "object" and isinstance(schema.get("properties"), dict)
    text = json.dumps(schema)
    assert '"$ref"' not in text and '"$defs"' not in text and '"definitions"' not in text
    assert '"oneOf"' not in text
    for name in schema.get("required") or []:
        assert name in schema["properties"]


def test_quirky_schemas_are_discovered_and_offered_as_every_model_accepts(fake):
    """Row: tool discovery and schema quirks — missing/None descriptions, no object root,
    $ref into $defs/definitions (with a cycle), oneOf, a root union, a very large schema.

    FAILED BEFORE: the inputSchema went to the model untouched, so one such tool made an
    OpenAI-compatible API refuse the whole turn ("schema must be a JSON Schema of 'type:
    object'"), and a None description reached the model as the word "None"."""
    from boss_engine.services import mcp_registry, tool_schema

    server = fake("streamable", tools=QUIRKY_TOOLS)
    found = mcp_registry.probe(None, server.url)
    assert found["reachable"] and len(found["tools"]) == len(QUIRKY_TOOLS)
    huge = next(t for t in found["tools"] if t["name"] == "huge")
    assert len(huge["inputSchema"]["properties"]) == 400, "a large schema is kept whole"
    for tool in MCPClient(None).list_tools(server.url):
        _offered(tool_schema.for_model(tool.get("inputSchema")))
        said = tool_schema.description_for_model(tool)
        assert said and said != "None"
    refs = tool_schema.for_model(QUIRKY_TOOLS[3]["inputSchema"])
    assert refs["properties"]["filter"]["properties"]["field"] == {"type": "string"}
    assert refs["properties"]["filter"]["properties"]["op"]["anyOf"]
    assert refs["properties"]["tree"]["properties"]["child"]["type"] == "object"
    union = tool_schema.for_model(QUIRKY_TOOLS[4]["inputSchema"])
    assert set(union["properties"]) == {"id", "name"} and "required" not in union
    # Pure: the stored schema (and so its hash) is untouched.
    assert "$defs" in QUIRKY_TOOLS[3]["inputSchema"]


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
    assert "did not answer within 1s" in said and "timed out" in said
    assert "BOSS_MCP_CALL_TIMEOUT" in said
    assert slow.value.retryable is False and slow.value.outcome == "unknown_outcome"
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


def test_a_networked_stdio_server_starts_with_the_company_proxy_and_mirrors(monkeypatch):
    """Row: stdio via npx/uvx/docker behind the company network.

    FAILED BEFORE: a template with a network got only its own allowlisted variables, so an
    `npx -y` or `uvx` server behind a proxy-only network with an internal mirror could not
    download itself. Now the proxy and mirror settings travel by name (never in argv)."""
    from boss_engine.services import mcp_stdio_supervisor as sup

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,10.0.0.0/8")
    monkeypatch.setenv("BOSS_NPM_REGISTRY", "https://artifacts.corp.example/api/npm/npm/")
    monkeypatch.setenv("BOSS_PIP_INDEX_URL", "https://artifacts.corp.example/api/pypi/simple")
    template = {"id": "t1", "name": "fetch", "image": "node:22", "network": "bridge",
                "argv": ["npx", "-y", "@acme/mcp-server"], "env_allowlist": ["API_TOKEN"],
                "env_from_auth": "API_TOKEN"}
    argv, env = sup.launch_plan(template, "s3cret-token", mode="docker", name="c1")
    passed = {argv[i + 1] for i, a in enumerate(argv) if a == "--env"}
    assert {"HTTPS_PROXY", "NO_PROXY", "npm_config_registry", "PIP_INDEX_URL",
            "UV_INDEX_URL", "API_TOKEN"} <= passed
    assert env["npm_config_registry"].startswith("https://artifacts.corp.example")
    assert not any("s3cret" in a or "proxy.corp" in a for a in argv), "values never in argv"
    offline = {**template, "network": "none"}
    argv, _ = sup.launch_plan(offline, None, mode="docker", name="c2")
    assert "HTTPS_PROXY" not in argv and "npm_config_registry" not in argv


def test_inside_a_coding_build_the_gateway_reaches_an_sse_server_and_says_failures_plainly(
        db, settings, fake):
    """Row: use inside a coding build. The harness's MCP traffic goes through BOSS's gateway,
    which calls the upstream with BOSS's client: an HTTP+SSE server works there too, and a
    failure reaches the coder as words, not httpx's text."""
    from boss_engine.services import mcp_gateway

    upstream = fake("sse", auth=("header", "X-API-Key", "k-1"))
    server = _register(db, settings, "sse-srv", upstream.url,
                       auth={"type": "header", "header_name": "X-API-Key", "secret": "k-1"})
    broken = _register(db, settings, "down-srv", "http://127.0.0.1:9/mcp")
    gateway = mcp_gateway.McpGateway(db=lambda: db, settings=settings, poll_interval=0.05,
                                     keepalive=0.1, ask_timeout=5)
    try:
        token, base = gateway.issue(PROJECT, {
            "sse": {"server_id": server["id"], "granted_tools": []},
            "down": {"server_id": broken["id"], "granted_tools": []}},
            role_key="forge", run_id="crun_conf")
        harness = MCPClient(settings)
        assert "echo" in [t["name"] for t in harness.list_tools(f"{base}/sse")]
        assert harness.call_tool(f"{base}/sse", "echo", {"text": "via gateway"}) == {
            "echo": "via gateway"}
        with pytest.raises(MCPToolError) as failed:
            harness.call_tool(f"{base}/down", "echo", {"text": "x"})
        assert "connection refused" in str(failed.value)
        assert "developer.mozilla.org" not in str(failed.value)
    finally:
        gateway.stop()



def test_a_timeout_is_never_retried_on_the_unattended_road(fake):
    """Review of #331: the class itself says retryable False, so no road retries a call that
    may have run (skill_runtime reads `retryable`; call_tool_unattended raises it as is)."""
    from boss_engine.services import mcp_registry

    server = fake("streamable")
    row = {"id": "s1", "name": "slow-srv", "url": server.url, "risk_tier": 0,
           "provenance": {"trusted": True}, "tools": DEFAULT_TOOLS}
    with pytest.raises(MCPTimeout) as slow:
        mcp_registry.call_tool_unattended(None, row, "slow", {"seconds": 3}, timeout=1)
    assert MCPTimeout.retryable is False and slow.value.retryable is False
    assert [s["method"] for s in server.seen].count("tools/call") == 1


@pytest.mark.parametrize("make", ["timeout", "read", "write", "pool", "500"])
def test_an_ambiguous_failure_never_reads_as_did_not_run(make, monkeypatch):
    """Review of #331: durable_operations decides "did not run" (safe to retry an irreversible
    call) from words. A server on port 4010 and a tool named not_found_check must not trip
    the 401/404/"not found" marks; and the structured code says unknown_outcome."""
    import httpx

    from boss_engine.services.durable_operations import ambiguous_outcome

    url = "http://mcp.example:4010/not_found_check/mcp"
    request = httpx.Request("POST", url)
    error = {"timeout": MCPTimeout("not_found_check", 5),
             "read": httpx.ReadTimeout("timed out", request=request),
             "write": httpx.WriteTimeout("timed out", request=request),
             "pool": httpx.PoolTimeout("timed out", request=request),
             "500": httpx.HTTPStatusError("x", request=request,
                                          response=httpx.Response(500, request=request))}[make]
    failed = mcp_client.failed_call(error, url)
    assert getattr(failed, "outcome", None) == "unknown_outcome"
    assert getattr(failed, "retryable", True) is False
    words = str(failed)
    assert "4010" not in words and "not_found" not in words
    assert ambiguous_outcome({"type": type(failed).__name__, "message": words})
    assert ambiguous_outcome({"type": "ReadTimeout",
                              "message": f"{url} not_found_check port 4010 timed out"})


def test_whole_word_did_not_run_marks_still_work():
    from boss_engine.services.durable_operations import ambiguous_outcome

    assert not ambiguous_outcome({"type": "HTTPStatusError", "message": "404 Not Found"})
    assert not ambiguous_outcome({"type": "X", "message": "timed out after 401 Unauthorized"})


def test_a_proxy_only_internal_name_is_refused_unless_allow_listed(monkeypatch):
    """Review of #331: behind a proxy, a name that does not resolve here is left to the proxy -
    but not an internal-looking one (single label, .internal/.local/.corp/.lan ...): a hostile
    MCP server's OAuth metadata must not steer BOSS at the company's own hosts."""
    from boss_engine.identity import http as safe_http
    from boss_engine.services import mcp_oauth

    def no_dns(host, port):
        raise OSError("no such name here")

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(safe_http, "_resolve", no_dns)
    monkeypatch.setattr(safe_http, "_allowed_private_hosts", lambda: frozenset())
    for url in ("https://vault.internal/token", "https://jenkins/token",
                "https://idp.corp/token", "https://nas.local/x", "https://db.lan/x"):
        with pytest.raises(safe_http.OutboundRefused, match="cannot resolve"):
            safe_http.fetch("GET", url, purpose="mcp.oauth")
        with pytest.raises(mcp_oauth.UnsafeDestination):
            mcp_oauth.guard_url(url, what="token endpoint")
    assert mcp_oauth.guard_url("https://auth.vendor.example/token", what="token endpoint")
    monkeypatch.setattr(safe_http, "_allowed_private_hosts",
                        lambda: frozenset({"idp.corp"}))
    assert mcp_oauth.guard_url("https://idp.corp/token", what="token endpoint")
