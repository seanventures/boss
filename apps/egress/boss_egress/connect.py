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
    # The security review's dependency audit (OSV-Scanner, online by default: decision D6).
    "api.osv.dev",
)
#: OPT-IN TOOLCHAINS' HOSTS (docs/community/TOOLCHAINS.md), allowed only when THIS image carries the
#: toolchain (its /opt/toolchains/manifest.json; egress is built from the same python-runtime
#: stage as the api and worker, with the same BOSS_TOOLCHAINS). Every allowlisted host is a way
#: OUT as well as in: a package proxy that accepts arbitrary paths (proxy.golang.org fetches any
#: public module path on request; Maven Central and the Gradle portal take any GET) can carry
#: data a sandboxed run encodes into the requests it makes. That is why each list is the
#: smallest that builds, and why an install without the toolchain does not open them at all.
TOOLCHAIN_ALLOW = {
    # Java: Maven Central and the Gradle plugin portal (which redirects its downloads to
    # plugins-artifacts). Nothing broader: no services.gradle.org, so a project's ./gradlew
    # cannot fetch a distribution; BOSS runs the image's pinned Gradle instead.
    "java": ("repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org",
             "plugins-artifacts.gradle.org"),
    # Go: the module proxy and the checksum database (GOPROXY/GOSUMDB, set per run by
    # toolchains.sandbox_env and in the image's go.env). The proxy serves module zips itself (no
    # redirect to storage.googleapis.com), and with GOPROXY set to it there is no `direct` VCS
    # fallback, so no source host is needed.
    "go": ("proxy.golang.org", "sum.golang.org"),
}


def toolchain_hosts() -> tuple[str, ...]:
    """The hosts of the opt-in toolchains this image carries (none when the manifest is absent)."""
    try:
        from boss_engine.services import toolchains

        have = toolchains.installed()
    except Exception as exc:  # noqa: BLE001 — fail closed: no toolchain hosts
        log.warning("could not read the toolchain manifest; no toolchain hosts allowed: %s", exc)
        return ()
    return tuple(h for name, hosts in TOOLCHAIN_ALLOW.items() if name in have for h in hosts)


#: Never through the tunnel, whatever the allowlist says.
NEVER = ("github.com", "*.github.com", "*.githubusercontent.com", "api.github.com")
MAX_HEADER = 8192


#: STRICT-SECRETS MODE (P8b S10, boss-agent-hosts-lld.md §3.11): on a strict host the only path
#: to a model is the injecting route (the model gateway), so model hosts leave the opaque CONNECT
#: allowlist — an exfiltrated subscription token or key cannot be used through the tunnel.
STRICT_ENV = "BOSS_EGRESS_STRICT_SECRETS"


def strict_secrets() -> bool:
    return (os.environ.get(STRICT_ENV) or "").strip().lower() in ("1", "true", "yes")


def _model_hosts() -> tuple[str, ...]:
    from boss_engine.services.streaming_model_gateway import MODEL_HOSTS

    return MODEL_HOSTS


def allowlist() -> tuple[str, ...]:
    raw = (os.environ.get("BOSS_EGRESS_CONNECT_ALLOW") or "").strip()
    hosts = (tuple(h.strip().lower() for h in raw.split(",") if h.strip()) if raw
             else DEFAULT_ALLOW + toolchain_hosts())
    if strict_secrets():
        hosts = tuple(h for h in hosts if not _matches(h, _model_hosts()) and h not in _model_hosts())
    return hosts


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
