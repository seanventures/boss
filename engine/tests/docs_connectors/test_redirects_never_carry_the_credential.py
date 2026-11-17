"""A redirect never carries the credential to another host, and a credential never goes over plain
http to a non-loopback host (the shared ticket-connector HTTP path: Jira and Confluence)."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from boss_engine.connectors.tickets import http as th
from boss_engine.connectors.tickets.contract import HostNotAllowed, TicketConnection, TicketSystemError


class _Server:
    def __init__(self, redirect_to: str = ""):
        seen: list[dict] = []
        target = redirect_to
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                self.end_headers()

            def log_message(self, *a):  # noqa: D401
                pass

        self.redirect_to, self.seen = target, seen
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def servers():
    other, first = _Server(), None
    first = _Server(redirect_to=f"http://localhost:{other.port}/steal")
    yield first, other
    first.close(); other.close()


def test_a_cross_host_redirect_is_not_followed_and_carries_no_credential(servers):
    first, other = servers
    got = th._urllib_transport("GET", f"http://127.0.0.1:{first.port}/a",
                               {"Authorization": "Basic c2VjcmV0"}, None, 5, None)
    assert got.status == 302
    assert first.seen[0]["auth"] == "Basic c2VjcmV0" and other.seen == []


def test_the_shared_helper_for_jira_never_follows_it_either(servers):
    first, other = servers
    conn = TicketConnection("jira", {"base_url": f"http://127.0.0.1:{first.port}"},
                            credential_resolver=lambda: "tok")
    http = th.TicketHttp(conn, base_url=f"http://127.0.0.1:{first.port}",
                         auth=lambda c: ({"Authorization": "Bearer tok"}, None),
                         base_url_fields=("base_url",), guard_provider="jira")
    with pytest.raises(TicketSystemError):
        http.request("GET", "/rest/api/3/myself")
    assert other.seen == []


def test_same_host_redirects_still_work():
    target = _Server()
    first = _Server(redirect_to=f"http://127.0.0.1:{target.port}/b")
    try:
        got = th._urllib_transport("GET", f"http://127.0.0.1:{first.port}/a", {}, None, 5, None)
        assert got.status == 302          # a different PORT is a different origin: not followed
        assert target.seen == []
    finally:
        first.close(); target.close()


def test_a_credentialed_call_over_plain_http_is_refused_off_loopback():
    with pytest.raises(HostNotAllowed, match="plain-http"):
        th.check_host("http://wiki.corp.example/api", (), ("wiki.corp.example",), "confluence")
    th.check_host("https://wiki.corp.example/api", (), ("wiki.corp.example",), "confluence")
    th.check_host("http://127.0.0.1:9/api", (), ("127.0.0.1",), "jira")
