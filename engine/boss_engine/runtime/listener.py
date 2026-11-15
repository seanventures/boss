"""THE REPLICA'S TLS LISTENER on VIP:443 (P8b-9, BOSSAPP-1076; boss-agent-hosts-lld.md §3.13.1,
§3.13.2, §3.17.1-§3.17.3; explained.md Flow 2 ⑤-⑥).

    caller ──TLS (agent cert; optional client cert)──► VM: verify the caller LOCALLY
            (bearer: a BOSS key's checksum, or a JWT's signature against the cached JWKS;
             client certificate: the agent CA's caller bundle, not revoked)
         ──► every request: POST /v1/run-plane/admit {forwarded credential} through boss-hostd
             (BOSS re-runs the PDP agent:invoke; the VM never decides authorisation)
         ◄── JSON, or SSE on the SAME connection while the run drives in this VM

TLS ENDS HERE. The serving key is generated in the VM (after the snapshot reseed: one key per
replica), only its CSR leaves (`POST /v1/run-plane/replica/cert`), and the certificate from the
BOSS agent CA (24 h, SAN = the agent's DNS name + VIP) is renewed at two thirds of its life by
loading the new chain into the SAME SSL context: new handshakes get the new certificate, open
connections keep theirs (P8b-T4). The key is held in memory; it touches a 0600 file in a 0700
private directory only for the instant OpenSSL loads it.

HARDENING (§3.17.1 "one listener, connection limits, SYN cookies"): a bounded number of
concurrent connections (the excess is closed at accept), a large accept backlog so the kernel's
SYN cookies (net.ipv4.tcp_syncookies=1, set by boss-init) absorb a flood, TCP_DEFER_ACCEPT where
the kernel has it (a connection that never sends is never handed to a thread), a handshake and
header deadline, a 1 MiB body cap, and HTTP/1.1 with SSE streamed in chunks.

The handlers are protocols/agent_serving's (the same ones the control plane serves in-process),
over runtime/serving.RunPlaneBackend.
"""
from __future__ import annotations

import datetime as _dt
import http.server
import logging
import os
import shutil
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from typing import Any, Callable, Optional

from ..protocols.agent_serving import (MAX_BODY_BYTES, Backend, Caller, ServingRequest,
                                       dispatch, match)
from .client import RunPlaneClient

log = logging.getLogger(__name__)

MAX_CONNECTIONS = 256
BACKLOG = 4096
HANDSHAKE_SECONDS = 10.0
HEADER_SECONDS = 30.0
TRUST_TTL_SECONDS = 300.0
JWT_SKEW_SECONDS = 60
#: An unknown `kid` re-fetches the trust document at most this often (a stream of forged kids
#: never turns into a stream of run-plane calls).
UNKNOWN_KID_REFRESH_SECONDS = 30.0
#: How often the listener retries / re-reads the caller bundle for mTLS.
CALLER_BUNDLE_SECONDS = 30.0


class CallerRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── the serving identity: key in the VM, certificate from the agent CA ─────────────────────

