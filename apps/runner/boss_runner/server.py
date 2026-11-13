"""The runner's HTTP surface (stdlib only), called by the API (services/runner_dispatch.py).

    GET  /healthz                              liveness; says nothing about any run
    GET  /v1/runner/identity                   runner_id, boot_id, and the PUBLIC DPoP key (jwk, jkt)
                                               the API binds this runner's run tokens to
    POST /v1/runner/commands                   start | steer | cancel | close (daemon.Runner.command)
    GET  /v1/runner/sessions/{id}              state, exit code, seqs, whether the process is alive
    GET  /v1/runner/sessions/{id}/result       the finished turn: exit code, stdout, stderr, files

Every /v1/runner route but identity needs `Authorization: DPoP <run token bound to this runner's
key, for the run concerned>` (auth.TokenVerifier).
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .auth import RunnerAuthError
from .daemon import CommandError, Runner

_log = logging.getLogger("boss.runner.http")

MAX_BODY_BYTES = 8 * 1024 * 1024


def make_handler(runner: Runner) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "boss-runner"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 — quiet by default
            _log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self, fn) -> None:
            try:
                status, body = fn()
            except RunnerAuthError as exc:
                status, body = exc.status, {"error": exc.code}
            except CommandError as exc:
                status, body = exc.status, {"error": exc.code}
            except Exception:  # noqa: BLE001 — never a stack trace on the wire
                _log.exception("runner request failed")
                status, body = 500, {"error": "internal"}
            self._send(status, body)

        def _path(self) -> list[str]:
            return [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]

        def do_GET(self) -> None:  # noqa: N802
            parts = self._path()
            auth = self.headers.get("Authorization")
            if parts == ["healthz"]:
                self._send(200, {"ok": True, "runner_id": runner.runner_id})
            elif parts == ["v1", "runner", "identity"]:
                self._send(200, runner.identity())
            elif len(parts) == 4 and parts[:3] == ["v1", "runner", "sessions"]:
                self._guard(lambda: (200, runner.status(parts[3], auth)))
            elif len(parts) == 5 and parts[:3] == ["v1", "runner", "sessions"] and parts[4] == "result":
                self._guard(lambda: (200, runner.result(parts[3], auth)))
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parts = self._path()
            if parts != ["v1", "runner", "commands"]:
                self._send(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, {"error": "body_too_large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"error": "bad_json"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "bad_json"})
                return
            self._guard(lambda: (200, runner.command(body, self.headers.get("Authorization"))))

    return Handler


class RunnerServer:
    """The HTTP server around a Runner. Port 0 picks a free port (tests)."""

    def __init__(self, runner: Runner, *, host: str = "0.0.0.0", port: int = 8095):
        self.runner = runner
        self.httpd = ThreadingHTTPServer((host, port), make_handler(runner))
        self.httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"

    def start(self) -> "RunnerServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="boss-runner-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
