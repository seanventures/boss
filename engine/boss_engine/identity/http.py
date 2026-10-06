"""The SSRF-safe outbound client for every identity-driven fetch (IDENTITY.md §4.13). Owned by S1;
used by S1 (OIDC discovery, JWKS, token, SAML metadata, social presets) and S3 (GitHub).
FROZEN IN PHASE 0: the signature and the rules.

Resolve DNS once and connect to the pinned IP; refuse loopback, RFC 1918, link-local, CGNAT,
169.254.169.254, ULA and ::1 unless the host is in BOSS_OUTBOUND_ALLOW_PRIVATE; https only (except
allowed private hosts); no cross-host redirects; 5 s connect, 10 s total, 1 MiB body cap.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urljoin, urlsplit


class OutboundRefused(Exception):
    """The destination is not allowed (private range, scheme, redirect, size). Never retried."""


@dataclass(frozen=True)
class SafeResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)

    def json(self) -> Any:
        import json

        return json.loads(self.body)


CONNECT_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 1024 * 1024
MAX_REDIRECTS = 3


def _allowed_private_hosts() -> frozenset[str]:
    from ..config import get_settings

    raw = get_settings().outbound_allow_private or ""
    return frozenset(h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip())


def _resolve(host: str, port: int) -> list[str]:
    """DNS, once per fetch. Tests replace this."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 6 and ip.sixtofour is not None:
        ip = ip.sixtofour
    # is_global excludes loopback, RFC 1918, link-local (incl. 169.254.169.254), CGNAT
    # 100.64/10, ULA fc00::/7, ::1, unspecified and the documentation ranges.
    return bool(ip.is_global) and not ip.is_multicast


def _open_connection(scheme: str, host: str, ip: str, port: int, timeout: float) -> socket.socket:
    """Connect to the PINNED address; TLS verifies the certificate against the host NAME. Tests
    replace this."""
    raw = socket.create_connection((ip, port), timeout=timeout)
    if scheme != "https":
        return raw
    ctx = ssl.create_default_context()
    try:
        return ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise


def _http_exchange(sock: socket.socket, method: str, host_header: str, target: str,
                   headers: Mapping[str, str], data: Optional[bytes], deadline: float
                   ) -> tuple[int, dict[str, str], bytes]:
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host_header}", "Connection: close",
             "Accept-Encoding: identity", "User-Agent: boss-identity/1"]
    sent = {k.lower() for k in headers}
    for k, v in headers.items():
        if "\r" in k or "\n" in k or "\r" in str(v) or "\n" in str(v):
            raise OutboundRefused("header injection refused")
        if k.lower() in ("host", "connection", "content-length", "transfer-encoding"):
            continue
        lines.append(f"{k}: {v}")
    if data is not None:
        lines.append(f"Content-Length: {len(data)}")
    elif method.upper() in ("POST", "PUT", "PATCH") and "content-length" not in sent:
        lines.append("Content-Length: 0")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (data or b""))
    buf = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutboundRefused("total timeout exceeded")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES + 65536:
            raise OutboundRefused("response larger than 1 MiB")
    head, sep, body = bytes(buf).partition(b"\r\n\r\n")
    if not sep:
        raise OutboundRefused("malformed response")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise OutboundRefused("malformed status line")
    status = int(parts[1])
    out_headers: dict[str, str] = {}
    for line in header_lines:
        k, _, v = line.partition(":")
        out_headers[k.strip().lower()] = v.strip()
    if out_headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)
    if len(body) > MAX_BODY_BYTES:
        raise OutboundRefused("response larger than 1 MiB")
    return status, out_headers, body


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    rest = body
    while rest:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        out.extend(rest[:size])
        rest = rest[size + 2:]
        if len(out) > MAX_BODY_BYTES:
            raise OutboundRefused("response larger than 1 MiB")
    return bytes(out)


def fetch(method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
          data: Optional[bytes] = None, purpose: str) -> SafeResponse:
    """`purpose` names the caller for the audit of private-host exceptions (e.g. "oidc.jwks")."""
    if not purpose:
        raise ValueError("purpose is required")
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    current = url
    pinned: dict[str, str] = {}
    first_host: Optional[str] = None
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or scheme not in ("http", "https"):
            raise OutboundRefused("only absolute http(s) URLs are fetched")
        if parts.username or parts.password:
            raise OutboundRefused("credentials in URLs are refused")
        if first_host is None:
            first_host = host
        elif host != first_host:
            raise OutboundRefused("redirect to another host refused")
        allowed_private = host in _allowed_private_hosts()
        if scheme != "https" and not allowed_private:
            raise OutboundRefused("https is required")
        port = parts.port or (443 if scheme == "https" else 80)
        if host not in pinned:
            try:
                literal = ipaddress.ip_address(host)
                addrs = [str(literal)]
            except ValueError:
                try:
                    addrs = _resolve(host, port)
                except OSError as exc:
                    raise OutboundRefused(f"cannot resolve host ({type(exc).__name__})") from exc
            if not addrs:
                raise OutboundRefused("host has no address")
            private = [a for a in addrs if not _is_public(a)]
            if private and not allowed_private:
                # ANY private answer refuses the host: a rebinding resolver that mixes a public
                # and a private address gets nothing.
                raise OutboundRefused("destination is a private, loopback or link-local address")
            if allowed_private:
                print(f"[identity] outbound to allow-listed private host {host} ({purpose})",
                      flush=True)
            pinned[host] = addrs[0]
        ip = pinned[host]
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host_header = host if parts.port is None else f"{host}:{parts.port}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutboundRefused("total timeout exceeded")
        try:
            sock = _open_connection(scheme, host, ip, port, min(CONNECT_TIMEOUT_SECONDS, remaining))
        except OutboundRefused:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise OutboundRefused(f"connection failed ({type(exc).__name__})") from exc
        try:
            status, resp_headers, body = _http_exchange(sock, method.upper(), host_header, target,
                                                        dict(headers or {}), data, deadline)
        except OutboundRefused:
            raise
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise OutboundRefused(f"exchange failed ({type(exc).__name__})") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if status in (301, 302, 303, 307, 308) and resp_headers.get("location"):
            current = urljoin(current, resp_headers["location"])
            if status == 303:
                method, data = "GET", None
            continue
        return SafeResponse(status=status, headers=resp_headers, body=body)
    raise OutboundRefused("too many redirects")
