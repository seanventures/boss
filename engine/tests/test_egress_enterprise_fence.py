"""The sandbox fence reaches a company's internal registries, and says why when it refuses.

Before: BOSS_EGRESS_CONNECT_ALLOW REPLACED the built-in list (a missed model host silently broke
model calls), only port 443 was allowed, and any name resolving to a private address was refused
even when listed — which rules out most on-premises Artifactory/Nexus hosts. A refusal was a bare
403 that npm reports as "E403 ... forbidden by your security policy" (a registry token problem, to
anyone reading it), logged without the host. There was no way through an outbound proxy.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.egress.boss_egress import connect  # noqa: E402

REAL_HOST_GATEWAY_ADDRESSES = connect.host_gateway_addresses

def _addr(ip: str, port: int = 443):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("BOSS_EGRESS_CONNECT_ALLOW", "BOSS_EGRESS_CONNECT_DEFAULTS", "BOSS_EGRESS_PRIVATE_ALLOW",
                 "BOSS_NPM_REGISTRY", "BOSS_PIP_INDEX_URL", "BOSS_GOPROXY", "BOSS_MAVEN_MIRROR",
                 "BOSS_PLAYWRIGHT_DOWNLOAD_HOST", "BOSS_GOSUMDB", "BOSS_UPSTREAM_PROXY",
                 "BOSS_UPSTREAM_NO_PROXY", "BOSS_EGRESS_STRICT_SECRETS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(connect, "toolchain_hosts", lambda: ())
    monkeypatch.setattr(connect, "own_networks", lambda: [__import__("ipaddress").ip_network("172.18.0.0/16")])
    if hasattr(connect, "host_gateway_addresses"):
        monkeypatch.setattr(connect, "host_gateway_addresses", lambda: set())


def _resolve(monkeypatch, table):
    def fake(host, port, type=0):
        if host not in table:
            raise socket.gaierror("no such host")
        return [_addr(ip, port) for ip in table[host]]
    monkeypatch.setattr(connect.socket, "getaddrinfo", fake)


def _decide(host, port=443):
    return connect.decide(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())


def test_the_allow_setting_adds_to_the_built_in_hosts(monkeypatch):
    _resolve(monkeypatch, {"api.anthropic.com": ["104.18.0.1"], "artifacts.example.com": ["93.184.216.34"]})
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "artifacts.example.com")
    assert _decide("artifacts.example.com")[1] == ""
    assert _decide("api.anthropic.com")[1] == ""          # a model host is NOT dropped
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_DEFAULTS", "off")
    assert _decide("api.anthropic.com")[1] == "host_not_allowed"
    assert _decide("artifacts.example.com")[1] == ""


def test_an_entry_opens_only_the_port_it_names(monkeypatch):
    _resolve(monkeypatch, {"nexus.example.com": ["93.184.216.34"]})
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "nexus.example.com:8443")
    assert _decide("nexus.example.com", 8443)[1] == ""
    assert _decide("nexus.example.com", 443)[1] == "port_not_allowed"
    assert _decide("nexus.example.com", 22)[1] == "port_not_allowed"


def test_configured_registries_are_allowed_without_listing_them_again(monkeypatch):
    _resolve(monkeypatch, {"npm.example.com": ["93.184.216.34"], "pypi.example.com": ["93.184.216.35"]})
    monkeypatch.setenv("BOSS_NPM_REGISTRY", "https://npm.example.com/api/npm/virtual/")
    monkeypatch.setenv("BOSS_PIP_INDEX_URL", "https://pypi.example.com:8443/simple")
    assert _decide("npm.example.com")[1] == ""
    assert _decide("pypi.example.com", 8443)[1] == ""
    assert _decide("pypi.example.com", 443)[1] == "port_not_allowed"


def test_an_internal_registry_on_a_private_address_is_an_explicit_exact_opt_in(monkeypatch):
    _resolve(monkeypatch, {"nexus.corp.example.com": ["10.20.30.40"],
                           "meta.corp.example.com": ["169.254.169.254"],
                           "loop.corp.example.com": ["127.0.0.1"],
                           "boss.corp.example.com": ["172.18.0.5"],     # BOSS's own network
                           "other.corp.example.com": ["10.20.30.41"]})
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "*.corp.example.com")
    assert _decide("nexus.corp.example.com")[1] == "not_a_public_address"   # allowlisted is not enough
    monkeypatch.setenv("BOSS_EGRESS_PRIVATE_ALLOW",
                       "nexus.corp.example.com:8081,nexus.corp.example.com,meta.corp.example.com,"
                       "loop.corp.example.com,boss.corp.example.com")
    assert _decide("nexus.corp.example.com", 8081)[1] == ""
    assert _decide("nexus.corp.example.com")[1] == ""
    assert _decide("other.corp.example.com")[1] == "not_a_public_address"
    for never in ("meta.corp.example.com", "loop.corp.example.com", "boss.corp.example.com"):
        assert _decide(never)[1] == "not_a_public_address", never


def test_a_private_opt_in_never_reaches_the_host_gateway_or_ipv6_ula(monkeypatch):
    """host.docker.internal / host-gateway reach the host's own listeners, and fd00:ec2::254 is a
    cloud metadata service: never through the private opt-in (review N4)."""
    monkeypatch.setattr(connect, "host_gateway_addresses", lambda: {"192.168.65.254", "172.17.0.1"})
    _resolve(monkeypatch, {"a.corp.example.com": ["192.168.65.254"], "b.corp.example.com": ["172.17.0.1"],
                           "c.corp.example.com": ["fd00:ec2::254"], "d.corp.example.com": ["fe80::1"],
                           "e.corp.example.com": ["10.9.9.9"]})
    monkeypatch.setenv("BOSS_EGRESS_PRIVATE_ALLOW",
                       "a.corp.example.com,b.corp.example.com,c.corp.example.com,d.corp.example.com,e.corp.example.com")
    for host in "abcd":
        assert _decide(f"{host}.corp.example.com")[1] == "not_a_public_address", host
    assert _decide("e.corp.example.com")[1] == ""


def test_the_host_gateway_is_found_from_the_routing_table_and_docker_names(monkeypatch, tmp_path):
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                     "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n")
    monkeypatch.setattr(connect, "ROUTE_FILE", str(route))
    monkeypatch.setattr(connect.socket, "gethostbyname_ex",
                        lambda name: (name, [], ["192.168.65.254"]) if name == "host.docker.internal"
                        else (_ for _ in ()).throw(socket.gaierror()))
    assert {"172.18.0.1", "192.168.65.254"} <= REAL_HOST_GATEWAY_ADDRESSES()


def test_an_old_ip_entry_still_works(monkeypatch):
    _resolve(monkeypatch, {"93.184.216.34": ["93.184.216.34"]})
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "93.184.216.34")
    assert _decide("93.184.216.34")[1] == ""


def test_a_wildcard_or_a_service_name_is_never_a_private_opt_in(monkeypatch):
    _resolve(monkeypatch, {"x.corp.example.com": ["10.0.0.9"], "db": ["172.18.0.2"]})
    monkeypatch.setenv("BOSS_EGRESS_PRIVATE_ALLOW", "*.corp.example.com,db")
    assert _decide("x.corp.example.com")[1] == "host_not_allowed"
    assert _decide("db")[1] == "host_not_allowed"


def test_github_never_goes_through_the_tunnel_whatever_the_settings(monkeypatch):
    _resolve(monkeypatch, {"api.github.com": ["140.82.112.6"]})
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "api.github.com,*.github.com")
    monkeypatch.setenv("BOSS_EGRESS_PRIVATE_ALLOW", "api.github.com")
    assert _decide("api.github.com")[1] == "host_not_allowed"


def _serve_one(head: bytes) -> bytes:
    a, b = socket.socketpair()
    t = threading.Thread(target=connect.handle, args=(b,), daemon=True)
    t.start()
    a.sendall(head)
    data = b""
    a.settimeout(5)
    while True:
        chunk = a.recv(4096)
        if not chunk:
            break
        data += chunk
    t.join(timeout=5)
    a.close()
    return data


def test_a_refusal_names_the_host_and_the_setting_and_is_logged_with_the_host(monkeypatch, caplog):
    _resolve(monkeypatch, {"artifacts.example.com": ["93.184.216.34"]})
    with caplog.at_level(logging.INFO, logger="boss.egress.connect"):
        answer = _serve_one(b"CONNECT artifacts.example.com:443 HTTP/1.1\r\n\r\n")
    status = answer.split(b"\r\n", 1)[0].decode()
    assert status.startswith("HTTP/1.1 403 Blocked by BOSS network fence: artifacts.example.com")
    assert "BOSS_EGRESS_CONNECT_ALLOW" in status
    assert b"X-Boss-Fence: refused" in answer
    assert any("artifacts.example.com" in r.getMessage() and "host_not_allowed" in r.getMessage()
               for r in caplog.records)


def test_each_reason_says_what_to_change():
    assert "BOSS_EGRESS_PRIVATE_ALLOW" in connect.refusal_reason("nexus.corp.example.com", 8081, "not_a_public_address")
    assert "nexus.corp.example.com:8081" in connect.refusal_reason("nexus.corp.example.com", 8081, "port_not_allowed")
    assert "BOSS_UPSTREAM_PROXY" in connect.refusal_reason("x.example.com", 443, "unresolvable")
    assert "GitHub" in connect.refusal_reason("github.com", 443, "host_not_allowed")
    # A hostile name cannot inject a header line into the reason phrase.
    assert "\r" not in connect.refusal_reason("evil\r\nX-Injected: 1", 443, "host_not_allowed")


class _FakeProxy:
    """An upstream proxy: records the CONNECT it is asked for, then echoes (or refuses)."""

    def __init__(self, answer: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.answer = answer
        self.requests: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        conn, _ = self.sock.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1024)
        self.requests.append(head)
        conn.sendall(self.answer)
        if b" 200 " in self.answer:
            data = conn.recv(1024)
            conn.sendall(b"echo:" + data)
        conn.close()


def _unresolvable(monkeypatch, *names):
    """The company DNS does not resolve outside names; everything else (the proxy) resolves."""
    original = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in names:
            raise socket.gaierror("no such host")
        if host == "proxy.example.com":           # the fake upstream proxy listens on loopback
            host = "127.0.0.1"
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(connect.socket, "getaddrinfo", fake)


def test_a_tunnel_is_chained_through_the_upstream_proxy(monkeypatch):
    proxy = _FakeProxy()
    _unresolvable(monkeypatch, "pkg.example.com")
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "pkg.example.com")
    monkeypatch.setenv("BOSS_UPSTREAM_PROXY", f"http://proxy.example.com:{proxy.port}")
    route, reason = _decide("pkg.example.com")
    assert reason == "" and isinstance(route, connect.Upstream) and route.port == 443
    a, b = socket.socketpair()
    t = threading.Thread(target=connect.handle, args=(b,), daemon=True)
    t.start()
    a.settimeout(5)
    a.sendall(b"CONNECT pkg.example.com:443 HTTP/1.1\r\n\r\n")
    head = b""
    while b"\r\n\r\n" not in head:
        head += a.recv(1024)
    assert head.startswith(b"HTTP/1.1 200")
    a.sendall(b"hello")
    assert a.recv(1024) == b"echo:hello"
    a.close()
    t.join(timeout=5)
    assert proxy.requests[0].startswith(b"CONNECT pkg.example.com:443 HTTP/1.1\r\n")


def test_an_upstream_refusal_is_reported_as_the_proxy_s_not_the_registry_s(monkeypatch):
    proxy = _FakeProxy(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
    _unresolvable(monkeypatch, "pkg.example.com")
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "pkg.example.com")
    monkeypatch.setenv("BOSS_UPSTREAM_PROXY", f"http://proxy.example.com:{proxy.port}")
    answer = _serve_one(b"CONNECT pkg.example.com:443 HTTP/1.1\r\n\r\n")
    status = answer.split(b"\r\n", 1)[0].decode()
    assert status.startswith("HTTP/1.1 502 BOSS egress: your company proxy (BOSS_UPSTREAM_PROXY) "
                             "refused pkg.example.com:443 (HTTP/1.1 407")
    # What to do, for a proxy that wants a login (BOSSAPP-1436).
    assert "local authenticating proxy" in status and "boss upgrade" in status


def test_an_upstream_refusal_of_a_host_says_who_allows_it_and_the_setting(monkeypatch):
    proxy = _FakeProxy(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    _unresolvable(monkeypatch, "pkg.example.com")
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "pkg.example.com")
    monkeypatch.setenv("BOSS_UPSTREAM_PROXY", f"http://proxy.example.com:{proxy.port}")
    status = _serve_one(b"CONNECT pkg.example.com:443 HTTP/1.1\r\n\r\n").split(b"\r\n", 1)[0].decode()
    assert "refused pkg.example.com:443 (HTTP/1.1 403 Forbidden)" in status
    assert "Ask IT to allow it" in status and "BOSS_UPSTREAM_NO_PROXY" in status
    # The engine reads the same words back into the card (network_refusal).
    from boss_engine.services import network_refusal

    got = network_refusal.explain(status)
    assert got and got["kind"] == "company_proxy" and got["host"] == "pkg.example.com"


def test_no_proxy_hosts_are_dialled_directly_and_unresolvable_ones_are_named(monkeypatch):
    _resolve(monkeypatch, {"nexus.corp.example.com": ["10.1.1.1"]})
    monkeypatch.setenv("BOSS_EGRESS_PRIVATE_ALLOW", "nexus.corp.example.com")
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "gone.example.com")
    monkeypatch.setenv("BOSS_UPSTREAM_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("BOSS_UPSTREAM_NO_PROXY", ".corp.example.com")
    route, reason = _decide("nexus.corp.example.com")
    assert reason == "" and not isinstance(route, connect.Upstream)
    monkeypatch.delenv("BOSS_UPSTREAM_PROXY")
    assert _decide("gone.example.com")[1] == "unresolvable"


def test_doctor_explains_each_configured_registry(monkeypatch):
    _resolve(monkeypatch, {"npm.example.com": ["93.184.216.34"], "nexus.corp.example.com": ["10.0.0.3"]})
    lines = connect.explain(["npm.example.com", "nexus.corp.example.com:8081"])
    assert lines[0].startswith("bad ") and "BOSS_EGRESS_CONNECT_ALLOW" in lines[0]
    monkeypatch.setenv("BOSS_NPM_REGISTRY", "https://npm.example.com/")
    monkeypatch.setenv("BOSS_EGRESS_PRIVATE_ALLOW", "nexus.corp.example.com:8081")
    assert [line.split()[0] for line in connect.explain(["npm.example.com", "nexus.corp.example.com:8081"])] == ["ok", "ok"]
