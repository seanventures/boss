"""WHERE A SIGN-IN CAME FROM, IN WORDS A PERSON CAN USE.

A notice that says "New sign-in from 172.18.0.8" names the console container, not the person:
behind a proxy or inside the compose network every request arrives from a private address. A
private (RFC 1918, where container networks live), loopback, link-local or unique-local address
tells the reader nothing about who signed in, so it is never shown: the notice says "a new device"
instead.

A TAILNET address is different (rc6): Docker never hands out carrier-grade NAT (100.64/10) or
Tailscale's IPv6 prefix (fd7a:115c:a1e0::/48), so on a tailnet install those name a real device
of the person's, and the Sessions and key last-used views show them like a public address.
The console has the same rule (apps/web/lib/identity/address.ts).
"""
from __future__ import annotations

import ipaddress
from typing import Optional

_PRIVATE = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918
    "169.254.0.0/16",                                   # link-local
    "fc00::/7", "fe80::/10",                            # unique-local, link-local
)]

#: A person's own device on a tailnet: shown, never hidden as "private".
_TAILNET = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48")]


def public_address(value: Optional[str]) -> Optional[str]:
    """The address as text when it names the person's device (public, or on their tailnet), else
    None (container/private, loopback, link-local, internal or unparsable)."""
    try:
        ip = ipaddress.ip_address(str(value).strip().split("%", 1)[0]) if value else None
    except ValueError:
        return None
    if ip is None:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        return None
    if any(ip.version == n.version and ip in n for n in _TAILNET):
        return str(ip)
    if any(ip.version == n.version and ip in n for n in _PRIVATE):
        return None
    return str(ip)


def sign_in_origin(value: Optional[str]) -> str:
    """'from 203.0.113.9' for a public address, 'from a new device' for any other."""
    where = public_address(value)
    return f"from {where}" if where else "from a new device"
