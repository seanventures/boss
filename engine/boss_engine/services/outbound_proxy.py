"""WHICH PROXY ONE OUTBOUND REQUEST TAKES, read from the standard environment.

BOSS's containers get the company proxy as ``HTTPS_PROXY`` / ``HTTP_PROXY`` and the hosts reached
without it as ``NO_PROXY`` (docs/community/ENTERPRISE_NETWORK.md: "host names, .domain suffixes or
CIDR ranges"). Both HTTP stacks BOSS's own processes use match ``NO_PROXY`` by NAME only:

* httpx turns ``10.0.0.0/8`` into a pattern for the literal host ``10.0.0.0``, so an internal MCP
  server at ``10.1.2.3`` was sent to the company proxy, which refuses or cannot reach it;
* urllib does the same suffix match and nothing else.

This module is the one reading of those variables, curl's rules plus CIDR: ``*`` (everything),
a name (that host and its sub-domains), ``.example.com`` / ``*.example.com`` (sub-domains and the
domain), an IP address, a CIDR range, and any of those with ``:port``. Upper- and lower-case
variables are both read, the lower-case one first (curl's order).
"""
from __future__ import annotations

import ipaddress
import os
import ssl
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

_PROXY_VARS = {
    "https": ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"),
    "http": ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"),
}


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def _ip(value: str) -> Optional[Any]:
    try:
        return ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def _loopback(host: str) -> bool:
    host = str(host or "").strip("[]").lower().rstrip(".")
    address = _ip(host)
    return host == "localhost" or host.endswith(".localhost") or bool(
        address is not None and address.is_loopback)


def _entry_matches(entry: str, host: str, port: Optional[int]) -> bool:
    entry = entry.strip().lower()
    if not entry:
        return False
    if entry == "*":
        return True
    want_port: Optional[int] = None
    if entry.startswith("["):                       # [v6]:port
        inside, _, rest = entry[1:].partition("]")
        entry = inside
        if rest.startswith(":") and rest[1:].isdigit():
            want_port = int(rest[1:])
    elif entry.count(":") == 1:                     # host:port (one colon: not a bare v6)
        name, _, maybe_port = entry.partition(":")
        if maybe_port.isdigit():
            entry, want_port = name, int(maybe_port)
    if want_port is not None and port is not None and want_port != port:
        return False
    address = _ip(host)
    if address is not None:
        try:
            return address in ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return False
    if "/" in entry or _ip(entry) is not None:
        return False                                # an address rule never matches a name
    suffix = entry.lstrip("*").lstrip(".")
    return host == suffix or host.endswith("." + suffix)


def bypassed(host: str, port: Optional[int] = None,
             env: Optional[Mapping[str, str]] = None) -> bool:
    """True when ``NO_PROXY`` says `host` (and `port`) is reached directly."""
    env = os.environ if env is None else env
    host = str(host or "").strip("[]").lower().rstrip(".")
    raw = _first(env, ("no_proxy", "NO_PROXY"))
    return any(_entry_matches(item, host, port) for item in raw.split(","))


def proxy_for(url: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The proxy URL a request to `url` goes through, or None for a direct connection."""
    env = os.environ if env is None else env
    parts = urlsplit(str(url or ""))
    scheme = (parts.scheme or "").lower()
    if scheme not in _PROXY_VARS or not parts.hostname:
        return None
    proxy = _first(env, _PROXY_VARS[scheme])
    if not proxy:
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = None
    if _loopback(parts.hostname) or bypassed(parts.hostname, port, env):
        # A proxy can never reach this process's own loopback (the stdio bridge, the gateway).
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


def any_proxy_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return bool(_first(env, _PROXY_VARS["https"]) or _first(env, _PROXY_VARS["http"]))


def ssl_context() -> ssl.SSLContext:
    """The trust httpx would build from the environment: ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``
    (with-extra-ca's combined bundle, the company CA added to the public ones), else certifi."""
    cafile = os.environ.get("SSL_CERT_FILE") or ""
    capath = os.environ.get("SSL_CERT_DIR") or ""
    if cafile and os.path.isfile(cafile):
        return ssl.create_default_context(cafile=cafile)
    if capath and os.path.isdir(capath):
        return ssl.create_default_context(capath=capath)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — the system store is the last resort
        return ssl.create_default_context()


def httpx_options(url: str, env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Extra keyword arguments for an httpx call to `url`: nothing when no proxy is configured
    (httpx's own reading is then exact), else this module's decision in place of httpx's
    (``trust_env=False`` with the proxy chosen here and the same CA trust)."""
    env = os.environ if env is None else env
    if not any_proxy_configured(env):
        return {}
    return {"trust_env": False, "proxy": proxy_for(url, env), "verify": ssl_context()}