class ServingIdentity:
    """The replica's TLS key (born here) and its certificate, renewed at two thirds."""

    def __init__(self, client: RunPlaneClient, *, common_name: str = "boss-runtime",
                 now: Callable[[], float] = time.time):
        self.client, self.common_name, self.now = client, common_name, now
        self.key: Any = None
        self.cert_pem: Optional[str] = None
        self.chain_pem: str = ""
        self.serial: Optional[str] = None
        self.not_after: Optional[_dt.datetime] = None
        self.renew_at: Optional[_dt.datetime] = None
        self.contexts: list = []
        self._lock = threading.Lock()

    def new_key(self) -> None:
        """A fresh P-256 key (at start, after the reseed; and on each renewal)."""
        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = ec.generate_private_key(ec.SECP256R1())

    def csr(self) -> str:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID

        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                           self.common_name)]))
               .sign(self.key, hashes.SHA256()))
        return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def obtain(self) -> dict:
        """Ask the agent CA for a certificate for a NEW key; load it into every context."""
        with self._lock:
            self.new_key()
            out = self.client.replica_cert(self.csr())
            self.cert_pem, self.chain_pem = out["certificate_pem"], out.get("chain_pem") or ""
            self.serial = out.get("serial")
            self.not_after = _dt.datetime.fromisoformat(out["not_after"])
            self.renew_at = _dt.datetime.fromisoformat(out["renew_after"])
            for context in self.contexts:
                self._load(context)
            return {k: v for k, v in out.items() if k != "_wake"}

    def due(self) -> bool:
        if self.renew_at is None:
            return True
        return _dt.datetime.fromtimestamp(self.now(), _dt.timezone.utc) >= self.renew_at

    def _load(self, context: ssl.SSLContext) -> None:
        from cryptography.hazmat.primitives import serialization

        private = tempfile.mkdtemp(prefix="boss-tls-")
        try:
            os.chmod(private, 0o700)
            path = os.path.join(private, "serving.pem")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(self.key.private_bytes(serialization.Encoding.PEM,
                                                    serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
                handle.write(((self.cert_pem or "") + self.chain_pem).encode("ascii"))
            # SSL_CTX_use_certificate on a live context: new handshakes get the new chain,
            # established connections keep theirs.
            context.load_cert_chain(path)
        finally:
            shutil.rmtree(private, ignore_errors=True)

    def server_context(self, *, caller_bundle_pem: str = "") -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= ssl.OP_NO_COMPRESSION
        context.set_alpn_protocols(["http/1.1"])
        if caller_bundle_pem:
            # mTLS is OPTIONAL: a caller may present a client certificate from the agent CA's
            # caller intermediate, or a bearer token, or both.
            context.verify_mode = ssl.CERT_OPTIONAL
            context.load_verify_locations(cadata=caller_bundle_pem)
        with self._lock:
            if self.cert_pem:
                self._load(context)
            self.contexts.append(context)
        return context

    def renew_loop(self, stop: threading.Event, *, retry_seconds: float = 30.0) -> None:
        while not stop.is_set():
            if self.due():
                try:
                    self.obtain()
                    log.info("serving certificate renewed (serial %s)", self.serial)
                except Exception as exc:  # noqa: BLE001 — retry; the old one is valid until expiry
                    log.warning("serving certificate renewal failed: %s", exc)
                    stop.wait(retry_seconds)
                    continue
            stop.wait(min(60.0, retry_seconds))


# ── the trust cache: JWKS, caller bundle, revoked serials, the signed card ─────────────────

class TrustCache:
    def __init__(self, client: RunPlaneClient, *, ttl: float = TRUST_TTL_SECONDS):
        self.client, self.ttl = client, ttl
        self._doc: Optional[dict] = None
        self._at = 0.0
        self._kid_refresh_at = -UNKNOWN_KID_REFRESH_SECONDS
        self._lock = threading.Lock()

    def doc(self, *, refresh: bool = False) -> dict:
        with self._lock:
            if refresh or self._doc is None or time.monotonic() - self._at > self.ttl:
                self._doc = self.client.trust()
                self._at = time.monotonic()
            return self._doc

    def card(self) -> Optional[dict]:
        return self.doc().get("card")

    def _key(self, kid: str) -> Any:
        from ..identity import jwks

        for refresh in (False, True):
            if refresh:
                # An unknown kid may be a key BOSS just rotated in: re-fetch, but rate-limited.
                with self._lock:
                    if time.monotonic() - self._kid_refresh_at < UNKNOWN_KID_REFRESH_SECONDS:
                        return None
                    self._kid_refresh_at = time.monotonic()
            for jwk in (self.doc(refresh=refresh).get("jwks") or {}).get("keys") or []:
                if jwk.get("kid") == kid:
                    try:
                        return jwks.public_key_from_jwk(jwk)
                    except Exception:  # noqa: BLE001
                        return None
        return None

    def verify_bearer(self, token: str) -> None:
        """LOCAL authn of a bearer (§3.17.3): a BOSS key must carry a valid checksum (its
        status is checked at admit); a JWT must verify against the cached JWKS and be unexpired.
        Anything else is refused here, before BOSS is asked."""
        from ..identity import jwks
        from ..identity.authn import parse_api_key

        parsed = parse_api_key(token)
        if parsed is not None:
            if not parsed.checksum_ok:
                raise CallerRefused("invalid_credentials")
            return
        try:
            unverified = jwks.split(token)
        except jwks.JoseError as exc:
            raise CallerRefused("invalid_credentials") from exc
        header = unverified.header
        if header.get("alg") != "EdDSA" or "crit" in header:
            raise CallerRefused("invalid_credentials")
        key = self._key(str(header.get("kid") or ""))
        if key is None:
            raise CallerRefused("invalid_credentials")
        try:
            key.verify(unverified.signature, unverified.signing_input)
        except Exception as exc:  # noqa: BLE001 — InvalidSignature
            raise CallerRefused("invalid_credentials") from exc
        claims, now = unverified.claims, time.time()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp + JWT_SKEW_SECONDS < now:
            raise CallerRefused("token_expired")
        nbf = claims.get("nbf")
        if nbf is not None and (not isinstance(nbf, (int, float)) or nbf - JWT_SKEW_SECONDS > now):
            raise CallerRefused("token_not_yet_valid")
        doc = self.doc()
        if not doc.get("issuer") or claims.get("iss") != doc["issuer"]:
            raise CallerRefused("invalid_credentials")
        aud = claims.get("aud")
        auds = {aud} if isinstance(aud, str) else set(aud) if isinstance(aud, list) else set()
        if not auds & set(doc.get("audiences") or []):
            raise CallerRefused("invalid_credentials")

    def check_client_cert(self, der: bytes) -> str:
        """The handshake verified the chain; here: not revoked. Returns the PEM to forward."""
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        if format(cert.serial_number, "x") in set(self.doc().get("revoked") or []):
            raise CallerRefused("client_certificate_revoked")
        return ssl.DER_cert_to_PEM_cert(der)


# ── the server ─────────────────────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "boss-runtime"
    sys_version = ""
    timeout = HEADER_SECONDS

    def log_message(self, fmt: str, *args: Any) -> None:  # never log headers or bodies
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: Any, headers: Optional[dict] = None) -> None:
        import json

        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status: int, code: str) -> None:
        self._send_json(status, {"detail": {"error": code, "message": code}},
                        {"WWW-Authenticate": 'Bearer realm="boss-agent"'} if status == 401
                        else None)

    def _caller(self) -> Caller:
        server: AgentListener = self.server  # type: ignore[assignment]
        authorization = (self.headers.get("Authorization") or "").strip() or None
        cert_pem = None
        try:
            der = self.connection.getpeercert(binary_form=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            der = None
        if der:
            cert_pem = server.trust.check_client_cert(der)
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() not in ("bearer", "dpop") or not value.strip():
                raise CallerRefused("invalid_credentials")
            server.trust.verify_bearer(value.strip())
        if not authorization and not cert_pem:
            raise CallerRefused("unauthenticated")
        tp = (self.headers.get("traceparent") or "").strip().lower() or None
        return Caller(authorization=authorization, client_cert_pem=cert_pem, traceparent=tp)

    def _serve(self, method: str) -> None:
        server: AgentListener = self.server  # type: ignore[assignment]
        path, _, qs = self.path.partition("?")
        from urllib.parse import parse_qsl

        raw_length = (self.headers.get("Content-Length") or "0").strip()
        if not raw_length.isdigit():   # negative, signed, or not a number: never read to EOF
            self.close_connection = True
            return self._refuse(400, "bad_content_length")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return self._refuse(413, "body_too_large")
        body = self.rfile.read(length) if length else b""
        name, _ = match(method, path)
        try:
            caller = Caller() if name == "card" else self._caller()
        except CallerRefused as refused:
            return self._refuse(401, refused.code)
        request = ServingRequest(method=method, path=path,
                                 headers={k.lower(): v for k, v in self.headers.items()},
                                 body=body, query=dict(parse_qsl(qs)))
        response = dispatch(server.backend, request, caller)
        if response.stream is None:
            return self._send_json(response.status, response.body, response.headers)
        # SSE back on the same connection, chunked; the connection closes with the stream.
        self.send_response(response.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        for k, v in response.headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True
        try:
            for frame in response.stream:
                data = frame.encode("utf-8")
                self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            log.debug("caller went away mid-stream")

    def do_GET(self) -> None:  # noqa: N802
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")


class AgentListener(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """VIP:443 (module doc). The TLS handshake happens in the connection's thread, under a
    deadline, so a slow handshake never blocks accept."""

    daemon_threads = True
    request_queue_size = BACKLOG
    allow_reuse_address = True

    def __init__(self, address: tuple, *, backend: Backend, trust: TrustCache,
                 identity: ServingIdentity, max_connections: int = MAX_CONNECTIONS):
        self.backend, self.trust, self.identity = backend, trust, identity
        self.max_connections = max(1, int(max_connections))
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self.refused_connections = 0
        try:
            bundle = trust.doc().get("caller_bundle_pem") or ""
        except Exception as exc:  # noqa: BLE001 — no mTLS until the trust bundle arrives
            log.warning("no caller bundle yet (mTLS off until it arrives): %s", exc)
            bundle = ""
        self.ssl_context = identity.server_context(caller_bundle_pem=bundle)
        self.caller_bundle = bundle
        self._closed = threading.Event()
        super().__init__(address, _Handler)
        threading.Thread(target=self._caller_bundle_loop, daemon=True,
                         name="caller-bundle").start()

    def reload_caller_bundle(self) -> bool:
        """Fetch the caller bundle and load it into the LIVE context (new handshakes verify
        against it). True when it changed. A failed fetch leaves the current one in place."""
        try:
            bundle = self.trust.doc(refresh=True).get("caller_bundle_pem") or ""
        except Exception as exc:  # noqa: BLE001 — retried by the loop
            log.warning("caller bundle fetch failed (retrying): %s", exc)
            return False
        if not bundle or bundle == self.caller_bundle:
            return False
        self.ssl_context.load_verify_locations(cadata=bundle)
        self.ssl_context.verify_mode = ssl.CERT_OPTIONAL
        self.caller_bundle = bundle
        log.info("caller bundle loaded: mTLS on")
        return True

    def _caller_bundle_loop(self) -> None:
        # Retry quickly while mTLS is off; afterwards re-read now and then (a new caller
        # intermediate is added; the old one stays trusted until its callers' certs expire).
        while not self._closed.wait(5.0 if not self.caller_bundle else CALLER_BUNDLE_SECONDS):
            self.reload_caller_bundle()

    def server_close(self) -> None:
        self._closed.set()
        super().server_close()

    def server_bind(self) -> None:
        if hasattr(socket, "TCP_DEFER_ACCEPT"):
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_DEFER_ACCEPT,
                                       int(HANDSHAKE_SECONDS))
            except OSError:
                pass
        super().server_bind()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.refused_connections += 1
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        tls = None
        try:
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            request.settimeout(HANDSHAKE_SECONDS)
            tls = self.ssl_context.wrap_socket(request, server_side=True)
            tls.settimeout(HEADER_SECONDS)
            self.finish_request(tls, client_address)
        except (ssl.SSLError, OSError, ConnectionError) as exc:
            log.debug("connection from %s refused at TLS: %s", client_address, exc)
        except Exception:  # noqa: BLE001
            self.handle_error(request, client_address)
        finally:
            self._slots.release()
            self.shutdown_request(tls or request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.warning("listener: error serving %s", client_address, exc_info=True)


def syncookies_on() -> Optional[bool]:
    """Whether the guest kernel has SYN cookies on (None when it cannot tell)."""
    try:
        with open("/proc/sys/net/ipv4/tcp_syncookies") as handle:
            return handle.read().strip() in ("1", "2")
    except OSError:
        return None


def serve(client: RunPlaneClient, backend: Optional[Backend] = None, *, host: str = "0.0.0.0",
          port: int = 443, max_connections: int = MAX_CONNECTIONS, inbox: Any = None,
          stop: Optional[threading.Event] = None) -> AgentListener:
    """Obtain the serving certificate, start the listener and the renewal loop (in threads).
    Runs it admits are driven in the work inbox's slots (`inbox.start_local`)."""
    from .serving import RunPlaneBackend

    if syncookies_on() is False:
        log.warning("net.ipv4.tcp_syncookies is off in this guest; a SYN flood fills the backlog")
    identity = ServingIdentity(client)
    identity.obtain()
    trust = TrustCache(client)
    if backend is None:
        backend = RunPlaneBackend(client, card=trust.card,
                                  start_local=getattr(inbox, "start_local", None))
    listener = AgentListener((host, port), backend=backend, trust=trust, identity=identity,
                             max_connections=max_connections)
    stop = stop or threading.Event()
    threading.Thread(target=listener.serve_forever, daemon=True, name="agent-listener").start()
    threading.Thread(target=identity.renew_loop, args=(stop,), daemon=True,
                     name="serving-cert-renewal").start()
    return listener


__all__ = ["AgentListener", "CallerRefused", "MAX_CONNECTIONS", "ServingIdentity", "TrustCache",
           "serve", "syncookies_on"]
