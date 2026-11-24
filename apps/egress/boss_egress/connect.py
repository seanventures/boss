"""The sandbox's only route to the internet that is not GitHub: an allowlisted HTTPS CONNECT proxy.

A sandboxed process (scripts/sandbox/entrypoint.sh) can reach loopback, the BOSS API and
boss-egress, nothing else. Its model calls (the coding CLI), package installs and browser
downloads go through here as `HTTPS_PROXY=http://egress:8091`:

* CONNECT only, to a host on the allowlist (exact names, or `*.suffix`), on port 443 unless the
  entry names another port (`host:8443`);
* the name is resolved HERE and every address must be public: an allowlisted name that resolves
  to the database, the API, a link-local metadata service or any private range is refused, so the
  allowlist can never become a way back into BOSS's own network. The one opt-in exception is
  BOSS_EGRESS_PRIVATE_ALLOW: exact host names (an internal package registry) that may resolve to
  a private address, still never to loopback, link-local (cloud metadata), multicast, IPv6
  unique-local, the Docker host (host.docker.internal / host-gateway), or one of this container's
  own networks (where BOSS's services live);
* the tunnel is opaque TLS: no MITM, no CA to inject, nothing decrypted.

THE ALLOWLIST (docs/community/ENTERPRISE_NETWORK.md) is the built-in hosts below, PLUS the hosts
of the registries the operator configured (BOSS_NPM_REGISTRY, BOSS_PIP_INDEX_URL, BOSS_GOPROXY,
BOSS_MAVEN_MIRROR, BOSS_PLAYWRIGHT_DOWNLOAD_HOST: enterprise_network.registry_endpoints), PLUS
`BOSS_EGRESS_CONNECT_ALLOW` (comma-separated; it ADDS to the built-in list. It used to replace
it, so a missed model host silently broke model calls). `BOSS_EGRESS_CONNECT_DEFAULTS=off` drops
the built-in list for an operator who wants only their own. GitHub is never on it — GitHub goes
through the credential-injecting proxy, under the run's rules.

A REFUSAL SAYS WHY, IN THE TOOL'S OWN ERROR. The 403's reason phrase names the host and the
setting to change; npm, pip and Go print the reason phrase (npm otherwise says "E403 forbidden by
your security policy", which reads exactly like a registry token problem). It is logged with the
host, port and reason.

AN UPSTREAM PROXY (BOSS_UPSTREAM_PROXY) is used for every allowed host that is not in
BOSS_UPSTREAM_NO_PROXY: the tunnel is chained with a CONNECT to it, for networks with no direct
route out. A name the company DNS cannot resolve is then left to the proxy to resolve; a name it
does resolve is still held to the address rules above.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import ssl
import struct
import threading
from typing import Iterable, NamedTuple, Optional, Union

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
#: toolchain (its /opt/toolchains/manifest.json: the slim egress image copies the same manifest the
#: api and worker carry, built with the same BOSS_TOOLCHAINS). Every allowlisted host is a way
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

ALLOW_ENV = "BOSS_EGRESS_CONNECT_ALLOW"
DEFAULTS_ENV = "BOSS_EGRESS_CONNECT_DEFAULTS"
PRIVATE_ENV = "BOSS_EGRESS_PRIVATE_ALLOW"


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


def _defaults_on() -> bool:
    return (os.environ.get(DEFAULTS_ENV) or "on").strip().lower() not in ("off", "0", "false", "no")


def _enterprise():
    from boss_engine.services import enterprise_network

    return enterprise_network


class Rule(NamedTuple):
    pattern: str            # exact host or *.suffix
    port: int               # the one port this rule opens (443 unless the entry named one)
    private: bool = False   # BOSS_EGRESS_PRIVATE_ALLOW: may resolve to a private address


def allow_rules() -> tuple[Rule, ...]:
    """Every rule the fence applies, from all four sources (see the module docstring)."""
    rules: list[Rule] = []
    if _defaults_on():
        rules += [Rule(h, 443) for h in DEFAULT_ALLOW + toolchain_hosts()]
    try:
        en = _enterprise()
        rules += [Rule(h, p) for h, p in en.registry_endpoints(os.environ)]
        rules += [Rule(h, p or 443) for h, p in en.allow_entries(os.environ, ALLOW_ENV)]
        rules += [Rule(h, p or 443, True) for h, p in en.allow_entries(os.environ, PRIVATE_ENV)]
    except Exception as exc:  # noqa: BLE001 — fail closed: only the built-in list
        log.warning("could not read the enterprise network settings; built-in hosts only: %s", exc)
    if strict_secrets():
        models = _model_hosts()
        rules = [r for r in rules if not _matches(r.pattern, models) and r.pattern not in models]
    return tuple(dict.fromkeys(rules))


def allowlist() -> tuple[str, ...]:
    """The allowed host patterns (any port)."""
    return tuple(dict.fromkeys(r.pattern for r in allow_rules()))


def _matches(host: str, patterns: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for p in patterns:
        if p.startswith("*.") and host.endswith(p[1:]) and host != p[2:]:
            return True
        if host == p:
            return True
    return False


def _rules_for(host: str) -> list[Rule]:
    if _matches(host, NEVER):
        return []
    return [r for r in allow_rules() if _matches(host, (r.pattern,))]


def host_allowed(host: str, port: int = 443) -> bool:
    return any(r.port == port for r in _rules_for(host))


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    return ip.is_global and not ip.is_multicast


ROUTE_FILE = "/proc/net/route"
IPV6_ROUTE_FILE = "/proc/net/ipv6_route"
#: Addresses that reach the Docker HOST itself (its localhost listeners), whatever this container's
#: routes say: Docker's default bridge gateway, Docker Desktop's host address, Lima/Colima's host.
#: Never a target, even for an opted-in private name.
KNOWN_HOST_ADDRESSES = frozenset({"172.17.0.1", "192.168.65.254", "192.168.65.1", "192.168.65.2",
                                  "192.168.5.2"})
HOST_NAMES = ("host.docker.internal", "gateway.docker.internal", "host.lima.internal")


def own_networks() -> list:
    """This container's directly attached networks (where BOSS's own services are reachable),
    IPv4 and IPv6, read from the kernel's routing tables as the sandbox fence reads them."""
    nets: list = []
    try:
        with open(ROUTE_FILE) as fh:
            next(fh)
            for line in fh:
                f = line.split()
                dest, mask = int(f[1], 16), int(f[7], 16)
                if dest == 0 or mask == 0:
                    continue
                nets.append(ipaddress.ip_network(
                    f"{socket.inet_ntoa(struct.pack('<I', dest))}/{bin(mask).count('1')}", strict=False))
    except (OSError, ValueError, StopIteration, IndexError):
        pass
    try:
        with open(IPV6_ROUTE_FILE) as fh:
            for line in fh:
                f = line.split()
                dest, plen = f[0], int(f[1], 16)
                if plen == 0 or len(dest) != 32:
                    continue
                addr = ipaddress.IPv6Address(bytes.fromhex(dest))
                if addr.is_multicast:
                    continue
                nets.append(ipaddress.ip_network(f"{addr}/{plen}", strict=False))
    except (OSError, ValueError, IndexError):
        pass
    return nets


