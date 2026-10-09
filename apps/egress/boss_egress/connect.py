"""The sandbox's only route to the internet that is not GitHub: an allowlisted HTTPS CONNECT proxy.

A sandboxed process (scripts/sandbox/entrypoint.sh) can reach loopback, the BOSS API and
boss-egress, nothing else. Its model calls (the coding CLI), package installs and browser
downloads go through here as `HTTPS_PROXY=http://egress:8091`:

* CONNECT only, to port 443 only, to a host on the allowlist (exact names, or `*.suffix`);
* the name is resolved HERE and every address must be public: an allowlisted name that resolves
  to the database, the API, a link-local metadata service or any private range is refused, so the
  allowlist can never become a way back into BOSS's own network;
* the tunnel is opaque TLS: no MITM, no CA to inject, nothing decrypted.

`BOSS_EGRESS_CONNECT_ALLOW` replaces the default allowlist (comma-separated); GitHub is never on
it — GitHub goes through the credential-injecting proxy, under the run's rules.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from typing import Iterable, Optional

log = logging.getLogger("boss.egress.connect")

DEFAULT_ALLOW = (
    # model providers the coding CLIs call
    "api.anthropic.com", "statsig.anthropic.com", "console.anthropic.com", "claude.ai",
    "api.openai.com", "chatgpt.com", "auth.openai.com", "api.x.ai",
    "generativelanguage.googleapis.com", "openrouter.ai",
    # package registries and toolchain downloads
    "registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
    "playwright.azureedge.net", "playwright-akamai.azureedge.net", "playwright-verizon.azureedge.net",
    "cdn.playwright.dev", "nodejs.org",
    # Java (an opt-in toolchain, docs/community/TOOLCHAINS.md): Maven Central and the Gradle
    # plugin portal (which redirects its downloads to plugins-artifacts). Nothing broader: no
    # services.gradle.org, so a project's ./gradlew cannot fetch a distribution; BOSS runs the
    # image's pinned Gradle instead.
    "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "plugins-artifacts.gradle.org",
)
#: Never through the tunnel, whatever the allowlist says.
NEVER = ("github.com", "*.github.com", "*.githubusercontent.com", "api.github.com")
MAX_HEADER = 8192


def allowlist() -> tuple[str, ...]:
    raw = (os.environ.get("BOSS_EGRESS_CONNECT_ALLOW") or "").strip()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip()) if raw else DEFAULT_ALLOW


def _matches(host: str, patterns: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for p in patterns:
        if p.startswith("*.") and host.endswith(p[1:]) and host != p[2:]:
            return True
        if host == p:
            return True
    return False


def host_allowed(host: str) -> bool:
    return _matches(host, allowlist()) and not _matches(host, NEVER)


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> Optional[tuple]:
    """The first public address of `host`, or None if ANY address it resolves to is not public
    (a DNS answer mixing a public and a private address is refused, not filtered)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos or not all(_public(i[4][0]) for i in infos):
        return None
    return infos[0]


def parse_connect(head: bytes) -> Optional[tuple[str, int]]:
    """`CONNECT host:443 HTTP/1.1` -> (host, 443), else None."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not port.isdigit() or not host or host.startswith("["):
        return None
    return host.lower(), int(port)


def decide(head: bytes) -> tuple[Optional[tuple], str]:
    """(address to dial, "") or (None, reason)."""
    target = parse_connect(head)
    if target is None:
        return None, "not_a_connect"
    host, port = target
    if port != 443:
        return None, "port_not_allowed"
    if not host_allowed(host):
        return None, "host_not_allowed"
    info = resolve_public(host, port)
    if info is None:
        return None, "not_a_public_address"
    return info, ""


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket) -> None:
    client.settimeout(30)
    head = b""
    try:
        while b"\r\n\r\n" not in head and len(head) < MAX_HEADER:
            chunk = client.recv(1024)
            if not chunk:
                return
            head += chunk
        info, reason = decide(head)
        if info is None:
            log.info("connect refused: %s", reason)
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        upstream = socket.socket(info[0], info[1], info[2])
        upstream.settimeout(30)
        upstream.connect(info[4])
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        rest = head.split(b"\r\n\r\n", 1)[1]
        if rest:
            upstream.sendall(rest)
        client.settimeout(None)
        upstream.settimeout(None)
        t = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
        t.start()
        _pipe(client, upstream)
        t.join(timeout=5)
        upstream.close()
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def serve_forever(host: str, port: int) -> None:  # pragma: no cover — exercised by the A51 container test
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
