"""Stream S1: the SSRF-safe outbound client (IDENTITY.md §4.13; A70, A71).

DNS and sockets are replaced with in-test fakes, so nothing leaves the machine; every test asserts
both the refusal AND that no connection was attempted where it must not be.
"""
from __future__ import annotations

import pytest

from boss_engine.config import get_settings
from boss_engine.identity import http
from boss_engine.identity.http import OutboundRefused


class FakeSocket:
    def __init__(self, response: bytes):
        self.sent = b""
        self._chunks = [response[i:i + 65536] for i in range(0, len(response), 65536)]

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _t: float) -> None:
        pass

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        pass


def ok(body: bytes = b'{"ok":true}', status: str = "200 OK", headers: str = "") -> bytes:
    return (f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n{headers}\r\n").encode() + body


@pytest.fixture
def net(monkeypatch):
    """resolver answers + a log of (scheme, host, ip, port) connections and the sockets used."""
    state = {"dns": {}, "resolves": [], "connects": [], "responses": [], "sockets": []}

    def resolve(host, port):
        state["resolves"].append(host)
        answers = state["dns"].get(host)
        if isinstance(answers, list) and answers and isinstance(answers[0], list):
            return answers.pop(0)          # a sequence of answers (rebinding)
        return list(answers or [])

    def open_connection(scheme, host, ip, port, timeout):
        state["connects"].append((scheme, host, ip, port))
        sock = FakeSocket(state["responses"].pop(0) if state["responses"] else ok())
        state["sockets"].append(sock)
        return sock

    monkeypatch.setattr(http, "_resolve", resolve)
    monkeypatch.setattr(http, "_open_connection", open_connection)
    monkeypatch.setattr(get_settings(), "outbound_allow_private", "")
    return state


def test_a_public_https_host_is_fetched_from_its_pinned_address(net):
    net["dns"]["idp.example.com"] = ["93.184.216.34"]
    res = http.fetch("GET", "https://idp.example.com/.well-known/openid-configuration", purpose="test")
    assert res.status == 200 and res.json() == {"ok": True}
    assert net["connects"] == [("https", "idp.example.com", "93.184.216.34", 443)]
    assert b"Host: idp.example.com\r\n" in net["sockets"][0].sent


@pytest.mark.parametrize("url", [
    "https://169.254.169.254/latest/meta-data/",          # A70: cloud metadata
    "http://169.254.169.254/",                            # A70 (and not https)
    "https://127.0.0.1/jwks",                             # A71: loopback literal
    "https://[::1]/jwks",
    "https://10.0.0.5/x", "https://192.168.1.1/x", "https://172.16.0.1/x",   # RFC 1918
    "https://100.64.0.1/x",                               # CGNAT
    "https://[fd00::1]/x",                                # ULA
    "https://[::ffff:127.0.0.1]/x",                       # v4-mapped loopback
])
def test_a70_a71_private_and_metadata_addresses_are_refused_without_connecting(net, url):
    with pytest.raises(OutboundRefused):
        http.fetch("GET", url, purpose="oidc.jwks")
    assert net["connects"] == []


def test_a71_a_name_resolving_to_loopback_is_refused_unless_allow_listed(net, monkeypatch):
    net["dns"]["keycloak.lan"] = ["127.0.0.1"]
    with pytest.raises(OutboundRefused):
        http.fetch("GET", "https://keycloak.lan/certs", purpose="oidc.jwks")
    assert net["connects"] == []
    monkeypatch.setattr(get_settings(), "outbound_allow_private", "keycloak.lan")
    assert http.fetch("GET", "https://keycloak.lan/certs", purpose="oidc.jwks").status == 200
    assert net["connects"] == [("https", "keycloak.lan", "127.0.0.1", 443)]


def test_a71_a_resolver_mixing_public_and_private_answers_is_refused(net):
    net["dns"]["rebind.example"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(OutboundRefused):
        http.fetch("GET", "https://rebind.example/", purpose="test")
    assert net["connects"] == []


def test_a71_dns_is_resolved_once_so_rebinding_between_requests_cannot_switch_the_target(net):
    # first answer public, any later answer private: a same-host redirect reuses the pinned IP
    net["dns"]["flip.example"] = [["93.184.216.34"], ["127.0.0.1"]]
    net["responses"] = [ok(b"", "302 Found", "Location: /next\r\n"), ok()]
    assert http.fetch("GET", "https://flip.example/start", purpose="test").status == 200
    assert net["resolves"] == ["flip.example"]
    assert [c[2] for c in net["connects"]] == ["93.184.216.34", "93.184.216.34"]


def test_https_is_required(net):
    net["dns"]["idp.example.com"] = ["93.184.216.34"]
    with pytest.raises(OutboundRefused, match="https"):
        http.fetch("GET", "http://idp.example.com/", purpose="test")
    assert net["connects"] == []


def test_a_redirect_to_another_host_is_refused(net):
    net["dns"]["idp.example.com"] = ["93.184.216.34"]
    net["responses"] = [ok(b"", "302 Found", "Location: https://evil.example/steal\r\n")]
    with pytest.raises(OutboundRefused, match="another host"):
        http.fetch("GET", "https://idp.example.com/", purpose="test")
    assert len(net["connects"]) == 1


def test_a_body_over_one_mebibyte_is_refused(net):
    net["dns"]["big.example"] = ["93.184.216.34"]
    net["responses"] = [ok(b"x" * (http.MAX_BODY_BYTES + 10))]
    with pytest.raises(OutboundRefused, match="1 MiB"):
        http.fetch("GET", "https://big.example/", purpose="test")


def test_header_injection_and_credentials_in_urls_are_refused(net):
    net["dns"]["idp.example.com"] = ["93.184.216.34"]
    with pytest.raises(OutboundRefused):
        http.fetch("GET", "https://idp.example.com/", headers={"X-A": "1\r\nHost: evil"}, purpose="test")
    with pytest.raises(OutboundRefused):
        http.fetch("GET", "https://user:pw@idp.example.com/", purpose="test")


def test_purpose_is_required():
    with pytest.raises(ValueError):
        http.fetch("GET", "https://idp.example.com/", purpose="")


def test_an_unroutable_ipv6_answer_falls_back_to_the_ipv4_one(net, monkeypatch):
    # boss-1, 2026-10-08: Google's token host answers v6 + v4; the container has no v6 route. The
    # v6 address sorted first and was the only one tried, so every Google sign-in failed.
    net["dns"]["oauth2.example.com"] = ["2607:f8b0:4004:c29::5f", "64.233.180.95"]
    tried = []

    def open_connection(scheme, host, ip, port, timeout):
        tried.append(ip)
        if ":" in ip:
            raise OSError("Network is unreachable")
        return FakeSocket(ok())

    monkeypatch.setattr(http, "_open_connection", open_connection)
    assert http.fetch("POST", "https://oauth2.example.com/token", data=b"x", purpose="oidc.token").status == 200
    assert tried == ["64.233.180.95"]                      # IPv4 first: no wasted v6 attempt


def test_every_checked_address_is_tried_before_giving_up(net, monkeypatch):
    net["dns"]["flaky.example"] = ["93.184.216.34", "93.184.216.35"]
    tried = []

    def open_connection(scheme, host, ip, port, timeout):
        tried.append(ip)
        if ip.endswith(".34"):
            raise OSError("refused")
        return FakeSocket(ok())

    monkeypatch.setattr(http, "_open_connection", open_connection)
    assert http.fetch("GET", "https://flaky.example/", purpose="test").status == 200
    assert tried == ["93.184.216.34", "93.184.216.35"]
    net["dns"]["dead.example"] = ["93.184.216.36"]
    monkeypatch.setattr(http, "_open_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(OutboundRefused, match="connection failed"):
        http.fetch("GET", "https://dead.example/", purpose="test")