def host_gateway_addresses() -> set[str]:
    """Every address that is the Docker host from here: this container's default gateway(s), the
    names Docker and Colima give the host (host-gateway), and the known host addresses."""
    found = set(KNOWN_HOST_ADDRESSES)
    try:
        with open(ROUTE_FILE) as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if int(f[1], 16) == 0 and int(f[2], 16) != 0:
                    found.add(socket.inet_ntoa(struct.pack("<I", int(f[2], 16))))
    except (OSError, ValueError, StopIteration, IndexError):
        pass
    for name in HOST_NAMES:
        try:
            found.update(socket.gethostbyname_ex(name)[2])
        except (OSError, UnicodeError):
            continue
    return found


#: IPv6 unique-local space: cloud metadata (fd00:ec2::254) and host-internal networks live here.
_ULA = ipaddress.ip_network("fc00::/7")


def _private_target_ok(addr: str) -> bool:
    """A private address an opted-in internal host may use: never loopback, link-local (the cloud
    metadata service), multicast, unspecified or reserved, never IPv6 unique-local, never the
    Docker host (host.docker.internal / host-gateway, the default gateway), never one of this
    container's own networks (BOSS's database, API and other services)."""
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip.version == 6 and ip in _ULA:
        return False
    if str(ip) in host_gateway_addresses():
        return False
    return not any(ip.version == net.version and ip in net for net in own_networks())


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


