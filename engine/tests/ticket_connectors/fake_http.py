"""A small recorded fake HTTP server for ticket-connector tests: real sockets on 127.0.0.1, regex
routes, a request log, and injected faults (429 with Retry-After, 5xx, ...)."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit


class Request:
    def __init__(self, method: str, path: str, query: dict[str, list[str]],
                 headers: dict[str, str], body: bytes):
        self.method, self.path, self.query, self.headers, self.body = (method, path, query,
                                                                       headers, body)

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    def q(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return (self.query.get(name) or [default])[0]


Reply = tuple[int, Any, dict[str, str]]


class FakeServer:
    """Subclasses add routes with :meth:`route`. A handler returns (status, json_body, headers)."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Reply]]] = []
        self.log: list[Request] = []
        self.faults: list[tuple[int, dict[str, str], Any]] = []
        self.lock = threading.RLock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def route(self, method: str, pattern: str, handler: Callable[..., Reply]) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    def fail_next(self, status: int, times: int = 1, *, headers: Optional[dict[str, str]] = None,
                  body: Any = None) -> None:
        with self.lock:
            for _ in range(times):
                self.faults.append((status, dict(headers or {}), body))

    def authorized(self, request: Request) -> bool:  # overridden
        return True

    def requests(self, method: str = "", contains: str = "") -> list[Request]:
        return [r for r in self.log if (not method or r.method == method) and contains in r.path]

    def dispatch(self, request: Request) -> Reply:
        with self.lock:
            self.log.append(request)
            if self.faults:
                status, headers, body = self.faults.pop(0)
                return status, body if body is not None else {"error": f"injected {status}"}, \
                    headers
        for method, pattern, handler in self.routes:
            if method != request.method:
                continue
            match = pattern.match(request.path)
            if match:
                if not self.authorized(request):
                    return 401, {"errorMessages": ["Client must be authenticated"]}, {}
                with self.lock:
                    return handler(request, *match.groups())
        return 404, {"errorMessages": [f"no route {request.method} {request.path}"]}, {}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = Request(self.command, parts.path, parse_qs(parts.query),
                                  {k.lower(): v for k, v in self.headers.items()}, body)
                status, payload, headers = fake.dispatch(request)
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        return Handler
