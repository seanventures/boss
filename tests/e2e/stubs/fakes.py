"""The outside world for the end-to-end harness: GitHub, a work tracker, YouTube, a deploy target
and a preview host over HTTP, and an SMTP sink — one process, every request logged.

WHY ONE PROCESS. Each example and template reaches a different external service at its final
step — weekly-newsletter mails, meeting-to-tickets files tickets, video-workflow uploads — and
on boss-1 every one of them would have failed only after a human approved it, because none was
configured. The harness has to prove the step DOES the thing, so each fake keeps what it was
sent and serves it back at /_log, /_mail, /github/_prs, /plane/_issues and /youtube/_videos for
the driver to assert on. Nothing here talks to the internet.

Stdlib only.
"""
from __future__ import annotations

import argparse
import email
import json
import re
import socketserver
import ssl
import sys
import threading
import urllib.error
import urllib.request
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from egress import CertificateAuthority, GitRemotes, host_route  # noqa: E402
from github_fake import GitHubFake  # noqa: E402
from github_device_fake import DeviceFlowFake  # noqa: E402
import github_probes  # noqa: E402


class World:
    """Everything the fakes were told. Thread-safe."""

    def __init__(self, state_dir: Optional[Path] = None):
        self.lock = threading.Lock()
        self.state_dir = state_dir
        self.log: list[dict[str, Any]] = []
        self.mail: list[dict[str, Any]] = []
        self.prs: list[dict[str, Any]] = []
        self.created_repos: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.videos: list[dict[str, Any]] = []
        self.deploys: list[dict[str, Any]] = []
        self.mcp_calls: list[dict[str, Any]] = []
        self.refs: dict[str, str] = {}
        # What the PRs were told, and the CI the test wants GitHub to report: check runs keyed by
        # commit sha or branch name ("*" for any commit), set through POST /github/_checks.
        self.pr_comments: list[dict[str, Any]] = []
        self.pr_labels: list[dict[str, Any]] = []
        self.checks: dict[str, list[dict[str, Any]]] = {}
        # A real app process standing in for what a deploy host runs (POST /_app_backend): the
        # app's *.e2e.test address, and curl on that host over the ssh stub, reach it.
        self.app_backends: dict[str, str] = {}
        if state_dir:
            state_dir.mkdir(parents=True, exist_ok=True)

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.log.append({"at": time.time(), **entry})

    def add(self, bucket: str, item: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            items = getattr(self, bucket)
            item = {"number": len(items) + 1, **item}
            items.append(item)
        if self.state_dir:
            (self.state_dir / f"{bucket}.json").write_text(json.dumps(getattr(self, bucket),
                                                                      indent=2, default=str))
        return item


def _forward(base: str, method: str, path: str, query: str, raw: bytes) -> tuple[int, Any, str]:
    """Pass one request to a registered app backend, as the deploy host's port would."""
    url = base + path + (f"?{query}" if query else "")
    request = urllib.request.Request(url, data=raw or None, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")
    except Exception as exc:  # noqa: BLE001 — the app being down is what a probe should see
        return 502, {"message": f"boss-e2e: the app backend did not answer: {exc}"}, "application/json"


# --------------------------------------------------------------------------- SMTP sink
class SmtpHandler(socketserver.StreamRequestHandler):
    """Enough SMTP for smtplib: EHLO/HELO, MAIL, RCPT, DATA, RSET, NOOP, QUIT. No auth, no TLS
    (the harness sets BOSS_EMAIL_SMTP_USE_TLS=false); AUTH is accepted so a configured username
    does not fail the send."""

    world: World

    def _say(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self) -> None:
        self._say("220 boss-e2e smtp sink")
        sender, rcpts = "", []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb == "EHLO":
                self.wfile.write(b"250-boss-e2e\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif verb == "HELO":
                self._say("250 boss-e2e")
            elif verb == "AUTH":
                self._say("235 ok")
            elif verb == "MAIL":
                sender, rcpts = line.split(":", 1)[1].strip().strip("<>").split(">")[0], []
                self._say("250 ok")
            elif verb == "RCPT":
                rcpts.append(line.split(":", 1)[1].strip().strip("<>").split(">")[0])
                self._say("250 ok")
            elif verb == "DATA":
                self._say("354 end with .")
                chunks: list[bytes] = []
                while True:
                    part = self.rfile.readline()
                    if not part or part in (b".\r\n", b".\n"):
                        break
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                body = b"".join(chunks)
                message = email.message_from_bytes(body)
                self.world.add("mail", {
                    "from": sender, "to": rcpts, "subject": str(message.get("Subject") or ""),
                    "body": _text_of(message)[:20000],
                })
                self._say("250 queued")
            elif verb in ("RSET", "NOOP"):
                self._say("250 ok")
            elif verb == "QUIT":
                self._say("221 bye")
                return
            else:
                self._say("502 not implemented")


def _text_of(message: email.message.Message) -> str:
    if message.is_multipart():
        return "\n".join(_text_of(part) for part in message.get_payload())  # type: ignore[arg-type]
    payload = message.get_payload(decode=True)
    return payload.decode(errors="replace") if isinstance(payload, bytes) else str(payload or "")


# --------------------------------------------------------------------------- HTTP fakes
class Handler(BaseHTTPRequestHandler):
    world: World
    ca: Optional[CertificateAuthority] = None
    git: Optional[GitRemotes] = None
    github: Optional[GitHubFake] = None
    #: "Sign in with GitHub" (device flow) and a user token's reach (github_device_fake.py).
    device: Optional[DeviceFlowFake] = None
    server_version = "boss-e2e-fakes/1"
    protocol_version = "HTTP/1.1"
    _tunnel_host: Optional[str] = None
    _extra_headers: dict[str, str]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _raw_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            while True:
                size = int((self.rfile.readline().strip().split(b";")[0] or b"0"), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    @staticmethod
    def _json(raw: bytes) -> Any:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"_raw_bytes": len(raw)}

    def _send(self, status: int, payload: Any, content_type: str = "application/json",
              headers: Optional[dict[str, str]] = None) -> None:
        data = (payload if isinstance(payload, (bytes, bytearray))
                else (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()))
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            if name.lower() not in ("content-length", "content-type"):
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _route(self, method: str) -> None:
        host = self._tunnel_host
        target = self.path
        if host is None and target.startswith(("http://", "https://")):
            # A plain-HTTP proxy request carries the absolute URL.
            rest = target.split("://", 1)[1]
            host, _, tail = rest.partition("/")
            target = "/" + tail
        path, _, query = target.partition("?")
        raw = self._raw_body() if method in ("POST", "PUT", "PATCH") else b""
        if host is not None:
            self._egress(method, host, path, query, raw)
            return
        if path.startswith(("/gh-web/", "/gh-api/")):
            # GitHub reached by BASE URL instead of the proxy (BOSS_GITHUB_WEB_URL /
            # BOSS_GITHUB_API_URL pointed here, as identity's SSRF-safe client needs): the same
            # answers as github.com and api.github.com through the proxy.
            gh_host = "github.com" if path.startswith("/gh-web/") else "api.github.com"
            self._egress(method, gh_host, path[len("/gh-web"):], query, raw)
            return
        body = self._json(raw)
        self._extra_headers = {}
        status, payload, ctype = self._dispatch(method, path, body)
        self.world.record({"method": method, "path": path, "query": query, "status": status,
                           "body_keys": sorted(body) if isinstance(body, dict) else []})
        self._send(status, payload, ctype, self._extra_headers)

    # ------------------------------------------------------------------ egress (proxy)
    def do_CONNECT(self) -> None:  # noqa: N802
        host = self.path.split(":", 1)[0]
        if self.ca is None:
            self.send_error(502, "no CA: the fakes were started without a state dir")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            tls = self.ca.context_for(host).wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            self.world.record({"egress": host, "method": "CONNECT", "status": 0,
                               "error": f"TLS handshake failed: {exc}"})
            self.close_connection = True
            return
        self.connection = tls
        self.rfile = tls.makefile("rb", buffering=0)
        self.wfile = tls.makefile("wb", buffering=0)
        self._tunnel_host = host
        self.close_connection = False
        while not self.close_connection:
            try:
                self.handle_one_request()
            except (ssl.SSLError, OSError, ValueError):
                break
        self.close_connection = True

    def _egress(self, method: str, host: str, path: str, query: str, raw: bytes) -> None:
        route = host_route(host)
        entry = {"egress": host, "method": method, "path": path, "query": query[:200],
                 "route": route}
        if route is None:
            entry["status"] = 502
            self.world.record({**entry, "blocked": True})
            self._send(502, {"message": f"boss-e2e: egress to {host} is blocked — no fake "
                                        f"answers this host, and a scripted run must not reach "
                                        f"the real one"})
            return
        if route in ("git", "github-api") and self.device is not None:
            answered = self.device.handle(method, path, dict(self.headers.items()), raw)
            if answered is not None:
                entry["status"] = answered[0]
                self.world.record(entry)
                self._send(answered[0], answered[1])
                return
        if route == "git" and self.git is not None:
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, out_headers, payload = self.git.serve(method, path, query, headers, raw)
            entry["status"] = status
            self.world.record(entry)
            self._send(status, payload, out_headers.pop("Content-Type", ""), out_headers)
            return
        self._extra_headers = {}
        body = self._json(raw)
        if route == "github-api":
            # REPO AND MODELS' PROBES, per token profile (github_probes.py): a read-only token,
            # one with no pull-request permission, an expired or org-pending one. Any other
            # token, and every other path, is the git-backed fake as before.
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            probed = github_probes.answer(token, method, path, body=body)
            if probed is not None:
                status, probe_headers, payload = probed
                self._extra_headers = dict(probe_headers)
                ctype = "application/json"
            else:
                status, payload, ctype = self._github(method, path, body, query)
        elif route == "app" and host.split(":")[0].lower() in self.world.app_backends:
            status, payload, ctype = _forward(self.world.app_backends[host.split(":")[0].lower()],
                                              method, path, query, raw)
        elif route == "app":
            status, payload, ctype = (self._media(path) if path.startswith("/media/")
                                      else self._dispatch(method, "/app" + path, body))
        else:
            status, payload, ctype = self._google(method, host, path, query, body)
        entry["status"] = status
        self.world.record(entry)
        self._send(status, payload, ctype, self._extra_headers)

    def _google(self, method: str, host: str, path: str, query: str,
                body: Any) -> tuple[int, Any, str]:
        json_t = "application/json"
        if host.startswith("oauth2.") or path.endswith("/token"):
            return 200, {"access_token": "e2e-google-access-token", "expires_in": 3600,
                         "refresh_token": "e2e-google-refresh-token",
                         "token_type": "Bearer", "scope": "youtube.upload drive.readonly"}, json_t
        if "/upload/youtube" in path:
            if "uploadType=resumable" in query and "upload_id" not in query:
                self._extra_headers = {
                    "Location": f"https://{host}{path}?uploadType=resumable&upload_id=e2e-up"}
                return 200, {}, json_t
            video = self.world.add("videos", {"host": host, "path": path,
                                              "snippet": (body or {}).get("snippet")
                                              if isinstance(body, dict) else None})
            return 200, {"id": f"e2e-video-{video['number']}", "kind": "youtube#video",
                         "status": {"uploadStatus": "uploaded", "privacyStatus": "private"}}, json_t
        if "/youtube/v3/channels" in path:
            return 200, {"items": [{"id": "UC_e2e", "snippet": {"title": "e2e channel"}}]}, json_t
        if "/youtube/v3/videos" in path and method == "GET":
            return 200, {"items": [{"id": v.get("number"), "status": {"uploadStatus": "processed"}}
                                   for v in self.world.videos]}, json_t
        if "/drive/v3/files" in path:
            if "alt=media" in query:
                return 200, "An e2e brief: cut a 5-second clip with a title card.\n", "text/plain"
            return 200, {"id": path.rsplit("/", 1)[-1], "name": "brief.txt",
                         "mimeType": "text/plain", "size": "52"}, json_t
        return 404, {"error": {"message": f"boss-e2e: no Google fake for {method} {host}{path}"}}, json_t

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._route("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._route("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    # ------------------------------------------------------------------ dispatch
    def _dispatch(self, method: str, path: str, body: Any) -> tuple[int, Any, str]:
        w = self.world
        json_t = "application/json"
        if path in ("/health", "/healthz"):
            return 200, {"ok": True}, json_t
        if path == "/_log":
            return 200, {"entries": list(w.log)}, json_t
        if path == "/_mail":
            return 200, {"messages": list(w.mail)}, json_t
        if path == "/_world":
            return 200, {"prs": w.prs, "issues": w.issues, "videos": w.videos,
                         "deploys": w.deploys, "mail": w.mail, "pr_comments": w.pr_comments,
                         "pr_labels": w.pr_labels, "checks": w.checks}, json_t
        if path == "/_app_backend" and method == "POST" and isinstance(body, dict):
            host = str(body.get("host") or "").split("@")[-1].split(":")[0].lower()
            with w.lock:
                if body.get("url"):
                    w.app_backends[host] = str(body["url"]).rstrip("/")
                else:
                    w.app_backends.pop(host, None)
            return 200, {"ok": True, "host": host}, json_t
        if path.startswith("/_app_backend/"):
            host = path[len("/_app_backend/"):].split("@")[-1].split(":")[0].lower()
            url = w.app_backends.get(host)
            return (200, {"url": url}, json_t) if url else (404, {"url": None}, json_t)
        if path == "/github/_checks" and method == "POST" and isinstance(body, dict):
            # The test plays GitHub Actions: the check runs a commit (or branch) reports.
            key = str(body.get("sha") or body.get("branch") or "*")
            with w.lock:
                w.checks[key] = [dict(run) for run in body.get("check_runs") or []]
            return 200, {"ok": True, "key": key}, json_t
        if path == "/github/_owners" and method == "POST" and isinstance(body, dict) and self.git is not None:
            # LOCAL-FIRST: owners whose repositories exist only once made, and each one's type
            # ("Organization" | "User"): {"owners": [{"login": "acme-new", "type": "Organization"}]}.
            for item in body.get("owners") or []:
                login = str(item.get("login") or "").lower()
                if login:
                    self.git.fresh_owners.add(login)
                    self.git.owner_types[login] = str(item.get("type") or "Organization")
            return 200, {"fresh": sorted(self.git.fresh_owners)}, json_t
        if path == "/github/_outage" and method == "POST" and isinstance(body, dict) and self.git is not None:
            # LOCAL-FIRST (BOSSAPP-922): GitHub down (every repository call and git answers 502).
            self.git.outage = bool(body.get("on"))
            return 200, {"outage": self.git.outage}, json_t
        if path == "/github/_seed" and method == "POST" and isinstance(body, dict) and self.git is not None:
            # A repository that already holds other history (Publish refuses it in words).
            self.git.ensure(str(body.get("owner") or ""), str(body.get("repo") or ""))
            return 200, {"ok": True}, json_t
        if path == "/github/_prs":
            return 200, {"prs": list(w.prs)}, json_t
        if path == "/github/_created" and self.git is not None:
            return 200, {"repos": list(getattr(self.world, "created_repos", []))}, json_t
        if path == "/github/_device" and method == "POST" and isinstance(body, dict) and self.device:
            # The test plays the person at github.com/login/device: approve, deny, expire, slow_down.
            return 200, self.device.control(body), json_t
        if path.startswith("/github"):
            return self._github(method, path[len("/github"):] or "/", body)
        if path.startswith("/plane"):
            return self._plane(method, path[len("/plane"):] or "/", body)
        if path.startswith("/youtube") or path.startswith("/upload/youtube"):
            return self._youtube(method, path, body)
        if path.startswith("/app"):
            # A preview or a deployed app: answers health and serves a page, so a monitor or
            # a preview probe sees a live service.
            if path.endswith("/health") or path.endswith("/healthz"):
                return 200, {"status": "ok"}, json_t
            return 200, ("<!doctype html><html><head><title>e2e app</title></head>"
                         "<body><h1>e2e app is serving</h1></body></html>"), "text/html"
        if path.startswith("/deploy"):
            item = w.add("deploys", {"path": path, "body": body})
            return 200, {"ok": True, "deploy": item["number"]}, json_t
        if path.startswith("/media/"):
            return self._media(path)
        if path.rstrip("/") == "/mcp/_calls":
            return 200, {"calls": list(w.mcp_calls)}, json_t
        if path.rstrip("/") == "/mcp":
            return self._mcp(method, body)
        return 404, {"message": f"boss-e2e fakes: no fake for {method} {path}"}, json_t

    def _github(self, method: str, path: str, body: Any, query: str = "") -> tuple[int, Any, str]:
        if self.github is None:
            return 503, {"message": "boss-e2e: the GitHub fake needs --state-dir"}, "application/json"
        status, payload = self.github.handle(method, path or "/", query, body)
        accept = (self.headers.get("Accept") or "") if getattr(self, "headers", None) else ""
        if ("vnd.github.raw" in accept and status == 200 and isinstance(payload, dict)
                and payload.get("type") == "file" and payload.get("encoding") == "base64"):
            # application/vnd.github.raw: GitHub answers a file's bytes, not its JSON envelope.
            import base64
            return 200, base64.b64decode(payload.get("content") or ""), "application/vnd.github.raw"
        if status == 204:
            return 204, b"", ""
        return status, payload, "application/json"


    def _plane(self, method: str, path: str, body: Any) -> tuple[int, Any, str]:
        json_t = "application/json"
        if path.endswith("/_issues"):
            return 200, {"issues": self.world.issues}, json_t
        if "/issues" in path and method == "POST":
            issue = self.world.add("issues", {"path": path, "name": (body or {}).get("name")
                                              or (body or {}).get("title"),
                                              "description": str((body or {}).get("description_html")
                                                                 or (body or {}).get("body") or "")[:2000]})
            return 201, {"id": f"iss_{issue['number']}", "sequence_id": issue["number"],
                         "name": issue["name"]}, json_t
        if "/issues" in path:
            return 200, {"results": self.world.issues, "next_page_results": False}, json_t
        if path.rstrip("/").endswith("/projects"):
            return 200, {"results": [{"id": "proj_e2e", "name": "e2e", "identifier": "E2E"}]}, json_t
        if re.search(r"/projects/[^/]+/?$", path):
            # The connection test reads the project back by id.
            return 200, {"id": path.rstrip("/").rsplit("/", 1)[-1], "name": "e2e tracker",
                         "identifier": "E2E"}, json_t
        if "/states" in path or "/labels" in path or "/members" in path:
            return 200, {"results": []}, json_t
        return 200, {"ok": True}, json_t

    def _youtube(self, method: str, path: str, body: Any) -> tuple[int, Any, str]:
        json_t = "application/json"
        if path.endswith("/_videos"):
            return 200, {"videos": self.world.videos}, json_t
        if method in ("POST", "PUT"):
            video = self.world.add("videos", {"path": path, "snippet": (body or {}).get("snippet")
                                              if isinstance(body, dict) else None})
            return 200, {"id": f"e2e-video-{video['number']}", "kind": "youtube#video",
                         "status": {"uploadStatus": "uploaded"}}, json_t
        return 200, {"items": [{"id": "UC_e2e", "snippet": {"title": "e2e channel"}}]}, json_t

    def _mcp(self, method: str, body: Any) -> tuple[int, Any, str]:
        """An MCP server (JSON-RPC 2.0 over HTTP, what engine/mcp_client.py speaks) with one
        tool, `e2e_echo`, that hands back what it was given and keeps every call.

        WHY. The R1 journey registered an MCP server at this URL and nothing answered there, so
        no journey could bind one of its tools to a workflow step and prove the step called it —
        the R3 journey was a palette smoke check that said "R3 proven". A step bound to this
        tool leaves its call at /mcp/_calls, which is what the journey asserts on.
        """
        json_t = "application/json"
        if method != "POST" or not isinstance(body, dict):
            return 405, {"message": "boss-e2e MCP fake: POST a JSON-RPC request"}, json_t
        rid, rpc = body.get("id"), str(body.get("method") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        if rpc == "initialize":
            result: Any = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "boss-e2e-mcp", "version": "1"}}
        elif rpc == "tools/list":
            result = {"tools": [MCP_ECHO_TOOL]}
        elif rpc == "tools/call":
            name = str(params.get("name") or "")
            if name != MCP_ECHO_TOOL["name"]:
                return 200, {"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32602, "message": f"boss-e2e MCP fake has no tool {name!r}"}}, json_t
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            call = self.world.add("mcp_calls", {"tool": name, "arguments": arguments,
                                                "at": time.time()})
            result = {"content": [{"type": "text", "text": json.dumps(
                {"echoed": arguments, "call": call["number"], "tool": name})}]}
        else:
            return 200, {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"boss-e2e MCP fake: no method {rpc!r}"}}, json_t
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}, json_t

    def _media(self, path: str) -> tuple[int, Any, str]:
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".txt"):
            return 200, "An e2e brief: cut a 5-second clip with a title card.\n", "text/plain"
        if name.endswith(".png"):
            return 200, _PNG_1PX, "image/png"
        if name.endswith(".mp4"):
            clip = _test_clip(self.world.state_dir)
            if clip is None:
                return 503, {"message": "ffmpeg is not installed, so the fakes have no video to "
                                        "serve; install ffmpeg to drive the video example"}, \
                    "application/json"
            return 200, clip.read_bytes(), "video/mp4"
        return 404, {"message": f"no media {name}"}, "application/json"


# The one tool the MCP fake serves. Named here so the journeys bind exactly this tool.
MCP_ECHO_TOOL: dict[str, Any] = {
    "name": "e2e_echo",
    "description": "Echo the arguments back; the e2e harness's MCP fake keeps every call.",
    "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}},
}

_CLIP_LOCK = threading.Lock()


def _test_clip(state_dir: Optional[Path]) -> Optional[Path]:
    """A real five-second video with sound, made once with ffmpeg: the video example verifies
    its output is playable and has audio (video_edit.verify_playable), so a byte blob named
    .mp4 would fail for the wrong reason."""
    import shutil
    import subprocess

    if state_dir is None or not shutil.which("ffmpeg"):
        return None
    clip = state_dir / "clip.mp4"
    with _CLIP_LOCK:
        if not clip.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=15",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                 "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 str(clip)], check=False, capture_output=True, timeout=120)
    return clip if clip.exists() else None


