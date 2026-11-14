"""THE one HTTP path every ticket-connector call takes.

It (1) refuses any URL whose host is not allowed for this connection — the connector's official
hosts from its manifest, the base URL configured on this connection, or an operator base URL for
the provider (env_refs.OPERATOR_BASE_URLS / BOSS_CREDENTIAL_HOSTS); (2) resolves the credential
server-side and attaches it (the caller never holds it); (3) sets a timeout; (4) retries 429 and
5xx with backoff (honouring Retry-After, capped); (5) maps errors to typed exceptions; and (6)
paginates. The process-wide outbound guard (env_refs.check_outbound) still runs underneath.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from .contract import (
    AuthFailed,
    HostNotAllowed,
    NotFound,
    RateLimited,
    TicketConnection,
    TicketSystemError,
)

DEFAULT_TIMEOUT_S = 20.0
MAX_RETRIES = 3
BACKOFF_BASE_S = 0.5
MAX_RETRY_AFTER_S = 30.0
MAX_PAGES = 50

#: Seams for tests: the sleep between retries, and the transport that actually sends.
SLEEP: Callable[[float], None] = time.sleep


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        text = self.body.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        return json.loads(text)


def _urllib_transport(method: str, url: str, headers: dict[str, str], data: Optional[bytes],
                      timeout: float, context: Optional[ssl.SSLContext]) -> Response:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if url.lower().startswith("https://"):
            kwargs["context"] = context or ssl.create_default_context()
        with urllib.request.urlopen(request, **kwargs) as response:  # noqa: S310 — host checked
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()},
                            response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, {k.lower(): v for k, v in (error.headers or {}).items()},
                        error.read() or b"")


TRANSPORT: Callable[..., Response] = _urllib_transport

#: Never sent to, whatever a configuration says: cloud metadata endpoints.
_BLOCKED_HOSTS = ("metadata.google.internal", "metadata", "169.254.169.254", "fd00:ec2::254")


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def _host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url or "").hostname or "").lower().rstrip(".")


def _link_local(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False


def allowed_hosts(conn: TicketConnection, default_hosts: tuple[str, ...] = (),
                  base_url_fields: tuple[str, ...] = (),
                  guard_provider: str = "") -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(official hosts — https only, hosts configured for this connection or by the operator)."""
    from ...services import env_refs

    configured = []
    for name in base_url_fields:
        host = _host_of(str(conn.configuration.get(name) or ""))
        if host:
            configured.append(host)
    configured.extend(env_refs._operator_hosts(guard_provider or conn.provider))  # noqa: SLF001
    return tuple(default_hosts), tuple(dict.fromkeys(configured))


def check_host(url: str, official: tuple[str, ...], configured: tuple[str, ...],
               provider: str = "") -> None:
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower().rstrip(".")
    scheme = (parts.scheme or "").lower()
    if not host or scheme not in ("http", "https"):
        raise HostNotAllowed(f"{provider}: not an http(s) URL", provider=provider)
    if host in _BLOCKED_HOSTS or _link_local(host):
        raise HostNotAllowed(f"{provider}: {host} is never a ticket system", provider=provider)
    if any(_host_matches(host, p) for p in configured):
        return
    if scheme == "https" and any(_host_matches(host, p) for p in official):
        return
    where = ", ".join(official + configured) or "no host"
    raise HostNotAllowed(
        f"{provider}: refusing to send a request to {host}; this connection may reach {where}. "
        f"A self-hosted system's URL is set as the connection's base URL (or by the operator).",
        provider=provider)


