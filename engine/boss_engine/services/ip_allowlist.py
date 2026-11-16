"""Approved source addresses for the console and for published apps (BOSSAPP-1271).

An empty list allows every address, so a fresh install is not locked out.
A configured list allows an address only when it falls in one entry (one IP,
or a CIDR network). Anything else is refused and one audit row is written in
the caller's transaction.
"""
from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy import text

from .core import make_id

SURFACES = frozenset({"console", "published_app"})


def canonical(raw: str) -> str:
    """One stored form: a single address is its host network, a CIDR stays a network."""
    entry = str(raw or "").strip()
    if not entry:
        raise ValueError("An allowlist entry is empty.")
    try:
        if "/" in entry:
            network = ipaddress.ip_network(entry, strict=False)
        else:
            address = ipaddress.ip_address(entry)
            network = ipaddress.ip_network(
                f"{address}/{address.max_prefixlen}", strict=False)
    except ValueError as exc:
        raise ValueError(f"{entry} is not an IP address or CIDR network.") from exc
    return str(network)


def replace(connection: Any, entries: list[str], *, actor: str) -> list[str]:
    """Replace the list. An empty list clears it, which lifts the restriction."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        item = canonical(raw)
        if item not in seen:
            seen.add(item)
            cleaned.append(item)
    connection.execute(text("DELETE FROM ip_allowlist"))
    for item in cleaned:
        connection.execute(
            text(
                "INSERT INTO ip_allowlist (id, cidr, created_by) "
                "VALUES (:id, :cidr, :actor)"
            ),
            {"id": make_id("ipal"), "cidr": item, "actor": actor or "operator"},
        )
    return cleaned


def listed(connection: Any) -> list[str]:
    rows = connection.execute(
        text("SELECT cidr FROM ip_allowlist ORDER BY cidr")
    ).all()
    return [str(row[0]) for row in rows]


def _on_list(client_ip: str, entries: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(entry, strict=False) for entry in entries)


def admit(connection: Any, *, surface: str, client_ip: str) -> dict:
    """Allow, or refuse with 403 after writing one audit row.

    The caller commits. Raising here would roll the audit row back.
    """
    if surface not in SURFACES:
        raise ValueError(f"Unknown surface '{surface}'.")
    seen = (client_ip or "").strip()
    entries = listed(connection)
    if not entries or _on_list(seen, entries):
        return {
            "status_code": 200,
            "allowed": True,
            "surface": surface,
            "client_ip": seen,
            "restricted": bool(entries),
        }
    connection.execute(
        text(
            "INSERT INTO ip_allow_audit (id, surface, client_ip, outcome) "
            "VALUES (:id, :surface, :ip, 'refused')"
        ),
        {"id": make_id("ipaud"), "surface": surface, "ip": seen or "unknown"},
    )
    return {
        "status_code": 403,
        "allowed": False,
        "surface": surface,
        "client_ip": seen,
        "restricted": True,
        "detail": "This address is not on the approved IP list.",
    }
