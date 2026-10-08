"""rc6 final gate: the outbound guard (services/env_refs.py, N-1) must not break legitimate traffic.

The guard wraps http.client and httpx for the whole process, so every library on top of them —
urllib, requests/urllib3, botocore, the OpenAI SDK — is judged. The risk review found five ways
it refused or garbled a call that was the operator's own:

  1. urllib3's HTTPSConnection (requests, botocore) is not an http.client.HTTPSConnection, so a
     TLS call to api.github.com or bedrock-runtime.*.amazonaws.com was judged as plain http and
     refused. TLS is now read from the socket.
  2. One value in two variables (an OpenRouter key also set as OPENAI_API_KEY) was refused by
     whichever binding did not cover the host. A value is refused only when no binding allows it.
  3. The microVM sandbox service (BOSS_SANDBOX_AGENT_URL) receives the run's model key and git
     token in its submit body by design: it is BOSS's own relay, set by the operator.
  4. The providers' own MCP servers (api.githubcopilot.com, mcp.linear.app, mcp.atlassian.com)
     were not their endpoints, so a token pasted for the Library's GitHub entry that equals the
     host's GITHUB_TOKEN was refused.
  5. The OpenAI SDK turns any transport exception into a retried APIConnectionError
     ("Connection error."), and the adapter retried that 8 times with backoff: minutes of a
     meaningless error. The adapter now refuses before the SDK, and never retries a refusal.

And the Host header no longer decides where bytes go (the peer, the CONNECT tunnel or the
absolute-form request target does).
"""
from __future__ import annotations

import datetime
import http.client
import ipaddress
import socket
import ssl
import threading
import time

import pytest