class TicketHttp:
    """A request session for ONE connection. ``auth`` turns the resolved token into headers."""

    def __init__(self, conn: TicketConnection, *, base_url: str,
                 auth: Callable[[Any], tuple[dict[str, str], Optional[str]]],
                 pick_credential: Optional[Callable[[], Any]] = None,
                 default_hosts: tuple[str, ...] = (), base_url_fields: tuple[str, ...] = (),
                 guard_provider: str = "", timeout: float = DEFAULT_TIMEOUT_S,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 extra_headers: Optional[dict[str, str]] = None):
        """``pick_credential()`` -> ActingCredential (auth.select_credential); ``auth(credential)``
        -> (headers, base URL override or None) — an OAuth token may address another API base
        (Atlassian: https://api.atlassian.com/ex/jira/{cloudId})."""
        self.conn = conn
        self.provider = conn.provider
        self.base_url = (base_url or "").rstrip("/")
        self._auth = auth
        self._pick = pick_credential
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.extra_headers = dict(extra_headers or {})
        self.official, self.configured = allowed_hosts(conn, default_hosts, base_url_fields,
                                                       guard_provider)
        self._headers_cache: Optional[dict[str, str]] = None
        self.credential: Any = None

    def url(self, path_or_url: str, params: Optional[dict[str, Any]] = None) -> str:
        url = path_or_url if "://" in path_or_url else f"{self.base_url}/{path_or_url.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None},
                                           doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        return url

    def _auth_headers(self) -> dict[str, str]:
        if self._headers_cache is None:
            if self._pick is not None:
                credential = self._pick()
            else:
                from .contract import ActingCredential

                token = self.conn.credential()
                if not token:
                    raise AuthFailed(f"{self.provider}: no credential is configured for this "
                                     "connection", reason="no_credential", provider=self.provider)
                credential = ActingCredential("api_token", "org", self.conn.connection_id, token)
            headers, base = self._auth(credential)
            if base:
                self.base_url = base.rstrip("/")
            self.credential = credential
            self._headers_cache = headers
        return self._headers_cache

    def request(self, method: str, path_or_url: str, *, params: Optional[dict[str, Any]] = None,
                json_body: Any = None, headers: Optional[dict[str, str]] = None,
                ok_statuses: tuple[int, ...] = ()) -> Response:
        auth_headers = self._auth_headers()
        url = self.url(path_or_url, params)
        if not self.base_url and "://" not in path_or_url:
            raise TicketSystemError(f"{self.provider}: no base URL is configured",
                                    provider=self.provider)
        check_host(url, self.official, self.configured, self.provider)
        sent_headers = {"Accept": "application/json", "User-Agent": "BOSS-ticket-connector/2",
                        **self.extra_headers, **(headers or {}), **auth_headers}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            sent_headers.setdefault("Content-Type", "application/json")
        safe_path = urllib.parse.urlsplit(url).path
        attempt = 0
        while True:
            try:
                response = TRANSPORT(method, url, sent_headers, data, self.timeout,
                                     self.ssl_context)
            except TicketSystemError:
                raise
            except Exception as error:  # noqa: BLE001 — network: retried, then typed
                if attempt < MAX_RETRIES:
                    SLEEP(BACKOFF_BASE_S * (2 ** attempt))
                    attempt += 1
                    continue
                raise TicketSystemError(
                    f"{self.provider}: {method} {safe_path} failed: {type(error).__name__}",
                    provider=self.provider) from None
            status = response.status
            self._record(method, safe_path, status)
            if status < 400 or status in ok_statuses:
                return response
            retryable = status == 429 or status >= 500
            if retryable and attempt < MAX_RETRIES:
                SLEEP(self._delay(response, attempt))
                attempt += 1
                continue
            raise self._error(method, safe_path, response)

    def _record(self, method: str, path: str, status: Optional[int]) -> None:
        if self.credential is not None:
            from .auth import record_call

            record_call(self.conn, self.credential, method, path, status)

    def _delay(self, response: Response, attempt: int) -> float:
        advice = _retry_after(response)
        if advice is not None:
            return min(advice, MAX_RETRY_AFTER_S)
        return min(BACKOFF_BASE_S * (2 ** attempt), MAX_RETRY_AFTER_S)

    def _error(self, method: str, path: str, response: Response) -> TicketSystemError:
        detail = response.body.decode("utf-8", errors="replace")[:300].strip()
        message = f"{self.provider}: {method} {path} failed HTTP {response.status}"
        if detail:
            message += f": {detail}"
        kw = {"status": response.status, "provider": self.provider}
        if response.status in (401, 403):
            return AuthFailed(message, **kw)
        if response.status == 404:
            return NotFound(message, **kw)
        if response.status == 429:
            return RateLimited(message, retry_after=_retry_after(response), **kw)
        return TicketSystemError(message, **kw)

    def get_json(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw).json()

    def post_json(self, path: str, body: Any, **kw: Any) -> Any:
        return self.request("POST", path, json_body=body, **kw).json()

    def paginate(self, fetch: Callable[[Optional[Any]], tuple[list[Any], Optional[Any]]], *,
                 limit: Optional[int] = None) -> Iterator[Any]:
        """Yield items across pages. ``fetch(cursor) -> (items, next_cursor or None)``; the
        provider's own cursor style (offset, token, Plane cursor) lives in ``fetch``."""
        cursor: Optional[Any] = None
        seen = 0
        for _ in range(MAX_PAGES):
            items, cursor = fetch(cursor)
            for item in items:
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            if cursor in (None, "") or not items:
                return


def _retry_after(response: Response) -> Optional[float]:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
