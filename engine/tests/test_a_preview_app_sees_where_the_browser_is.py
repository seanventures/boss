"""A previewed app sees where the browser is, without breaking dev servers that allowlist Host
(contributed from an enterprise install, 2026-11-20).

On the preview origin the browser sends `Origin: http://prev-abc123.localhost:3001`, and BOSS
reached the app with `Host: 127.0.0.1:<port>`. Apps that check a write as same-origin by
comparing Origin with Host refused every POST with 403: the contributor's app could not save.

Forwarding the public Host everywhere fixes those and breaks another, common class: dev servers
that allowlist Host (Vite `allowedHosts`, webpack-dev-server, Angular, Django, Rails) accept
localhost, `*.localhost` and IPs only, and would 403 a preview origin on a hostname. So the
proxy (preview_service.preview_public_host_headers):

  - always sends X-Forwarded-Host and X-Forwarded-Proto;
  - sends the public Host only for `<label>.localhost`, or when BOSS_PREVIEW_FORWARD_HOST opts in;
  - sends none of it for a host whose first label is not this preview's, or that is not a plain
    hostname[:port].

Real HTTP upstreams on loopback; no database.
"""
from __future__ import annotations

import contextlib
import http.server
import threading

import pytest

from boss_engine.services import preview_service

PREVIEW = "prev_abc123"


class _Upstream(http.server.BaseHTTPRequestHandler):
    """An app that does BOTH checks real apps do: a Vite-style Host allowlist (localhost,
    *.localhost, IPs), and a same-origin check on writes (Origin against the host it sees,
    X-Forwarded-Host first, as frameworks that honour it do)."""
    seen: list[dict[str, str]] = []

    def _allowed_host(self) -> bool:
        name = self.headers.get("Host", "").rsplit(":", 1)[0].lower()
        return name in ("localhost", "127.0.0.1") or name.endswith(".localhost")

    def _answer(self, write: bool) -> None:
        _Upstream.seen.append({k.lower(): v for k, v in self.headers.items()})
        if not self._allowed_host():
            self.send_response(403)
        elif write:
            public = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            self.send_response(200 if self.headers.get("Origin") == f"{proto}://{public}" else 403)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):  # noqa: N802
        self._answer(write=False)

    def do_POST(self):  # noqa: N802
        self._answer(write=True)

    def log_message(self, *_a):
        pass


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, *_a, **_k):
        return _Rows(self.row)


@pytest.fixture()
def upstream(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    row = {"url": f"http://127.0.0.1:{server.server_address[1]}", "status": "running",
           "mode": "process"}

    @contextlib.contextmanager
    def begin(_engine):
        yield _Conn(row)

    monkeypatch.setattr(preview_service, "begin", begin)
    monkeypatch.delenv(preview_service.PREVIEW_FORWARD_HOST_ENV, raising=False)
    _Upstream.seen.clear()
    yield server
    server.shutdown()


def _post(host: str, origin: str, scheme: str = "http") -> int:
    status, *_ = preview_service.proxy_preview(
        None, PREVIEW, "save", "POST", body=b"x=1",
        content_type="application/x-www-form-urlencoded",
        incoming_headers={"Host": host, "Origin": origin}, serve_at_root=True,
        public_scheme=scheme)
    return status


def test_a_localhost_preview_saves_same_origin_writes(upstream):
    host = "prev-abc123.localhost:3001"
    assert _post(host, f"http://{host}") == 200
    assert _Upstream.seen[-1]["host"] == host
    assert _post(host, "http://untrusted.example") == 403


def test_a_hostname_preview_keeps_a_host_the_dev_server_accepts(upstream):
    """No 403 regression for Vite-style allowlists: the app still sees a loopback Host, and is
    told the public one in X-Forwarded-Host, so its same-origin check still passes."""
    host = "prev-abc123.boss.example.com:3001"
    assert _post(host, f"https://{host}", scheme="https") == 200
    seen = _Upstream.seen[-1]
    assert seen["host"].startswith("127.0.0.1:")
    assert seen["x-forwarded-host"] == host
    assert seen["x-forwarded-proto"] == "https"


def test_the_opt_in_sends_the_public_host_for_a_custom_preview_origin(upstream, monkeypatch):
    monkeypatch.setenv(preview_service.PREVIEW_FORWARD_HOST_ENV, "1")
    host = "prev-abc123.boss.example.com:3001"
    _post(host, f"http://{host}")
    assert _Upstream.seen[-1]["host"] == host


@pytest.mark.parametrize("host", [
    "prev-other1.localhost:3001",          # another preview's label
    "evil.prev-abc123.localhost:3001",     # this label, not first
    "prev-abc123",                         # no parent
    "prev-abc123.localhost:3001\r\nX-Injected: 1",
    "prev-abc123.local host",
    "user@prev-abc123.localhost",
])
def test_a_host_that_is_not_this_previews_tells_the_app_nothing(upstream, host):
    assert preview_service.preview_public_host_headers({"host": host}, PREVIEW) == {}


def test_an_attacker_parent_is_never_sent_as_host(upstream):
    """`prev-abc123.attacker.example` passes the origin's first-label check; the app must not be
    handed it as Host (host-header poisoning of links it builds)."""
    _post("prev-abc123.attacker.example", "http://prev-abc123.attacker.example")
    assert _Upstream.seen[-1]["host"].startswith("127.0.0.1:")


def test_through_the_console_path_nothing_is_added(upstream):
    """Not on the preview origin (serve_at_root False): the app is reached as before."""
    preview_service.proxy_preview(None, PREVIEW, "", "GET",
                                  incoming_headers={"Host": "prev-abc123.localhost:3001"})
    seen = _Upstream.seen[-1]
    assert seen["host"].startswith("127.0.0.1:")
    assert "x-forwarded-host" not in seen


def test_the_scheme_is_only_http_or_https():
    got = preview_service.preview_public_host_headers(
        {"Host": "prev-abc123.localhost:3001"}, PREVIEW, scheme="javascript")
    assert got["X-Forwarded-Proto"] == "http"
