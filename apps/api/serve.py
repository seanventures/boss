"""Start the API: uvicorn, trusting forwarded headers ONLY from BOSS_TRUSTED_PROXIES.

    python -m apps.api.serve --host 0.0.0.0 --port 8000 [any other uvicorn option]

Uvicorn believes X-Forwarded-For from 127.0.0.1 on its own (its default `forwarded_allow_ips`),
and rewrites the request's client address before BOSS sees it. On a source install the console
reaches the API over loopback, so whatever a caller wrote in that header became the address the
collector's rate limits count — even with BOSS_TRUSTED_PROXIES set to trust nobody (re-check 4,
item 3). Here uvicorn is given the same list the collector reads: its IP and CIDR entries. A
NAME in the list (compose's `web`) is resolved per request by the collector itself
(apps/api/app/routers/improve.py client_ip), never handed to uvicorn, which would only compare
it as a literal.
"""
from __future__ import annotations

import ipaddress
import sys


def forwarded_allow_ips(trusted: str) -> str:
    """The IP and CIDR entries of a BOSS_TRUSTED_PROXIES value, as uvicorn's option. Empty →
    uvicorn trusts no proxy at all."""
    keep: list[str] = []
    for entry in (trusted or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            keep.append(str(ipaddress.ip_network(entry, strict=False)))
        except ValueError:
            continue
    return ",".join(keep)


def uvicorn_argv(argv: list[str], trusted: str) -> list[str]:
    """`argv` for uvicorn's CLI, the app and the forwarded-header trust filled in. An explicit
    --forwarded-allow-ips on the command line is replaced: the one list is BOSS_TRUSTED_PROXIES."""
    out: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "--forwarded-allow-ips":
            skip = True
            continue
        if arg.startswith("--forwarded-allow-ips="):
            continue
        out.append(arg)
    if "apps.api.app.main:app" not in out:
        out.insert(0, "apps.api.app.main:app")
    return [*out, "--forwarded-allow-ips", forwarded_allow_ips(trusted)]


def _option(argv: list[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def preview_listen_env(argv: list[str]) -> dict[str, str]:
    """Ask the API process to serve the preview origin too (apps/api/app/preview_origin.py), on
    the API's own host and BOSS_PREVIEW_PORT. Not with several workers: each would race for the
    one port, and one listener in one process is the whole design."""
    try:
        workers = int(_option(argv, "--workers") or "1")
    except ValueError:
        workers = 1
    if workers > 1:
        return {}
    return {"BOSS_PREVIEW_LISTEN": "1", "BOSS_PREVIEW_HOST": _option(argv, "--host") or "127.0.0.1"}


def main(argv: list[str] | None = None) -> None:
    import os

    from boss_engine.config import get_settings
    import uvicorn.main

    given = list(sys.argv[1:] if argv is None else argv)
    for key, value in preview_listen_env(given).items():
        os.environ.setdefault(key, value)
    args = uvicorn_argv(given, str(getattr(get_settings(), "trusted_proxies", "") or ""))
    uvicorn.main.main(args=args, prog_name="uvicorn")


if __name__ == "__main__":
    main()