_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def make_servers(port: int, smtp_port: int, state_dir: Optional[Path] = None,
                 host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, socketserver.ThreadingTCPServer, World]:
    world = World(state_dir)
    bound: dict[str, Any] = {"world": world}
    if state_dir is not None:
        bound["ca"] = CertificateAuthority(state_dir / "tls")
        bound["git"] = GitRemotes(state_dir / "git")
        bound["github"] = GitHubFake(bound["git"], world)
        bound["device"] = DeviceFlowFake()
        # The probes' full-access profile (github_probes.py), so Repo and models' Read / Write /
        # Pull-request checks pass on a repository picked after "Sign in with GitHub".
        bound["device"].fixed_token = "ghp_e2e_full"
    http = ThreadingHTTPServer((host, port), type("BoundHandler", (Handler,), bound))
    http.daemon_threads = True
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    smtp = socketserver.ThreadingTCPServer((host, smtp_port),
                                           type("BoundSmtp", (SmtpHandler,), {"world": world}))
    smtp.daemon_threads = True
    return http, smtp, world


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--smtp-port", type=int, default=0)
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args()
    http, smtp, _ = make_servers(args.port, args.smtp_port, args.state_dir, args.host)
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    print(f"fakes on http://{args.host}:{http.server_address[1]} smtp {smtp.server_address[1]}",
          flush=True)
    http.serve_forever()


if __name__ == "__main__":
    main()
