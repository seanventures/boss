"""The link gateway (F5, APP_TEMPLATES_AND_SECURITY_LOOP.md §5.4): app B reaches app A's preview,
and nothing else, on one port per active link (D8, `BOSS_LINK_PORTS`, default 18100-18199).

    B (build sandbox or preview)  --X-Boss-Link: boss_lnk_...-->  egress:<link port>  -->  api:<A's preview port>

Every request is judged by boss_engine.identity.link_rules.decide (the table and its order are
documented there): the pass, the link that owns the port, the consumer workload being live, the
canonical origin-form path, and the upstream being A's running preview on the preview host in
the preview port range. Never the console, the API, egress itself, another link, another preview
or anything off the preview host.

REVOCATION WITHIN 5 SECONDS (S5.4). The facts a decision needs are cached at most
link_rules.CACHE_SECONDS and the whole cache is dropped the moment Postgres delivers a NOTIFY on
`app_links` (create, revoke). A listener on a port whose link is gone is closed by the next sync
(every second, and on every NOTIFY), so a revoked link's port refuses connections too.

Nothing secret is logged: a line per refusal names the port, the link and the reason.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from boss_engine.app_links import service
from boss_engine.identity import link_rules

log = logging.getLogger("boss.egress.links")

MAX_BODY = 10 * 1024 * 1024


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


#: (method, url, headers, body) -> GatewayResponse. Replaced in tests.
Upstream = Callable[[str, str, dict[str, str], bytes], GatewayResponse]


def _httpx_upstream(method: str, url: str, headers: dict[str, str], body: bytes) -> GatewayResponse:
    import httpx

    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
        resp = client.request(method, url, headers=headers, content=body or None)
        return GatewayResponse(resp.status_code, list(resp.headers.multi_items()), resp.content)


def _refusal(decision: link_rules.LinkDecision) -> GatewayResponse:
    import json

    body = json.dumps({"error": "link_refused", "reason": decision.reason}).encode()
    return GatewayResponse(decision.status, [("content-type", "application/json")], body)


class LinkGateway:
    """The decision and the forward, for one request. Thread-safe."""

    def __init__(self, engine: Any, *, upstream: Optional[Upstream] = None,
                 resolve: Optional[Callable[[str], tuple[str, ...]]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.engine = engine
        self.upstream = upstream or _httpx_upstream
        self.resolve = resolve or _resolve
        self.clock = clock
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, key: tuple, load: Callable[[], Any]) -> Any:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < link_rules.CACHE_SECONDS:
                return hit[1]
        value = load()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def handle(self, port: int, method: str, target: str, headers: dict[str, str], body: bytes) -> GatewayResponse:
        headers = {k.lower(): v for k, v in headers.items()}
        presented = (headers.get(link_rules.PASS_HEADER) or "").strip()
        pass_key = service._hash(presented) if presented else ""

        def load_link():
            with self.engine.begin() as db:
                return service.link_on_port(db, port)

        def load_pass():
            with self.engine.begin() as db:
                return service.resolve_pass(db, presented)

        port_link = self._cached(("link", port), load_link)
        link_pass = self._cached(("pass", pass_key), load_pass) if presented else None
        consumer_live = False
        upstream = None
        if link_pass is not None and port_link is not None and link_pass["link_id"] == port_link["id"]:
            def load_live():
                with self.engine.begin() as db:
                    return service.consumer_live(db, link_pass)

            def load_upstream():
                with self.engine.begin() as db:
                    return service.provider_upstream(db, port_link)

            consumer_live = self._cached(("live", link_pass["consumer_kind"], link_pass["consumer_id"]), load_live)
            upstream = self._cached(("upstream", port_link["id"]), load_upstream)
        resolved = self.resolve(upstream[0]) if upstream else ()
        decision = link_rules.decide(method=method, target=target, headers=headers, port=port,
                                     port_link=port_link, link_pass=link_pass, consumer_live=consumer_live,
                                     upstream=upstream, policy=service.policy(), resolved=resolved)
        if not decision.allowed:
            log.info("link refused port=%s link=%s reason=%s", port, (port_link or {}).get("id"), decision.reason)
            return _refusal(decision)
        assert upstream is not None
        query = target.partition("?")[2]
        url = f"http://{upstream[0]}:{upstream[1]}{decision.path}" + (f"?{query}" if query else "")
        try:
            resp = self.upstream(method.upper(), url, link_rules.forward_headers(headers, upstream), body)
        except Exception:  # noqa: BLE001 — the provider is down: say so, name nothing
            return GatewayResponse(502, [("content-type", "application/json")],
                                   b'{"error":"link_refused","reason":"provider_unreachable"}')
        try:
            with self.engine.begin() as db:
                service.touch_used(db, port_link["id"])
        except Exception:  # noqa: BLE001 — "last used" is a hint, never a reason to fail a call
            pass
        return GatewayResponse(resp.status, link_rules.response_headers(resp.headers), resp.body)


def _resolve(host: str) -> tuple[str, ...]:
    try:
        return tuple(sorted({i[4][0] for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}))
    except OSError:
        return ("unresolvable",)


def _handler(gateway: LinkGateway, port: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "boss-link"
        sys_version = ""

        def log_message(self, *_args: Any) -> None:      # never the request line: it can carry a query
            return

        def _serve(self) -> None:
            if self.headers.get("transfer-encoding"):
                self._send(GatewayResponse(411, [("content-type", "application/json")],
                                           b'{"error":"link_refused","reason":"length_required"}'))
                return
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY:
                self._send(GatewayResponse(413, [], b""))
                return
            body = self.rfile.read(length) if length else b""
            headers = {k: v for k, v in self.headers.items()}
            self._send(gateway.handle(port, self.command, self.path, headers, body))

        def _send(self, resp: GatewayResponse) -> None:
            self.send_response(resp.status)
            for k, v in resp.headers:
                self.send_header(k, v)
            self.send_header("content-length", str(len(resp.body)))
            self.send_header("connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_CONNECT = _serve

    return Handler


class LinkListeners:
    """One listening socket per active preview link's port, opened and closed by `sync`."""

    def __init__(self, gateway: LinkGateway, host: str = "0.0.0.0"):
        self.gateway = gateway
        self.host = host
        self.servers: dict[int, ThreadingHTTPServer] = {}
        self._lock = threading.Lock()

    def sync(self) -> None:
        with self.gateway.engine.begin() as db:
            wanted = set(service.active_ports(db))
        pool = service.link_ports()
        wanted = {p for p in wanted if p in pool}
        with self._lock:
            for port in sorted(set(self.servers) - wanted):
                self._close(port)
            for port in sorted(wanted - set(self.servers)):
                try:
                    srv = ThreadingHTTPServer((self.host, port), _handler(self.gateway, port))
                except OSError as exc:
                    log.warning("link port %s could not be opened: %s", port, exc)
                    continue
                srv.daemon_threads = True
                threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True,
                                 name=f"link-{port}").start()
                self.servers[port] = srv

    def _close(self, port: int) -> None:
        srv = self.servers.pop(port, None)
        if srv is not None:
            srv.shutdown()
            srv.server_close()

    def close(self) -> None:
        with self._lock:
            for port in list(self.servers):
                self._close(port)


