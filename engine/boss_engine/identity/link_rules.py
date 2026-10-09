"""The link gateway's rule table (F5, APP_TEMPLATES_AND_SECURITY_LOOP.md §5.4 and §5.7).

A rule module of its own, next to egress_rules (GitHub's table, untouched). Pure: every fact is
looked up by the caller (boss_engine.app_links.service) and handed in, so the whole decision is
one table that a unit test can walk row by row.

The link gateway (apps/egress/boss_egress/links.py) listens on ONE port per active link (D8). A
request on port P is forwarded only when ALL of these hold, checked in this order:

  1. the request target is origin-form (`/path`): never an absolute URI, never CONNECT, so the
     request can name no host but the provider's;
  2. it carries a LINK PASS (`X-Boss-Link`) that BOSS minted for one consumer workload of the
     consumer app (a build run, or a preview), and the pass is not expired or revoked;
  3. port P belongs to an active link, and it is THE link the pass was minted for (a live pass
     for link L1 opens nothing on L2's port);
  4. the link is active (read per request, cached at most CACHE_SECONDS, dropped on NOTIFY), is
     a preview link, and the pass and the link are in the same project (D7);
  5. the consumer workload is live: the build run is still running and its run token's egress
     credential is not revoked; the preview is running and not expired;
  6. the path is canonical (egress_rules.canonical_path: no `..`, `//` or encoded separators)
     and nothing asks an intermediary to change the method;
  7. the upstream is the PROVIDER's running preview, and only that: the configured preview host,
     a port inside the preview port range, never a BOSS service port or a link port.

A refusal names a reason and never a secret. The pass header is never forwarded: the provider
sees only what the consumer's own code sent it (its own Authorization, cookies, body).
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .egress_rules import PathRefused, canonical_path

PASS_HEADER = "x-boss-link"
PASS_PREFIX = "boss_lnk_"
#: How long the gateway may act on a link's status without reading it again (S5.4: revocation
#: within 5 s). A revocation also NOTIFYs, which drops the cache at once.
CACHE_SECONDS = 2.0
#: Ports that are BOSS itself on any container: the API, egress (git/API, CONNECT), the console,
#: the MCP tools, Postgres, Infisical. Never an upstream, whatever a preview row says.
BOSS_SERVICE_PORTS = frozenset({8000, 8090, 8091, 3000, 8765, 8770, 8771, 5432, 6379, 8080})
#: The request headers that are dropped on the way to the provider: the pass, hop-by-hop headers,
#: and anything that would tell the provider a different client, host or protocol.
_DROP_REQUEST = frozenset({
    PASS_HEADER, "host", "connection", "keep-alive", "proxy-authorization", "proxy-authenticate",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade", "forwarded",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "x-real-ip",
    "content-length", "expect",
})
_DROP_RESPONSE = frozenset({"connection", "keep-alive", "proxy-authenticate", "te", "trailer",
                            "transfer-encoding", "upgrade", "content-length", "content-encoding"})
#: Ask an intermediary to treat the request as another method: refused, not stripped.
_OVERRIDE_HEADERS = frozenset({"x-http-method-override", "x-http-method", "x-method-override"})


@dataclass(frozen=True)
class LinkDecision:
    allowed: bool
    status: int
    reason: str = ""
    path: str = ""            # the canonical, encoded path to send upstream (allowed only)


@dataclass(frozen=True)
class PortRange:
    low: int
    high: int

    def __contains__(self, port: object) -> bool:
        return isinstance(port, int) and self.low <= port <= self.high

    @classmethod
    def parse(cls, raw: Optional[str], default: str) -> "PortRange":
        """`18100-18199` (or `18100:18199`). A malformed value falls back to the default."""
        for value in (raw, default):
            text = (value or "").strip().replace(":", "-")
            low, sep, high = text.partition("-")
            if sep and low.isdigit() and high.isdigit() and 1024 <= int(low) <= int(high) <= 65535:
                return cls(int(low), int(high))
        raise ValueError(f"bad port range {default!r}")

    def iptables(self) -> str:
        return f"{self.low}:{self.high}"


@dataclass(frozen=True)
class UpstreamPolicy:
    """Where a link may forward to: the provider's preview, on the preview host, in the preview
    port range. `link_ports` are refused as upstreams so a link can never chain into another."""
    preview_host: str
    preview_ports: PortRange
    link_ports: PortRange


def upstream_refusal(host: str, port: int, policy: UpstreamPolicy,
                     resolved: tuple[str, ...] = ()) -> Optional[str]:
    """Why (host, port) is not a permitted upstream, or None. `resolved` are the addresses the
    host name resolves to, when the caller looked them up: a link-local (metadata) or
    unspecified address is refused even on the configured host."""
    if host != policy.preview_host:
        return "upstream_not_preview_host"
    if port in BOSS_SERVICE_PORTS:
        return "upstream_is_boss"
    if port in policy.link_ports:
        return "upstream_is_a_link"
    if port not in policy.preview_ports:
        return "upstream_outside_preview_ports"
    for addr in resolved:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return "upstream_unresolvable"
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return "upstream_metadata_or_unroutable"
    return None


def decide(*, method: str, target: str, headers: Mapping[str, str], port: int,
           port_link: Optional[Mapping[str, Any]], link_pass: Optional[Mapping[str, Any]],
           consumer_live: bool, upstream: Optional[tuple[str, int]], policy: UpstreamPolicy,
           resolved: tuple[str, ...] = ()) -> LinkDecision:
    """The whole table. `headers` have lowercase keys. `link_pass` is the live pass row the
    presented pass resolved to (None when none was presented, or it is unknown, expired or
    revoked: the caller does not distinguish, and neither does the answer)."""
    if method.upper() == "CONNECT" or not target.startswith("/"):
        return LinkDecision(False, 400, "absolute_form_refused")
    if not (headers.get(PASS_HEADER) or "").strip():
        return LinkDecision(False, 401, "no_link_pass")
    if link_pass is None:
        return LinkDecision(False, 401, "link_pass_dead")
    if port_link is None:
        return LinkDecision(False, 403, "no_link_on_port")
    if str(link_pass.get("link_id")) != str(port_link.get("id")):
        return LinkDecision(False, 403, "wrong_link")
    if port_link.get("status") != "active":
        return LinkDecision(False, 403, "link_revoked")
    if port_link.get("env") != "preview":
        return LinkDecision(False, 403, "not_a_preview_link")
    if str(link_pass.get("project_id")) != str(port_link.get("project_id")):
        return LinkDecision(False, 403, "cross_project")
    if int(port_link.get("gateway_port") or 0) != port:
        return LinkDecision(False, 403, "wrong_port")
    if not consumer_live:
        return LinkDecision(False, 401, "consumer_not_live")
    path_raw, _, _query = target.partition("?")
    try:
        _decoded, encoded = canonical_path(path_raw)
    except PathRefused as exc:
        return LinkDecision(False, 400, str(exc))
    if any(h in _OVERRIDE_HEADERS for h in headers):
        return LinkDecision(False, 403, "method_override")
    if upstream is None:
        return LinkDecision(False, 502, "provider_not_running")
    why = upstream_refusal(upstream[0], upstream[1], policy, resolved)
    if why:
        return LinkDecision(False, 403, why)
    return LinkDecision(True, 200, "", encoded)


def forward_headers(headers: Mapping[str, str], upstream: tuple[str, int]) -> dict[str, str]:
    """What reaches the provider: the consumer's own headers minus the pass and hop-by-hop
    headers, with Host set to the provider's own origin (a clean origin, D8)."""
    out = {k: v for k, v in headers.items() if k.lower() not in _DROP_REQUEST}
    connection = headers.get("connection") or ""
    for name in (n.strip().lower() for n in connection.split(",")):
        out.pop(name, None)
    out["host"] = f"{upstream[0]}:{upstream[1]}"
    return out


def response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The provider's response headers minus hop-by-hop ones (repeated Set-Cookie kept)."""
    return [(k, v) for k, v in headers if k.lower() not in _DROP_RESPONSE]
