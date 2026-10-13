"""A LOCAL GitHub for the plumbing gate: git smart-HTTP and the REST API, on one loopback port.

Both are the e2e harness's own fakes, backed by real bare repositories (tests/e2e/stubs:
egress.GitRemotes serves `git http-backend`, github_fake.GitHubFake answers the Git Data API and
pulls from the same repositories), so a clone sees what a pull request wrote.

`blips` makes GitHub have a bad moment on purpose: the next N requests of a kind ("git" or "api")
are answered 502 Bad Gateway, exactly what crun_2f76585e met on its clone ("RPC failed; HTTP 502").
Every request is counted, so a check can say how many tries BOSS made.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_STUBS = Path(__file__).resolve().parents[1] / "stubs"
if str(_STUBS) not in sys.path:
    sys.path.insert(0, str(_STUBS))

from egress import GitRemotes  # noqa: E402  (tests/e2e/stubs, not a package)
from fakes import World  # noqa: E402
from github_fake import GitHubFake  # noqa: E402


class LocalGitHub:
    def __init__(self, root: Path):
        self.remotes = GitRemotes(root)
        self.world = World()
        self.api = GitHubFake(self.remotes, self.world)
        self.blips = {"git": 0, "api": 0}
        self.blip_match = {"git": "", "api": ""}
        self.requests = {"git": 0, "api": 0}
        self.blipped = {"git": 0, "api": 0}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # -- control ---------------------------------------------------------------------------
    def blip(self, kind: str, times: int = 1, match: str = "") -> None:
        """The next `times` requests of `kind` whose "METHOD /path" contains `match` get a 502."""
        with self._lock:
            self.blips[kind] = times
            self.blip_match[kind] = match

    def seed(self, owner: str, repo: str, files: dict[str, str]) -> Path:
        return self.remotes.ensure(owner, repo, seed=files)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def git_url(self, owner: str, repo: str) -> str:
        return f"http://127.0.0.1:{self.port}/git/{owner}/{repo}.git"

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api"

    def _take_blip(self, kind: str, what: str) -> bool:
        with self._lock:
            self.requests[kind] += 1
            if self.blips[kind] > 0 and self.blip_match[kind] in what:
                self.blips[kind] -= 1
                self.blipped[kind] += 1
                return True
            return False

    # -- serving ---------------------------------------------------------------------------
    def start(self) -> "LocalGitHub":
        hub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                self.send_response(status)
                for k, v in headers.items():
                    if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path, _, query = self.path.partition("?")
                kind = "git" if path.startswith("/git/") else "api" if path.startswith("/api") else ""
                if not kind:
                    self._send(404, {}, b"not found")
                    return
                if hub._take_blip(kind, f"{method} {path}"):
                    self._send(502, {"Content-Type": "text/html"},
                               b"<html><body><h1>502 Bad Gateway</h1></body></html>")
                    return
                if kind == "git":
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, out_headers, payload = hub.remotes.serve(
                        method, path[len("/git"):], query, headers, body)
                    self._send(status, out_headers, payload)
                    return
                tarball = re.match(r"^/api/repos/([^/]+)/([^/]+)/tarball/([0-9a-f]{40})$", path)
                if tarball and method == "GET":
                    # GitHub's tarball of a commit (what a stack deploy downloads): one top-level
                    # directory, as GitHub names it.
                    owner, repo, sha = tarball.groups()
                    bare = hub.remotes.ensure(owner, repo)
                    done = subprocess.run(["git", "--git-dir", str(bare), "archive", "--format=tar.gz",
                                           f"--prefix={owner}-{repo}-{sha[:7]}/", sha],
                                          capture_output=True, timeout=120)
                    if done.returncode != 0:
                        self._send(404, {"Content-Type": "application/json"}, b'{"message": "Not Found"}')
                    else:
                        self._send(200, {"Content-Type": "application/x-gzip"}, done.stdout)
                    return
                try:
                    data = json.loads(body) if body else None
                except ValueError:
                    data = None
                status, answer = hub.api.handle(method, path[len("/api"):] or "/", query, data)
                raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                self._send(status, {"Content-Type": "application/json"}, raw)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