class Upstream(NamedTuple):
    """Chain this tunnel through the operator's proxy (BOSS_UPSTREAM_PROXY)."""
    proxy_host: str
    proxy_port: int
    tls: bool
    host: str
    port: int


Route = Union[tuple, Upstream]


def _upstream_for(host: str) -> Optional[Upstream]:
    raw = (os.environ.get("BOSS_UPSTREAM_PROXY") or "").strip()
    if not raw:
        return None
    try:
        en = _enterprise()
        hp = en.upstream_proxy(os.environ)
        if hp is None or en.bypasses_proxy(host, os.environ):
            return None
    except Exception as exc:  # noqa: BLE001 — a refused proxy URL: dial directly, say so
        log.warning("BOSS_UPSTREAM_PROXY is not usable (%s); dialling directly", exc)
        return None
    return Upstream(hp[0], hp[1], raw.lower().startswith("https://"), host, 0)


def decide(head: bytes) -> tuple[Optional[Route], str]:
    """(where to dial, "") or (None, reason). Reasons: not_a_connect, host_not_allowed,
    port_not_allowed, unresolvable, not_a_public_address."""
    target = parse_connect(head)
    if target is None:
        return None, "not_a_connect"
    host, port = target
    rules = _rules_for(host)
    if not rules:
        return None, "host_not_allowed"
    rules = [r for r in rules if r.port == port]
    if not rules:
        return None, "port_not_allowed"
    private_ok = any(r.private for r in rules)
    upstream = _upstream_for(host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        infos = []
    if not infos:
        # The company DNS may not resolve outside names on a proxy-only network: the proxy does.
        if upstream is not None:
            return upstream._replace(port=port), ""
        return None, "unresolvable"
    for info in infos:
        addr = info[4][0]
        if _public(addr):
            continue
        if private_ok and _private_target_ok(addr):
            continue
        return None, "not_a_public_address"
    if upstream is not None:
        return upstream._replace(port=port), ""
    return infos[0], ""


_SAFE_HOST = re.compile(r"[^a-z0-9.*-]")


def refusal_reason(host: str, port: int, reason: str) -> str:
    """The 403's reason phrase: ASCII, one line, names the host and the setting to change."""
    host = _SAFE_HOST.sub("", host.lower())[:200] or "this host"
    where = host if port == 443 else f"{host}:{port}"
    fence = "Blocked by BOSS network fence"
    if reason == "host_not_allowed" and _matches(host, NEVER):
        return f"{fence}: {host} is reached through BOSS's GitHub proxy, not this tunnel"
    if reason == "host_not_allowed":
        return (f"{fence}: {host} is not on the egress allowlist. An operator can add {where} "
                "to BOSS_EGRESS_CONNECT_ALLOW in .env (see docs/community/ENTERPRISE_NETWORK.md)")
    if reason == "port_not_allowed":
        return (f"{fence}: port {port} is not open for {host}. An operator can add {where} "
                "to BOSS_EGRESS_CONNECT_ALLOW in .env")
    if reason == "not_a_public_address":
        return (f"{fence}: {host} resolves to a private address. If it is an internal registry, "
                f"an operator can add {where} to BOSS_EGRESS_PRIVATE_ALLOW in .env")
    if reason == "unresolvable":
        return (f"{fence}: {host} does not resolve from BOSS. Check DNS, or set "
                "BOSS_UPSTREAM_PROXY in .env if this network only reaches out through a proxy")
    return f"{fence}: not a CONNECT request"


def upstream_refusal(host: str, port: int, status: str) -> str:
    """The 502's reason phrase when the COMPANY proxy refused the chained CONNECT: ASCII, one line,
    what happened (its own status) and what to do (BOSSAPP-1436). Read back by
    boss_engine.services.network_refusal, which matches "(BOSS_UPSTREAM_PROXY) refused HOST:PORT"."""
    host = _SAFE_HOST.sub("", host.lower())[:200] or "this host"
    said = " ".join(status.split())[:80].encode("ascii", "replace").decode() or "no answer"
    if re.search(r"\b407\b", said):
        return (f"BOSS egress: your company proxy (BOSS_UPSTREAM_PROXY) refused {host}:{port} "
                f"({said}): it wants a login BOSS does not hold. An operator points "
                "BOSS_UPSTREAM_PROXY at a local authenticating proxy (see "
                "docs/community/ENTERPRISE_NETWORK.md) and runs boss upgrade")
    return (f"BOSS egress: your company proxy (BOSS_UPSTREAM_PROXY) refused {host}:{port} "
            f"({said}). Ask IT to allow it on that proxy; if {host} is internal, an operator "
            "adds it to BOSS_UPSTREAM_NO_PROXY in .env and runs boss upgrade")


def _refuse(client: socket.socket, status: str, phrase: str) -> None:
    body = (phrase + "\n").encode("ascii", "replace")
    client.sendall(f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain\r\n"
                   f"X-Boss-Fence: refused\r\nContent-Length: {len(body)}\r\n"
                   "Connection: close\r\n\r\n".encode("ascii", "replace") + body)


def _peer(client: socket.socket) -> str:
    try:
        return str(client.getpeername()[0])
    except (OSError, IndexError):
        return "?"


def _dial_upstream(route: Upstream) -> tuple[Optional[socket.socket], bytes, str]:
    """Open the chained tunnel. (socket, bytes already read past the proxy's answer, "") or
    (None, b"", the proxy's status line)."""
    sock = socket.create_connection((route.proxy_host, route.proxy_port), timeout=30)
    if route.tls:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=route.proxy_host)
    target = f"{route.host}:{route.port}"
    sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
    answer = b""
    while b"\r\n\r\n" not in answer and len(answer) < MAX_HEADER:
        chunk = sock.recv(4096)
        if not chunk:
            break
        answer += chunk
    head, _, rest = answer.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status.split(" ", 2)
    if len(parts) < 2 or parts[1] != "200":
        sock.close()
        return None, b"", status or "no answer"
    return sock, rest, ""


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
        route, reason = decide(head)
        target = parse_connect(head) or ("", 0)
        if route is None:
            log.warning("connect refused: %s host=%s port=%s peer=%s",
                        reason, target[0] or "-", target[1] or "-", _peer(client))
            _refuse(client, "403", refusal_reason(target[0], target[1], reason))
            return
        rest = head.split(b"\r\n\r\n", 1)[1]
        if isinstance(route, Upstream):
            upstream, early, status = _dial_upstream(route)
            if upstream is None:
                log.warning("connect failed: upstream proxy said %r host=%s port=%s peer=%s",
                            status[:80], route.host, route.port, _peer(client))
                _refuse(client, "502", upstream_refusal(route.host, route.port, status))
                return
        else:
            upstream = socket.socket(route[0], route[1], route[2])
            upstream.settimeout(30)
            upstream.connect(route[4])
            early = b""
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if early:
            client.sendall(early)
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


def explain(entries: Iterable[str]) -> list[str]:
    """What the fence would do with each host[:port] (boss doctor's enterprise check)."""
    out = []
    for entry in entries:
        host, _, port = entry.partition(":")
        port_n = int(port) if port.isdigit() else 443
        route, reason = decide(f"CONNECT {host}:{port_n} HTTP/1.1\r\n\r\n".encode("ascii", "replace"))
        if route is None:
            out.append(f"bad {refusal_reason(host, port_n, reason)}")
        else:
            via = " via the upstream proxy" if isinstance(route, Upstream) else ""
            out.append(f"ok {host}:{port_n} is allowed{via}")
    return out


def serve_forever(host: str, port: int) -> None:  # pragma: no cover — exercised by the A51 container test
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":  # pragma: no cover — `boss doctor` runs it inside the egress container
    import sys

    if sys.argv[1:2] == ["explain"]:
        targets = sys.argv[2:] or [f"{h}:{p}" for h, p in _enterprise().registry_endpoints(os.environ)]
        for line in explain(targets):
            print(line)
