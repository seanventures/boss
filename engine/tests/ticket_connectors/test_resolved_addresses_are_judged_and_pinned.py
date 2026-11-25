"""The ticket connectors' real transport judges what a host RESOLVES to, and connects to exactly
that address (BOSSAPP-1385 review).

A name is only a name: ``jira.example.com`` may resolve to 169.254.169.254, a ``*.ts.net`` name
to a public address, and a second lookup may answer differently from the first (DNS rebinding).
So every resolved address is judged (metadata and link-local never, in any embedded-IPv4
spelling; plain http only to non-public, and ``*.ts.net`` only into the tailnet), and the TCP
connection goes to the address that was judged. No network: resolution and the socket are seams.
"""
from __future__ import annotations

import pytest

from boss_engine.connectors.tickets import http as ticket_http
from boss_engine.connectors.tickets.contract import HostNotAllowed


class _Refused(Exception):
    """Raised by the fake socket: the test only needs to see WHERE the connection went."""


@pytest.fixture
def net(monkeypatch):
    seen = {"connect": []}
    answers: dict[str, list[str]] = {}

    def resolve(host, port):
        seen.setdefault("resolve", []).append(host)
        return answers[host]

    def connect(address, *a, **k):
        seen["connect"].append(address)
        raise _Refused(address)

    monkeypatch.setattr(ticket_http, "RESOLVE", resolve)
    monkeypatch.setattr(ticket_http, "CONNECT", connect)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    return answers, seen


def _send(url):
    return ticket_http._urllib_transport("GET", url, {}, None, 5.0, None)


@pytest.mark.parametrize("url,addresses", [
    ("https://jira.example.com/rest/api/2/myself", ["169.254.169.254"]),     # a name -> metadata
    ("https://jira.example.com/rest/api/2/myself", ["93.184.216.34", "169.254.169.254"]),
    ("https://jira.example.com/rest/api/2/myself", ["fd00:ec2::254"]),
    ("https://jira.example.com/rest/api/2/myself", ["100.100.100.200"]),     # Alibaba metadata
    ("https://jira.example.com/rest/api/2/myself", ["64:ff9b::a9fe:a9fe"]),  # NAT64 of 169.254.169.254
    ("https://jira.example.com/rest/api/2/myself", ["2002:a9fe:a9fe::1"]),   # 6to4 of it
    ("https://jira.example.com/rest/api/2/myself", ["::169.254.169.254"]),   # IPv4-compatible
    ("https://jira.example.com/rest/api/2/myself", ["fe80::1"]),
])
def test_a_name_resolving_to_metadata_or_link_local_is_refused(net, url, addresses):
    answers, seen = net
    answers["jira.example.com"] = addresses
    with pytest.raises(HostNotAllowed):
        _send(url)
    assert seen["connect"] == []          # nothing was ever opened


@pytest.mark.parametrize("address", ["8.8.8.8", "64:ff9b::808:808", "2002:808:808::1",
                                     "::8.8.8.8", "2001:4860::8888"])
def test_a_tailnet_name_resolving_to_a_public_address_is_refused_over_http(net, address):
    answers, seen = net
    answers["jira.tail1234.ts.net"] = [address]
    with pytest.raises(HostNotAllowed):
        _send("http://jira.tail1234.ts.net/rest/api/2/myself")
    assert seen["connect"] == []


@pytest.mark.parametrize("host,address", [("plane-api", "8.8.8.8"), ("jira.corp.local", "1.1.1.1")])
def test_a_non_public_name_over_http_must_resolve_non_public(net, host, address):
    answers, seen = net
    answers[host] = [address]
    with pytest.raises(HostNotAllowed):
        _send(f"http://{host}/rest/api/2/myself")


@pytest.mark.parametrize("url", ["http://instance-data/latest/meta-data/",
                                 "http://instance-data.ec2.internal/latest/meta-data/",
                                 "http://100.100.100.200/latest/meta-data/",
                                 "http://[64:ff9b::a9fe:a9fe]/latest/meta-data/"])
def test_metadata_names_and_spellings_are_refused_by_the_host_check(url):
    with pytest.raises(HostNotAllowed):
        ticket_http.check_host(url, (), (ticket_http._host_of(url),), provider="jira")


def test_the_connection_goes_to_the_address_that_was_judged(net):
    """Pinned: the TCP connection is opened to the checked address, never by name (a second
    lookup could answer differently). Over http the tailnet answer is the one connected to."""
    answers, seen = net
    answers["jira.tail1234.ts.net"] = ["100.101.102.103"]
    with pytest.raises(Exception):
        _send("http://jira.tail1234.ts.net:8080/rest/api/2/myself")
    assert seen["connect"] == [("100.101.102.103", 8080)]
    assert seen["resolve"] == ["jira.tail1234.ts.net"]


def test_https_is_pinned_too_and_keeps_the_name_for_tls(net, monkeypatch):
    answers, seen = net
    answers["jira.example.com"] = ["93.184.216.34"]
    wrapped = {}

    class _Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped["sni"] = server_hostname
            raise _Refused("tls")

    def connect(address, *a, **k):
        seen["connect"].append(address)
        return object()

    monkeypatch.setattr(ticket_http, "CONNECT", connect)
    with pytest.raises(Exception):
        ticket_http._urllib_transport("GET", "https://jira.example.com/rest/api/2/myself", {}, None,
                                      5.0, _Ctx())
    assert seen["connect"] == [("93.184.216.34", 443)] and wrapped["sni"] == "jira.example.com"


def test_a_private_address_behind_an_https_name_is_allowed(net):
    """Self-hosted Jira on internal DNS over https: a private address is fine, metadata is not."""
    answers, seen = net
    answers["jira.corp.example"] = ["10.1.2.3"]
    with pytest.raises(_Refused):
        _send("https://jira.corp.example/rest/api/2/myself")
    assert seen["connect"] == [("10.1.2.3", 443)]
