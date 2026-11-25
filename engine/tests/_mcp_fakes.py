"""Fake MCP servers on loopback for the MCP conformance test (BOSSAPP-1476).

Real sockets, real HTTP: BOSS's client code runs exactly as it does against a hosted server.
One server class covers every HTTP transport row:

  streamable      Streamable HTTP, legacy era: initialize + Mcp-Session-Id, JSON answers
  streamable_sse  the same, every answer sent as a `text/event-stream` body
  sse             the 2024-11-05 HTTP+SSE transport: GET <path> opens a stream whose first
                  event names the message endpoint; answers arrive on that stream

and the auth rows (none, bearer, a named header), a slow tool for timeouts and cancellation,
a failing tool, and any tool list a test hands it (schema quirks). A tiny forward proxy
(`FakeProxy`) records what is sent through it.
"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

DEFAULT_TOOLS = [
    {"name": "echo", "description": "Echo back the text you send.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "slow", "description": "Sleep for `seconds`, then answer.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "broken", "description": "Always reports an error.",
     "inputSchema": {"type": "object"}},
]


class FakeMcp:
    def __init__(self, mode: str = "streamable", *, auth: Optional[tuple] = None,
                 tools: Optional[list[dict]] = None, path: str = "/mcp",
                 tls: Optional[Any] = None) -> None:
        assert mode in ("streamable", "streamable_sse", "sse")
        self.mode = mode
        self.auth = auth
        self.tools = list(tools if tools is not None else DEFAULT_TOOLS)
        self.path = path if mode != "sse" else (path if path != "/mcp" else "/sse")
        self.seen: list[dict[str, Any]] = []
        self.cancelled: list[Any] = []
        self.unauthorized = 0
        self.sessions: set[str] = set()
        self._streams: dict[str, "queue.Queue[Optional[dict]]"] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self.scheme = "http"
        if tls is not None:
            # An HTTPS server (tls: an ssl.SSLContext with its certificate loaded).
            self._server.socket = tls.wrap_socket(self._server.socket, server_side=True)
            self.scheme = "https"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # ------------------------------------------------------------------ plumbing
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}{self.path}"

    def url_on(self, host: str) -> str:
        return f"{self.scheme}://{host}:{self.port}{self.path}"

    def close(self) -> None:
        for stream in list(self._streams.values()):
            stream.put(None)
        self._server.shutdown()
        self._server.server_close()

    def authorized(self, headers: Any) -> bool:
        if not self.auth:
            return True
        kind = self.auth[0]
        if kind == "bearer":
            return headers.get("Authorization") == f"Bearer {self.auth[1]}"
        if kind == "header":
            return headers.get(self.auth[1]) == self.auth[2]
        return False

    # ------------------------------------------------------------------ JSON-RPC
    def answer(self, message: dict) -> Optional[dict]:
        """The response to one client message (None for a notification)."""
        method = message.get("method")
        params = message.get("params") or {}
        self.seen.append({"method": method, "params": params})
        if "id" not in message:
            if method == "notifications/cancelled":
                self.cancelled.append(params.get("requestId"))
            return None
        rid = message["id"]

        def ok(result: Any) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        if method == "initialize":
            return ok({"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": f"fake-{self.mode}", "version": "1"}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tools})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                return ok({"content": [{"type": "text",
                                        "text": json.dumps({"echo": args.get("text")})}]})
            if name == "slow":
                time.sleep(float(args.get("seconds") or 1))
                return ok({"content": [{"type": "text", "text": json.dumps({"slept": True})}]})
            if name == "broken":
                return ok({"isError": True, "content": [{"type": "text",
                                                         "text": "the widget is jammed"}]})
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}


def _sse(message: dict) -> bytes:
    return f"event: message\ndata: {json.dumps(message)}\n\n".encode()


def _handler(fake: FakeMcp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a: Any) -> None:
            return

        def _send(self, status: int, body: bytes = b"", kind: str = "application/json",
                  extra: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _deny(self) -> bool:
            if fake.authorized(self.headers):
                return False
            fake.unauthorized += 1
            self._send(401, b'{"error":"unauthorized"}',
                       extra={"WWW-Authenticate": 'Bearer realm="fake"'})
            return True

        # -------------------------------------------------- the HTTP+SSE transport
        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            if fake.mode != "sse" or parts.path != fake.path:
                self._send(405)
                return
            if self._deny():
                return
            sid = secrets.token_hex(8)
            stream: "queue.Queue[Optional[dict]]" = queue.Queue()
            fake._streams[sid] = stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(f"event: endpoint\ndata: /messages?session_id={sid}\n\n"
                                 .encode())
                self.wfile.flush()
                while True:
                    try:
                        item = stream.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    if item is None:
                        return
                    self.wfile.write(_sse(item))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                fake._streams.pop(sid, None)

        def do_DELETE(self) -> None:  # noqa: N802
            self._send(200)

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            message = json.loads(self.rfile.read(length) or b"null")
            if fake.mode == "sse":
                if parts.path == fake.path:
                    # The old transport has no POST on its stream URL.
                    self._send(405)
                    return
                if parts.path != "/messages":
                    self._send(404)
                    return
                if self._deny():
                    return
                sid = (parse_qs(parts.query).get("session_id") or [""])[0]
                stream = fake._streams.get(sid)
                if stream is None:
                    self._send(404, b'{"error":"no such session"}')
                    return
                self._send(202, b"Accepted", kind="text/plain")

                def work() -> None:
                    answer = fake.answer(message)
                    if answer is not None:
                        stream.put(answer)

                threading.Thread(target=work, daemon=True).start()
                return
            if parts.path != fake.path:
                self._send(404)
                return
            if self._deny():
                return
            method = message.get("method") if isinstance(message, dict) else None
            extra = {}
            if method == "initialize":
                sid = secrets.token_hex(8)
                fake.sessions.add(sid)
                extra["Mcp-Session-Id"] = sid
            answer = fake.answer(message)
            if answer is None:
                self._send(202)
                return
            if fake.mode == "streamable_sse":
                self._send(200, b": hello\n\n" + _sse(answer), kind="text/event-stream",
                           extra=extra)
            else:
                self._send(200, json.dumps(answer).encode(), extra=extra)

    return Handler


class FakeProxy:
    """A plain-HTTP forward proxy: records each absolute-URI request and forwards it."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.requests: list[str] = []
        self.refuse = refuse
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                return

            def _forward(self, method: str) -> None:
                proxy.requests.append(f"{method} {self.path}")
                if proxy.refuse:
                    body = b"Blocked by the company proxy"
                    self.send_response(403)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "proxy-connection", "content-length",
                                                "connection")}
                # Like a company proxy resolving internal names: every host is this machine.
                target = urlsplit(self.path)
                local = target._replace(netloc=f"127.0.0.1:{target.port or 80}").geturl()
                with httpx.Client(trust_env=False, timeout=30) as client:
                    upstream = client.request(method, local, content=data, headers=headers)
                body = upstream.content
                self.send_response(upstream.status_code)
                for k, v in upstream.headers.items():
                    if k.lower() not in ("content-length", "transfer-encoding", "connection",
                                         "content-encoding"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                self._forward("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._forward("GET")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def make_tls(directory: Any, names: tuple[str, ...] = ()) -> tuple[Any, str]:
    """A private CA and a certificate for 127.0.0.1 it signed: (server SSLContext, CA PEM path).
    The CA plays the company's root (a TLS-inspecting gateway, an internal server)."""
    import datetime
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example Corp Root CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                       crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False,
                                       decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                         critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                 x509.DNSName("localhost"), *[x509.DNSName(n) for n in names]]),
                critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_path = f"{directory}/extra-ca.pem"
    with open(ca_path, "wb") as fh:
        fh.write(ca.public_bytes(pem))
    cert_path, key_path = f"{directory}/leaf.pem", f"{directory}/leaf.key"
    with open(cert_path, "wb") as fh:
        fh.write(leaf.public_bytes(pem))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, ca_path


class FakeConnectProxy:
    """An HTTPS proxy as a company runs one: CONNECT host:port, every host tunnelled to this
    machine's same port (the company DNS knows names this machine does not). Records each
    CONNECT; `refuse` answers 403 instead."""

    def __init__(self, *, refuse: bool = False) -> None:
        import socket

        self.connects: list[str] = []
        self.refuse = refuse
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                return

            def do_CONNECT(self) -> None:  # noqa: N802
                proxy.connects.append(self.path)
                if proxy.refuse:
                    self.send_response(403, "Blocked by the company proxy")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                port = int(self.path.rsplit(":", 1)[1])
                upstream = socket.create_connection(("127.0.0.1", port), timeout=30)
                self.send_response(200, "Connection established")
                self.end_headers()
                self.close_connection = True
                client = self.connection

                def pipe(src: Any, dst: Any) -> None:
                    try:
                        while True:
                            data = src.recv(65536)
                            if not data:
                                break
                            dst.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

                back = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
                back.start()
                pipe(client, upstream)
                back.join(timeout=30)
                upstream.close()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