def run(engine: Any, stop: threading.Event, *, host: str = "0.0.0.0", gateway: Optional[LinkGateway] = None,
        listeners: Optional[LinkListeners] = None) -> None:
    """LISTEN app_links; on every notice (and every second) drop the cache and sync the ports.
    Reconnects after any database error."""
    gateway = gateway or LinkGateway(engine)
    listeners = listeners or LinkListeners(gateway, host)
    delay = 0.5
    while not stop.is_set():
        raw = None
        try:
            raw = engine.raw_connection()
            pg = raw.driver_connection
            pg.autocommit = True
            pg.execute(f"LISTEN {service.NOTIFY_CHANNEL}")
            gateway.invalidate()
            listeners.sync()
            delay = 0.5
            while not stop.is_set():
                noticed = False
                for _note in pg.notifies(timeout=1.0, stop_after=1):
                    noticed = True
                if noticed:
                    gateway.invalidate()
                listeners.sync()
        except Exception:  # noqa: BLE001 — the database went away: reconnect
            log.warning("link gateway lost its database connection; reconnecting in %.1fs", delay)
            stop.wait(delay)
            delay = min(delay * 2, 30.0)
        finally:
            if raw is not None:
                # NEVER BACK INTO THE POOL: this connection is autocommit and LISTENing. Pooled, the
                # next engine.begin() on it ran without a transaction (no rollback, and every
                # SAVEPOINT failed: NoActiveSqlTransaction), so it is discarded instead.
                try:
                    raw.invalidate()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    raw.close()
                except Exception:  # noqa: BLE001
                    pass
    listeners.close()


def enabled() -> bool:
    return (os.environ.get("BOSS_LINK_GATEWAY") or "on").strip().lower() != "off"
