#!/usr/bin/env python3
"""Normalize --console-origin for BOSS_CONSOLE_PUBLIC_ORIGIN.

Prints the origin on stdout. A rejected value is one line on stderr and exit 2.
The web listener stays on loopback either way.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def normalize(raw: str) -> str:
    text = raw.strip()
    parts = urlsplit(text)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ValueError(
            "use an https origin with no path, query, or userinfo"
        )
    host = parts.hostname
    if host in LOOPBACK or ":" in host:
        raise ValueError(
            "name a DNS hostname you control; the listener stays on loopback"
        )
    if parts.port in (None, 443):
        return f"https://{host}"
    return f"https://{host}:{parts.port}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        print("pass one https origin", file=sys.stderr)
        return 2
    try:
        print(normalize(argv[1]))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