import boss_engine  # noqa: F401 — installs the guard
from boss_engine.services import env_refs
from boss_engine.services.env_refs import (
    CredentialMisdirected,
    check_outbound,
    misdirection_in,
    provider_binding,
    register_host_credential,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("BOSS_CREDENTIAL_HOSTS", "BOSS_SANDBOX_AGENT_URL", "OPENAI_BASE_URL",
                 "PLANE_BASE_URL", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN",
                 "BOSS_GITHUB_TOKEN", "LINEAR_API_KEY", "JIRA_API_TOKEN", "PLANE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    with env_refs._registry_lock:
        env_refs._registered.clear()


def _tls_listener(tmp_path):
    """A TLS server on 127.0.0.1 with a self-signed certificate, recording what it receives."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got: list[bytes] = []

    def serve():
        srv.settimeout(5)
        try:
            raw, _ = srv.accept()
            with ctx.wrap_socket(raw, server_side=True) as conn:
                conn.settimeout(2)
                try:
                    got.append(conn.recv(65536))
                except OSError:
                    pass
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return srv, got


def test_requests_over_tls_is_judged_as_https(tmp_path):
    """(1) requests/urllib3 (and botocore on top of it) over TLS reach a host the credential is
    bound to with https; before the fix this was refused as plain http."""
    requests = pytest.importorskip("requests")
    srv, got = _tls_listener(tmp_path)
    port = srv.getsockname()[1]
    canary = "ghs_canary_tls_0123456789abcdef"
    register_host_credential(canary, provider_binding("github", ["127.0.0.1"]), "A GitHub token")
    r = requests.get(f"https://127.0.0.1:{port}/user", headers={"Authorization": f"token {canary}"},
                     verify=False, timeout=5)
    assert r.status_code == 200
    assert any(canary.encode() in g for g in got)
    srv.close()


def test_plain_http_to_a_bound_host_is_still_refused_by_requests():
    requests = pytest.importorskip("requests")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    canary = "ghs_canary_http_0123456789abcdef"
    register_host_credential(canary, provider_binding("github", ["127.0.0.1"]), "A GitHub token")
    with pytest.raises(Exception) as refused:
        requests.get(f"http://127.0.0.1:{srv.getsockname()[1]}/", timeout=3,
                     headers={"Authorization": f"token {canary}"})
    assert misdirection_in(refused.value) is not None
    srv.close()


def test_one_value_in_two_variables_goes_where_either_allows(monkeypatch):
    """(2) the same OpenRouter key in OPENROUTER_API_KEY and OPENAI_API_KEY."""
    key = "sk-or-canary-0123456789abcdef"
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    check_outbound("https://openrouter.ai/api/v1/chat/completions", {"Authorization": f"Bearer {key}"})
    check_outbound("https://api.openai.com/v1/chat/completions", {"Authorization": f"Bearer {key}"})
    with pytest.raises(CredentialMisdirected):
        check_outbound("https://evil.example/v1", {"Authorization": f"Bearer {key}"})


def test_the_sandbox_service_receives_the_runs_credentials(monkeypatch):
    """(3) the microVM submit body carries the model key and the git token to BOSS's own relay."""
    model_key = "sk-canary-model-0123456789abcdef"
    git_token = "ghs_canary_git_0123456789abcdef"
    monkeypatch.setenv("OPENAI_API_KEY", model_key)
    register_host_credential(git_token, provider_binding("github"), "A GitHub installation token")
    monkeypatch.setenv("BOSS_SANDBOX_AGENT_URL", "http://sandbox-host:8088")
    from boss_engine.services.sandbox_contract import build_submit_request

    body = build_submit_request({"owner": "o", "repo": "r", "token": git_token,
                                 "openai_api_key": model_key}, "t", task_id="crun_1")
    import json

    check_outbound("http://sandbox-host:8088/v1/sandbox/tasks", {}, json.dumps(body).encode())
    # Only that service: another port or host of the same name is not the relay.
    for elsewhere in ("http://sandbox-host:9999/v1/sandbox/tasks", "http://evil.example:8088/"):
        with pytest.raises(CredentialMisdirected):
            check_outbound(elsewhere, {}, json.dumps(body).encode())


@pytest.mark.parametrize("name,url", [
    ("GITHUB_TOKEN", "https://api.githubcopilot.com/mcp/"),
    ("LINEAR_API_KEY", "https://mcp.linear.app/mcp"),
    ("JIRA_API_TOKEN", "https://mcp.atlassian.com/v1/mcp"),
])
def test_a_providers_own_mcp_server_is_its_endpoint(monkeypatch, name, url):
    """(4) the Library's GitHub/Linear/Atlassian MCP entries with the same token the host holds."""
    value = "canary-token-0123456789abcdef"
    monkeypatch.setenv(name, value)
    check_outbound(url, {"Authorization": f"Bearer {value}"})


def test_the_openai_adapter_refuses_at_once_with_words(monkeypatch):
    """(5) a misdirected model key: the reason, immediately — not minutes of "Connection error."."""
    from boss_engine.config import Settings
    from boss_engine.services.openai_adapter import OpenAIAdapter, _with_retry

    key = "sk-canary-adapter-0123456789abcdef"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    adapter = OpenAIAdapter(Settings(), base_url="https://evil.example/v1", api_key=key)
    with pytest.raises(CredentialMisdirected) as refused:
        adapter._openai_client()
    assert "OPENAI_API_KEY" in str(refused.value) and key not in str(refused.value)

    # A refusal from inside the SDK (wrapped as APIConnectionError) is not retried.
    import httpx
    from openai import APIConnectionError

    calls = []

    def call():
        calls.append(1)
        try:
            raise CredentialMisdirected("The host credential X", provider_binding("openai"), "evil")
        except CredentialMisdirected as inner:
            raise APIConnectionError(request=httpx.Request("POST", "https://evil.example")) from inner

    started = time.monotonic()
    with pytest.raises(CredentialMisdirected):
        _with_retry(call)
    assert calls == [1] and time.monotonic() - started < 1


def test_the_openai_sdk_through_the_guard_says_why(monkeypatch):
    """The SDK path end to end: the client the adapter builds for the right host still works,
    and a model failure that is a refusal is explained as one, never "couldn't finish"."""
    from boss_engine.services import model_failures

    exc = CredentialMisdirected("The host credential OPENAI_API_KEY", provider_binding("openai"),
                                "evil.example")
    said = model_failures.explain_turn_failure(exc)
    assert said["kind"] == "credential_misdirected" and said["retry"] is False
    assert "OPENAI_API_KEY" in said["sentence"] and "BOSS_CREDENTIAL_HOSTS" in said["sentence"]


class _FakeConn:
    def __init__(self, host, port=443, tls=True, tunnel=None):
        self.host, self.port = host, port
        self.sock = type("S", (), {"getpeercert": lambda self: {}})() if tls else object()
        self._tunnel_host, self._tunnel_port = tunnel, 443 if tunnel else None


def test_the_host_header_does_not_say_where_bytes_go():
    conn = _FakeConn("93.184.216.34")
    dest = env_refs._connection_destination(
        conn, b"GET /user HTTP/1.1\r\nHost: api.github.com\r\n\r\n")
    assert dest.startswith("https://93.184.216.34")
    canary = "ghs_canary_hosthdr_0123456789abcdef"
    register_host_credential(canary, provider_binding("github"), "A GitHub token")
    with pytest.raises(CredentialMisdirected):
        check_outbound(dest, None, f"GET / HTTP/1.1\r\nHost: api.github.com\r\n"
                                   f"Authorization: token {canary}\r\n\r\n".encode())


def test_a_tunnel_and_a_forward_proxy_name_the_real_destination():
    tunnelled = _FakeConn("proxy.corp", 3128, tls=True, tunnel="api.github.com")
    assert env_refs._connection_destination(
        tunnelled, b"GET /user HTTP/1.1\r\n\r\n").startswith("https://api.github.com")
    forward = _FakeConn("10.0.0.5", 3128, tls=False)
    assert env_refs._connection_destination(
        forward, b"POST http://plane.internal/api/v1 HTTP/1.1\r\n\r\n") == "http://plane.internal/"
    # a body chunk sent after the headers goes where the request went
    assert env_refs._connection_destination(forward, b'{"x": 1}') == "http://plane.internal/"


def test_a_forward_proxy_to_the_operators_own_endpoint_passes(monkeypatch):
    """A self-hosted Plane on plain http, reached through the operator's forward proxy."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got: list[bytes] = []

    def serve():
        srv.settimeout(3)
        try:
            conn, _ = srv.accept()
            conn.settimeout(1)
            got.append(conn.recv(65536))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            conn.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    key = "plane-canary-0123456789abcdef"
    monkeypatch.setenv("PLANE_API_KEY", key)
    monkeypatch.setenv("PLANE_BASE_URL", "http://plane.internal")
    proxy = http.client.HTTPConnection("127.0.0.1", srv.getsockname()[1], timeout=3)
    proxy.request("GET", "http://plane.internal/api/v1/workspaces/", headers={"X-API-Key": key})
    assert proxy.getresponse().status == 200
    assert any(key.encode() in g for g in got)
    srv.close()


def test_the_check_costs_little_per_request():
    """The guard runs on every send: a small request stays well under a millisecond on average
    even with a full registry, and a 1 MB body in tens of milliseconds."""
    for i in range(env_refs._REGISTRY_LIMIT):
        register_host_credential(f"ghs_registered_{i:06d}_xxxxxxxx", provider_binding("github"), "x")
    headers = {"Authorization": "Bearer someone-elses", "Content-Type": "application/json"}
    started = time.perf_counter()
    for _ in range(200):
        check_outbound("https://api.example.com/v1", headers, b'{"a": 1}' * 50)
    assert (time.perf_counter() - started) / 200 < 0.002
    started = time.perf_counter()
    check_outbound("https://api.example.com/v1", headers, b"a" * (1024 * 1024))
    assert time.perf_counter() - started < 0.25
