"""THE MODEL-CREDENTIAL PROXY: AN AGENT SESSION NEVER HOLDS THE REAL PROVIDER KEY.

WHY THIS EXISTS
---------------
A coding agent needs a model credential to run, and everything in its environment is something
the commands it runs can read. The claude_agent_sdk engine hid the Anthropic API key with
CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1, which makes the Claude CLI require its own Linux sandbox
(bubblewrap); inside BOSS's containers bwrap cannot create a namespace ("No permissions to create
new namespace"), so every Bash call failed. The claude_code engine never scrubbed at all: an
agent's `env` printed the operator's key.

So the key stays in BOSS. The process that starts an agent session (the API or a worker) runs a
small reverse proxy on loopback, and the session gets, in place of the key:

  * a base URL — `http://127.0.0.1:<port>/<provider>` — and
  * a token — `boss_mdl_<64 hex>` — that is short-lived (a TTL past the longest session),
    bound to the run and project it was issued for, revoked the moment the session that asked
    for it ends, and registered with the log redactor when it is minted.

The proxy accepts that token only on its provider's model API paths (Anthropic: /v1/messages,
/v1/messages/count_tokens, /v1/models; OpenAI-compatible: chat/completions, responses,
completions, embeddings, models), strips every credential header the client sent, injects the
real one, and streams the answer back untouched. Anything else — another path, another
provider's prefix, an unknown, expired or revoked token — is refused before a byte goes
upstream. It is not a way out to the internet: the upstream is fixed per provider, never taken
from the request.

WHY HERE AND NOT IN boss-egress
-------------------------------
The sandbox network fence (scripts/sandbox/entrypoint.sh) already lets a sandbox uid reach
loopback and nothing it should not; NO_PROXY already names 127.0.0.1 (sandbox_identity.env). The
process that spawns the session is the one that resolved the credential (claude_credential, the
console vault, the install's own environment) — nothing has to be copied to another container, a
database table or a new port. boss-egress holds GitHub credentials, buffers each response whole
(model answers stream for minutes) and does not see the install's own ANTHROPIC_API_KEY; putting
the model key there would mean a second resolution path, a token table and a streaming rewrite,
for no isolation the fence does not already give. Remote worker hosts get the same proxy, in
their own worker process.

METERING
--------
llm_calls stays the one ledger: the engine records each session's usage from the CLI's own
result, exactly as before. The proxy writes no llm_calls row. It keeps a per-token tally of what
actually crossed it (requests, input/output/cache tokens, read from the provider's own `usage`)
that the engine attaches to the session's result as `_proxy_usage`, and `run_usage` sums per run
— a consistency check, never a second charge.

WHAT IS NOT COVERED
-------------------
A subscription login (CLAUDE_CODE_OAUTH_TOKEN, the Codex ChatGPT login file, a Grok session) is
not an API key and is not proxied: those CLIs authenticate their own way, and their credential
stays in the session as before. A micro-VM guest receives its credential through guest_spec
(sandbox_contract); it has no loopback shared with BOSS. docs/community/MODEL_CONNECTIONS.md.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Mapping, Optional

log = logging.getLogger(__name__)

TOKEN_PREFIX = "boss_mdl_"
#: Loopback only: the sandbox fence lets a sandbox uid reach 127.0.0.0/8 and the sandbox's
#: NO_PROXY sends it there directly, never through the egress CONNECT proxy.
HOST = "127.0.0.1"
#: The backstop when a session's end is never seen (a killed worker): past the longest session
#: BOSS starts (code_agent caps a session at an hour unless the operator raised the timeout).
MIN_TTL_SECONDS = 3600
TTL_GRACE_SECONDS = 900
MAX_BODY_BYTES = 64 * 1024 * 1024
#: How much of a non-streamed answer is kept to read its `usage` (the body itself is streamed).
_USAGE_KEEP = 4 * 1024 * 1024

#: Never forwarded, in either direction: hop-by-hop headers and every credential header a client
#: could send under any provider's name.
_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                  "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"})
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key",
                                 "cookie", "proxy-authorization"})


# ── Providers ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderAdapter:
    """One provider's model API: where it is, which paths are model calls, how the credential
    goes on the request, and which environment names a client reads."""

    name: str
    upstream: str
    #: (METHOD, full-match regex on the path after the provider prefix).
    paths: tuple[tuple[str, str], ...]
    header: str
    header_format: str
    #: The variables the real key would occupy in a client's environment (all are replaced).
    key_env: tuple[str, ...]
    #: The variables a client reads its base URL from, and the suffix each one wants.
    base_env: tuple[tuple[str, str], ...]
    #: An install-environment override of the upstream (a corporate gateway), read in BOSS.
    upstream_env: tuple[str, ...] = ()

    def allows(self, method: str, path: str) -> bool:
        return any(method.upper() == m and re.fullmatch(p, path) for m, p in self.paths)

    def credential_header(self, credential: str) -> tuple[str, str]:
        return self.header, self.header_format.format(credential)

    def resolve_upstream(self, override: Optional[str] = None) -> str:
        if override:
            return override.rstrip("/")
        for name in self.upstream_env:
            value = (os.environ.get(name) or "").strip()
            if value and not _is_ours(value):
                return value.rstrip("/")
        return self.upstream


_MODEL_ID = r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}"
_OPENAI_PATHS = (
    ("POST", r"/v1/chat/completions"), ("POST", r"/v1/responses"),
    ("POST", r"/v1/completions"), ("POST", r"/v1/embeddings"),
    ("GET", r"/v1/models"), ("GET", rf"/v1/models/{_MODEL_ID}"),
)

ANTHROPIC = ProviderAdapter(
    name="anthropic", upstream="https://api.anthropic.com",
    paths=(("POST", r"/v1/messages"), ("POST", r"/v1/messages/count_tokens"),
           ("GET", r"/v1/models"), ("GET", rf"/v1/models/{_MODEL_ID}")),
    header="x-api-key", header_format="{}",
    # The Claude CLI sends ANTHROPIC_API_KEY as x-api-key and ANTHROPIC_AUTH_TOKEN as a bearer;
    # it reads ANTHROPIC_BASE_URL. litellm-based CLIs (aider) read ANTHROPIC_API_BASE.
    key_env=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    base_env=(("ANTHROPIC_BASE_URL", ""), ("ANTHROPIC_API_BASE", "")),
    upstream_env=("ANTHROPIC_BASE_URL",),
)
OPENAI = ProviderAdapter(
    name="openai", upstream="https://api.openai.com", paths=_OPENAI_PATHS,
    header="authorization", header_format="Bearer {}",
    key_env=("OPENAI_API_KEY",),
    base_env=(("OPENAI_BASE_URL", "/v1"), ("OPENAI_API_BASE", "/v1")),
    upstream_env=("OPENAI_BASE_URL",),
)
OPENROUTER = ProviderAdapter(
    name="openrouter", upstream="https://openrouter.ai/api", paths=_OPENAI_PATHS,
    header="authorization", header_format="Bearer {}",
    key_env=("OPENROUTER_API_KEY",),
    base_env=(("OPENROUTER_API_BASE", "/v1"), ("OPENROUTER_BASE_URL", "/v1")),
)

ADAPTERS: dict[str, ProviderAdapter] = {a.name: a for a in (ANTHROPIC, OPENAI, OPENROUTER)}
#: Which adapter a provider key variable belongs to (shield_env).
KEY_ENV_PROVIDER: dict[str, str] = {
    name: a.name for a in ADAPTERS.values() for name in a.key_env}


def _is_ours(url: str) -> bool:
    return url.startswith(f"http://{HOST}:") and any(
        f"/{name}" in url for name in ADAPTERS)


# ── Grants ────────────────────────────────────────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Grant:
    """What one token may do. The token itself is never stored, only its hash; the credential is
    held in memory and excluded from repr."""

    token_hash: str
    provider: str
    upstream: str
    expires_at: float
    run_id: Optional[str] = None
    project_id: Optional[str] = None
    credential: str = field(default="", repr=False)
    revoked: bool = False
    usage: dict = field(default_factory=lambda: {
        "requests": 0, "refused": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

    def live(self, now: Optional[float] = None) -> bool:
        return not self.revoked and (now or time.time()) < self.expires_at


@dataclass(frozen=True)
class Issued:
    """What a session gets: the token, the base URL, and the environment to put them in."""

    token: str = field(repr=False)
    base_url: str = ""
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    provider: str = ""


class ProxyUnavailable(RuntimeError):
    """The proxy could not start: the session is not started with the real key instead."""


#: (method, url, headers, body) -> a context manager yielding (status, headers, chunk iterator).
Upstream = Callable[[str, str, Mapping[str, str], bytes], Any]


@contextlib.contextmanager
def _httpx_upstream(method: str, url: str, headers: Mapping[str, str], body: bytes
                    ) -> Iterator[tuple[int, dict, Iterator[bytes]]]:
    import httpx

    timeout = httpx.Timeout(connect=30.0, read=900.0, write=120.0, pool=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        request = client.build_request(method, httpx.URL(url), headers=dict(headers),
                                       content=body or None)
        resp = client.send(request, stream=True)
        try:
            yield resp.status_code, dict(resp.headers), resp.iter_raw()
        finally:
            resp.close()


class _UsageReader:
    """Reads the provider's own `usage` out of an answer as it streams past: SSE events
    (Anthropic message_start / message_delta, OpenAI's final chunk) or one JSON body."""

    def __init__(self, content_type: str) -> None:
        self.sse = "text/event-stream" in (content_type or "")
        self.buf = b""
        self.kept = 0
        self.found: dict[str, int] = {}

    def feed(self, chunk: bytes) -> None:
        if self.sse:
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                self._sse_line(line)
        elif self.kept < _USAGE_KEEP:
            self.buf += chunk[: _USAGE_KEEP - self.kept]
            self.kept += len(chunk)

    def _sse_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        try:
            event = json.loads(line[5:].strip() or b"null")
        except ValueError:
            return
        if isinstance(event, dict):
            self._take(event.get("usage"))
            message = event.get("message")
            if isinstance(message, dict):
                self._take(message.get("usage"))
            response = event.get("response")
            if isinstance(response, dict):
                self._take(response.get("usage"))

    def _take(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        names = {"input_tokens": ("input_tokens", "prompt_tokens"),
                 "output_tokens": ("output_tokens", "completion_tokens"),
                 "cache_read_input_tokens": ("cache_read_input_tokens",),
                 "cache_creation_input_tokens": ("cache_creation_input_tokens",)}
        for key, sources in names.items():
            for src in sources:
                value = usage.get(src)
                if isinstance(value, int) and value >= 0:
                    # message_delta carries the running total, so the last value wins.
                    self.found[key] = value
                    break

    def finish(self) -> dict[str, int]:
        if self.sse:
            self._sse_line(self.buf)
        else:
            try:
                body = json.loads(self.buf or b"null")
            except ValueError:
                body = None
            if isinstance(body, dict):
                self._take(body.get("usage"))
        return dict(self.found)


class ModelProxy:
    """The loopback proxy and its grants. One per process (`get`); tests build their own."""

    def __init__(self, *, upstream: Optional[Upstream] = None, host: str = HOST,
                 poll_interval: float = 0.5) -> None:
        self.upstream = upstream or _httpx_upstream
        self.host = host
        self.poll_interval = poll_interval
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None

    # ---- the server ------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        self.start()
        assert self._server is not None
        return int(self._server.server_address[1])

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            try:
                server = ThreadingHTTPServer((self.host, 0), _handler_for(self))
            except OSError as exc:
                raise ProxyUnavailable(
                    f"the model proxy could not listen on {self.host}: {exc.strerror or exc}"
                ) from None
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, args=(self.poll_interval,),
                             daemon=True, name="boss-model-proxy").start()
            self._server = server

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def base_url(self, provider: str) -> str:
        return f"http://{self.host}:{self.port}/{provider}"

    # ---- grants ----------------------------------------------------------------------------------
    def issue(self, provider: str, credential: str, *, ttl: float,
              run_id: Optional[str] = None, project_id: Optional[str] = None,
              upstream: Optional[str] = None) -> Issued:
        adapter = ADAPTERS[provider]
        if not credential:
            raise ValueError("no credential to proxy")
        token = TOKEN_PREFIX + secrets.token_hex(32)
        _register_redaction(token)
        _register_redaction(credential)
        grant = Grant(token_hash=_hash(token), provider=provider,
                      upstream=adapter.resolve_upstream(upstream),
                      expires_at=time.time() + max(1.0, float(ttl)), run_id=run_id,
                      project_id=project_id, credential=credential)
        base = self.base_url(provider)
        with self._lock:
            self._sweep()
            self._grants[grant.token_hash] = grant
        env = {name: token for name in adapter.key_env[:1]}
        env.update({name: base + suffix for name, suffix in adapter.base_env})
        _current_scope_add(self, token)
        return Issued(token=token, base_url=base, env=env, provider=provider)

    def grant_for(self, token: str) -> Optional[Grant]:
        with self._lock:
            return self._grants.get(_hash(token))

    def revoke(self, token: str) -> dict:
        """End a token now; returns what crossed the proxy on it."""
        with self._lock:
            grant = self._grants.get(_hash(token))
            if grant is None:
                return {}
            grant.revoked = True
            grant.credential = ""
            return dict(grant.usage)

    def revoke_run(self, run_id: str) -> int:
        """End every token issued for a run (the run is over, or was stopped)."""
        n = 0
        with self._lock:
            for grant in self._grants.values():
                if grant.run_id == run_id and not grant.revoked:
                    grant.revoked, grant.credential = True, ""
                    n += 1
        return n

    def run_usage(self, run_id: str) -> dict:
        total: dict[str, int] = {}
        with self._lock:
            for grant in self._grants.values():
                if grant.run_id == run_id:
                    for k, v in grant.usage.items():
                        total[k] = total.get(k, 0) + int(v)
        return total

    def _sweep(self) -> None:
        """Forget grants long past their end (their usage has been read by then)."""
        cutoff = time.time() - 6 * 3600
        for key in [k for k, g in self._grants.items()
                    if g.expires_at < cutoff or (g.revoked and g.expires_at < time.time())]:
            del self._grants[key]

    # ---- one request -----------------------------------------------------------------------------
    def authorise(self, provider: str, path: str, method: str,
                  headers: Mapping[str, str]) -> tuple[int, str, Optional[Grant]]:
        """(status, reason, grant): 200 with the grant when the request may go upstream."""
        adapter = ADAPTERS.get(provider)
        if adapter is None:
            return 404, "unknown_provider", None
        token = _presented(headers)
        if not token or not token.startswith(TOKEN_PREFIX):
            return 401, "no_run_token", None
        with self._lock:
            grant = self._grants.get(_hash(token))
            if grant is None:
                return 401, "unknown_token", None
            if grant.revoked:
                return 401, "token_revoked", None
            if not grant.live():
                return 401, "token_expired", None
            if grant.provider != provider:
                grant.usage["refused"] += 1
                return 401, "foreign_provider", None
            if not adapter.allows(method, path):
                grant.usage["refused"] += 1
                return 403, "not_a_model_path", None
        return 200, "", grant

    def forward_headers(self, grant: Grant, headers: Mapping[str, str]) -> dict[str, str]:
        out = {k: v for k, v in headers.items()
               if k.lower() not in _HOP and k.lower() not in _CREDENTIAL_HEADERS
               and k.lower() != "accept-encoding"}
        name, value = ADAPTERS[grant.provider].credential_header(grant.credential)
        out[name] = value
        # Identity: the usage is read from the bytes as they pass.
        out["accept-encoding"] = "identity"
        return out

    def tally(self, grant: Grant, usage: Mapping[str, int]) -> None:
        with self._lock:
            grant.usage["requests"] += 1
            for k, v in usage.items():
                if k in grant.usage:
                    grant.usage[k] += int(v)


def _presented(headers: Mapping[str, str]) -> str:
    lowered = {k.lower(): v for k, v in headers.items()}
    key = (lowered.get("x-api-key") or "").strip()
    if key:
        return key
    scheme, _, value = (lowered.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _refusal(provider: str, status: int, reason: str) -> bytes:
    kind = "authentication_error" if status == 401 else (
        "permission_error" if status == 403 else "not_found_error")
    return json.dumps({"type": "error", "error": {
        "type": kind, "message": f"boss model proxy: {reason}"}}).encode()


def _handler_for(proxy: ModelProxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "boss-model-proxy"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 — no access log
            return

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _body(self) -> Optional[bytes]:
            if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
                out = b""
                while True:
                    size = int((self.rfile.readline().split(b";")[0].strip() or b"0"), 16)
                    if size == 0:
                        self.rfile.readline()
                        return out
                    out += self.rfile.read(size)
                    self.rfile.readline()
                    if len(out) > MAX_BODY_BYTES:
                        return None
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                return None
            return self.rfile.read(length) if length else b""

        def _handle(self) -> None:
            raw = self.path.split("?", 1)
            path, query = raw[0], (raw[1] if len(raw) > 1 else "")
            provider, _, rest = path.lstrip("/").partition("/")
            rest = "/" + rest
            headers = {k: v for k, v in self.headers.items()}
            status, reason, grant = proxy.authorise(provider, rest, self.command, headers)
            if grant is None:
                log.info("model proxy refused %s /%s%s: %s", self.command, provider,
                         rest if status != 404 else "", reason)
                return self._send(status, _refusal(provider, status, reason))
            body = self._body()
            if body is None:
                return self._send(413, _refusal(provider, 413, "body_too_large"))
            url = grant.upstream + rest + (f"?{query}" if query else "")
            out_headers = proxy.forward_headers(grant, headers)
            try:
                with proxy.upstream(self.command, url, out_headers, body) as (code, resp_headers,
                                                                               chunks):
                    reader = _UsageReader(str({k.lower(): v for k, v in resp_headers.items()}
                                              .get("content-type", "")))
                    self.send_response(int(code))
                    for k, v in resp_headers.items():
                        if k.lower() not in _HOP:
                            self.send_header(k, v)
                    self.send_header("transfer-encoding", "chunked")
                    self.send_header("connection", "close")
                    self.end_headers()
                    for chunk in chunks:
                        if not chunk:
                            continue
                        reader.feed(chunk)
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.close_connection = True
                    proxy.tally(grant, reader.finish())
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as exc:  # noqa: BLE001 — the client gets a 502, never a credential
                log.warning("model proxy upstream failed for %s: %s", provider, type(exc).__name__)
                try:
                    self._send(502, _refusal(provider, 502, "upstream_unreachable"))
                except Exception:  # noqa: BLE001 — headers already sent
                    self.close_connection = True

        do_GET = do_POST = _handle

        def do_PUT(self) -> None:  # noqa: N802
            self._send(405, _refusal("", 405, "method_not_allowed"))

        do_DELETE = do_PATCH = do_PUT

    return Handler


def _register_redaction(value: str) -> None:
    try:
        from ..identity.redaction import register_secrets

        register_secrets([value])
    except Exception:  # noqa: BLE001 — redaction must never stop a run
        pass


# ── The process's proxy, sessions, and the environment helpers ───────────────────────────────

_PROXY: Optional[ModelProxy] = None
_PROXY_LOCK = threading.Lock()


def get() -> ModelProxy:
    global _PROXY
    with _PROXY_LOCK:
        if _PROXY is None:
            _PROXY = ModelProxy()
        return _PROXY


def set_proxy(proxy: Optional[ModelProxy]) -> None:
    """Tests: replace the process's proxy (None: a fresh one next time)."""
    global _PROXY
    with _PROXY_LOCK:
        _PROXY = proxy


_SCOPE: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "boss_model_proxy_scope", default=None)


def _current_scope_add(proxy: ModelProxy, token: str) -> None:
    scope = _SCOPE.get()
    if scope is not None:
        scope.append((proxy, token))


@contextlib.contextmanager
def session_scope() -> Iterator[list]:
    """Every token issued inside is revoked when the block ends, however it ends. Yields the
    list the usage of those tokens is summed into on exit (one dict, appended)."""
    issued: list = []
    reset = _SCOPE.set(issued)
    out: list = []
    try:
        yield out
    finally:
        _SCOPE.reset(reset)
        total: dict[str, int] = {}
        for proxy, token in issued:
            for k, v in proxy.revoke(token).items():
                total[k] = total.get(k, 0) + int(v)
        if issued:
            out.append(total)


def scoped(attach_usage: bool = False):
    """Decorate a method that starts agent sessions: tokens it issued die with it. With
    `attach_usage`, a dict result carries what crossed the proxy as `_proxy_usage`."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with session_scope() as usage:
                result = fn(*args, **kwargs)
            if attach_usage and usage and isinstance(result, dict):
                result["_proxy_usage"] = usage[0]
            return result
        return inner
    return wrap


def session_ttl(settings: Any) -> int:
    """The token's life: past the longest session this install can start."""
    try:
        configured = int(getattr(settings, "code_agent_timeout_seconds", 0) or 0)
    except (TypeError, ValueError):
        configured = 0
    return max(configured, MIN_TTL_SECONDS) + TTL_GRACE_SECONDS


def _run_binding() -> tuple[Optional[str], Optional[str]]:
    try:
        from .llm_telemetry import get_llm_context

        ctx = get_llm_context()
        return ctx.get("run_id"), ctx.get("project_id")
    except Exception:  # noqa: BLE001
        return None, None


def shield(env: dict, provider: str, credential: str, *, settings: Any = None,
           proxy: Optional[ModelProxy] = None) -> dict:
    """`env` with `provider`'s real key replaced by a run token and the proxy's base URL.
    Raises ProxyUnavailable rather than fall back to the key."""
    adapter = ADAPTERS[provider]
    proxy = proxy or get()
    run_id, project_id = _run_binding()
    issued = proxy.issue(provider, credential, ttl=session_ttl(settings), run_id=run_id,
                         project_id=project_id)
    for name in adapter.key_env:
        env.pop(name, None)
    env.update(issued.env)
    return env


def shield_env(env: dict, *, settings: Any = None, proxy: Optional[ModelProxy] = None) -> dict:
    """Every provider key `env` holds (KEY_ENV_PROVIDER) replaced by a run token for its own
    provider. A value that already is a run token is left alone."""
    for name, provider in list(KEY_ENV_PROVIDER.items()):
        value = (env.get(name) or "").strip()
        if value and not value.startswith(TOKEN_PREFIX):
            shield(env, provider, value, settings=settings, proxy=proxy)
    return env


def holds_provider_key(env: Mapping[str, str]) -> list[str]:
    """The names in `env` that carry a provider API key rather than a run token (the guard the
    tests hold every engine to). A subscription login is not an API key and is not counted."""
    names = set(KEY_ENV_PROVIDER) | {"XAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"}
    return sorted(n for n in names
                  if (env.get(n) or "").strip()
                  and not str(env.get(n)).startswith(TOKEN_PREFIX))


__all__ = ["ADAPTERS", "ANTHROPIC", "Grant", "Issued", "ModelProxy", "OPENAI", "OPENROUTER",
           "ProviderAdapter", "ProxyUnavailable", "TOKEN_PREFIX", "get", "holds_provider_key",
           "scoped", "session_scope", "session_ttl", "set_proxy", "shield", "shield_env"]
