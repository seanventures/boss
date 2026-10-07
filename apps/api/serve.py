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


#: The ASGI scope key holding the SOCKET peer, recorded before the forwarded headers are applied
#: (F2: identity's trusted-proxy decisions read the peer that actually connected; uvicorn's own
#: proxy handling overwrote scope["client"] and kept nothing of it).
RAW_PEER_KEY = "boss.raw_peer"
APP_TARGET = "apps.api.serve:asgi"


class RecordRawPeer:
    """Outermost ASGI layer: keep the connecting peer, then hand on to the proxy-header layer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket") and scope.get("client"):
            scope[RAW_PEER_KEY] = scope["client"][0]
        await self.app(scope, receive, send)


def build_asgi(app, trusted: str):
    """The served app: the raw peer recorded, then X-Forwarded-For believed ONLY from
    BOSS_TRUSTED_PROXIES (uvicorn's own middleware, applied here instead of by its CLI)."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    return RecordRawPeer(ProxyHeadersMiddleware(app, trusted_hosts=forwarded_allow_ips(trusted)))


def __getattr__(name: str):
    """`asgi` is built on first use (uvicorn imports APP_TARGET), so importing this module for its
    helpers never imports the whole API."""
    if name == "asgi":
        from boss_engine.config import get_settings
        from apps.api.app.main import app

        built = build_asgi(app, str(getattr(get_settings(), "trusted_proxies", "") or ""))
        globals()["asgi"] = built
        return built
    raise AttributeError(name)


def uvicorn_argv(argv: list[str], trusted: str) -> list[str]:
    """`argv` for uvicorn's CLI with the app filled in. Forwarded headers are applied by the app
    itself (build_asgi, from BOSS_TRUSTED_PROXIES), after the raw peer is recorded, so uvicorn's
    own proxy handling is off and any --forwarded-allow-ips on the command line is dropped."""
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
        if arg in ("--proxy-headers", "--no-proxy-headers"):
            continue
        out.append(arg)
    out = [a for a in out if a != "apps.api.app.main:app"]
    if APP_TARGET not in out:
        out.insert(0, APP_TARGET)
    return [*out, "--no-proxy-headers"]


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
