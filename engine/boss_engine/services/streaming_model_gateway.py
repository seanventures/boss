"""THE CENTRAL STREAMING MODEL GATEWAY (P8b S10, BOSSAPP-1071; boss-agent-hosts-lld.md §3.9).

WHY A SEPARATE STREAMING SERVICE
--------------------------------
A microVM guest holds no provider key (G4). Its model client is pointed at the guest gateway in
`boss-hostd` (`127.0.0.1:7101/<provider>`, credential `boss_mdl_<run>`), which identifies the VM
by its vsock socket, strips every guest header and forwards here with the HOST's identity. This
service is where the real key is attached: it is read through `resolve_secret_ref` (scoped and
logged), held in memory only, and re-read when the reference rotates. boss-egress buffers each
response whole, and a model answer streams for minutes, so the model route is its own listener:
every chunk is relayed as it arrives, with back-pressure (a slow guest reader slows the upstream
read instead of filling memory), and nothing is ever buffered past the chunk in flight.

(`model_gateway.py` beside this module is the older model CAPACITY lease service — authority
brokering for workers that hold keys themselves. This module is the credential BOUNDARY.)

ONE IMPLEMENTATION, TWO LANES
-----------------------------
This module is the single implementation of the model-credential boundary:

  * provider adapters (Anthropic, OpenAI-compatible, OpenRouter, Azure OpenAI, Bedrock) with
    their PINNED inference paths — a stolen handle can call `/v1/messages`, never list files,
    batch, or reach an admin API;
  * the header rewrite: every inbound credential, cookie, forwarding and `X-Boss-*` header is
    dropped; the real credential goes on in the provider's own form, or the request is SIGNED
    (Bedrock SigV4), as Tokenizer's processors do;
  * the usage reader that accounts tokens from the provider's own stream, never from the
    agent's report;
  * the handle registry: `boss_mdl_` handles stored hashed and bound to (run, fence_epoch,
    vm_id), revoked at run end, revoked AND reported (`host_events(handle_replay)`) when
    presented from the wrong VM, refused `409 fenced` within one request of the run's epoch
    moving;
  * per-run budget reserve/settle through `BudgetGovernor` (via `model_routing.governed`), a
    ledger row per call (`record_llm_call`), and one `guest_calls` audit row per call (enqueued,
    written in batches, never on the request path).

The loopback proxy of the local lane (`model_proxy.py`) imports its adapters, header rewrite and
usage reader from here, so a `runtime: local` run and a microVM run cross the same code. The
central service (`apps/model-gateway`) mounts `ModelGateway.handle` as its ASGI handler.

WHO MAY CALL
------------
Callers are enrolled agent hosts: every forward carries `Authorization: BossHost <proof>`
(services/fleet_ca.verify_proof — pinned certificate, htm/htu, 60 s, jti spent once), the guest's
handle moved into `X-Boss-Handle`, and the host's word on which VM, run and epoch the call came
from (`X-Boss-Vm`, `X-Boss-Run`, `X-Boss-Fence-Epoch`, `X-Boss-Deployment`), plus the
`Boss-Run-Assertion` boss-hostd already signs. The handle's own binding must AGREE with the host's
word; a disagreement is a replay. docs/infra/HOST_API.md §4.6 is the contract the Go side sends.
`BOSS_MODEL_GATEWAY_DIRECT=1` additionally accepts a handle presented directly in `x-api-key` /
`Authorization: Bearer` (the local lane's thin client on the compose network; off by default).

FAIL CLOSED
-----------
An unknown provider, path, handle, model, host, epoch or key never lets a byte go upstream. A
placeholder (`boss_mdl_…`) is never forwarded under any header. There is no direct path to retry
on (S18).
"""
from __future__ import annotations

import base64
import collections
import contextlib
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Optional
from urllib.parse import quote, unquote, urlsplit

log = logging.getLogger("boss.model_gateway")

TOKEN_PREFIX = "boss_mdl_"
#: The principal the gateway reads keys as (secret_access_events names it).
GATEWAY_PRINCIPAL = "sys_model_gateway"
#: Where the host puts the guest's handle after stripping it from the guest's own header.
HANDLE_HEADER = "x-boss-handle"
VM_HEADER = "x-boss-vm"
RUN_HEADER = "x-boss-run"
EPOCH_HEADER = "x-boss-fence-epoch"
DEPLOYMENT_HEADER = "x-boss-deployment"
ASSERTION_HEADER = "boss-run-assertion"
TRACE_HEADER = "traceparent"
HOST_AUTH_SCHEME = "bosshost"
#: A handle outlives no run: the longest session BOSS starts, plus grace (model_proxy.session_ttl).
HANDLE_TTL_SECONDS = 3600 + 900
MAX_BODY_BYTES = 64 * 1024 * 1024
#: How much of a non-streamed answer is kept to read its `usage` (the body itself is streamed).
_USAGE_KEEP = 4 * 1024 * 1024
#: How long a resolved key is kept before its reference is re-read regardless; rotation is
#: noticed earlier, by the reference's `updated_at` on every call.
KEY_CACHE_SECONDS = 300
DIRECT_ENV = "BOSS_MODEL_GATEWAY_DIRECT"
URL_ENV = "BOSS_MODEL_GATEWAY_URL"

#: Never forwarded, in either direction: hop-by-hop headers and every credential header a client
#: could send under any provider's name (E2B's fail-closed rule: a same-named guest header never
#: survives), plus every forwarding and BOSS-internal header.
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailer", "trailers", "transfer-encoding", "upgrade", "host",
                         "content-length"})
CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key",
                                "cookie", "proxy-authorization", "x-auth-token", "private-token",
                                "x-amz-security-token", "x-amz-date", "x-amz-content-sha256"})
_FORWARDING_PREFIXES = ("x-forwarded-", "x-boss-", "boss-", "x-amz-")
_FORWARDING_HEADERS = frozenset({"forwarded", "x-real-ip"})


def dropped_header(name: str) -> bool:
    """Whether an inbound header never reaches the upstream."""
    name = name.lower()
    return (name in HOP_HEADERS or name in CREDENTIAL_HEADERS or name in _FORWARDING_HEADERS
            or name.startswith(_FORWARDING_PREFIXES) or name == "accept-encoding")


# ── Providers ─────────────────────────────────────────────────────────────────────────────────

_MODEL_ID = r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}"
#: A Bedrock model id or inference-profile ARN, as it appears percent-encoded in a path segment.
_BEDROCK_MODEL = r"[A-Za-z0-9][A-Za-z0-9._%:-]{0,255}"


@dataclass(frozen=True)
class ProviderAdapter:
    """One provider's model API: where it is, which paths are model calls, how the credential
    goes on the request, where the model name is, and which environment names a client reads."""

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
    #: "header": the credential goes on `header`; "sigv4": the request is signed (Bedrock).
    sign: str = "header"
    #: A regex with one group that names the model in the PATH (Azure deployments, Bedrock
    #: model ids); None reads the JSON body's `model`.
    model_path: Optional[str] = None
    #: The SigV4 service name, for `sign == "sigv4"`.
    service: str = ""

    def allows(self, method: str, path: str) -> bool:
        return any(method.upper() == m and re.fullmatch(p, path) for m, p in self.paths)

    def credential_header(self, credential: str) -> tuple[str, str]:
        return self.header, self.header_format.format(credential)

    def resolve_upstream(self, override: Optional[str] = None) -> str:
        if override:
            return override.rstrip("/")
        for name in self.upstream_env:
            value = (os.environ.get(name) or "").strip()
            if value and not is_ours(value):
                return value.rstrip("/")
        return self.upstream

    def model_of(self, path: str, body: Optional[bytes]) -> str:
        """The model a request names, from the path or the body; "" when it names none."""
        if self.model_path:
            found = re.fullmatch(self.model_path, path)
            return unquote(found.group(1)) if found else ""
        if not body:
            return ""
        try:
            parsed = json.loads(body)
        except ValueError:
            return ""
        model = parsed.get("model") if isinstance(parsed, dict) else None
        return str(model) if isinstance(model, str) else ""


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
#: Azure OpenAI: the upstream is the operator's resource (`https://<name>.openai.azure.com`),
#: set per handle or by AZURE_OPENAI_ENDPOINT in the gateway; the deployment in the path is the
#: model the allowlist judges. The `api-version` query string passes through.
AZURE_OPENAI = ProviderAdapter(
    name="azure_openai", upstream="",
    paths=(("POST", r"/openai/deployments/[^/]+/chat/completions"),
           ("POST", r"/openai/deployments/[^/]+/completions"),
           ("POST", r"/openai/deployments/[^/]+/embeddings"),
           ("POST", r"/openai/responses"), ("POST", r"/openai/v1/responses"),
           ("POST", r"/openai/v1/chat/completions"),
           ("GET", r"/openai/models"), ("GET", r"/openai/v1/models")),
    header="api-key", header_format="{}",
    key_env=("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY"),
    base_env=(("AZURE_OPENAI_ENDPOINT", ""),),
    upstream_env=("AZURE_OPENAI_ENDPOINT",),
    model_path=r"/openai/deployments/([^/]+)/.*",
)
#: Bedrock: the gateway SIGNS (SigV4) with the access key pair the credential holds
#: (`AKIA…:secret[:session]`), or sends a Bedrock API key as a bearer. The upstream is the
#: regional runtime endpoint, set per handle (`upstream`: a region or a URL) or from AWS_REGION.
BEDROCK = ProviderAdapter(
    name="bedrock", upstream="",
    paths=(("POST", rf"/model/{_BEDROCK_MODEL}/invoke"),
           ("POST", rf"/model/{_BEDROCK_MODEL}/invoke-with-response-stream"),
           ("POST", rf"/model/{_BEDROCK_MODEL}/converse"),
           ("POST", rf"/model/{_BEDROCK_MODEL}/converse-stream")),
    header="authorization", header_format="Bearer {}",
    key_env=("AWS_BEARER_TOKEN_BEDROCK",),
    base_env=(("BOSS_BEDROCK_BASE_URL", ""),),
    sign="sigv4", service="bedrock",
    model_path=rf"/model/({_BEDROCK_MODEL})/(?:invoke|invoke-with-response-stream|converse|"
               r"converse-stream)",
)

ADAPTERS: dict[str, ProviderAdapter] = {
    a.name: a for a in (ANTHROPIC, OPENAI, OPENROUTER, AZURE_OPENAI, BEDROCK)}
#: Which adapter a provider key variable belongs to (model_proxy.shield_env).
KEY_ENV_PROVIDER: dict[str, str] = {name: a.name for a in ADAPTERS.values() for name in a.key_env}
#: The hosts a strict host removes from the opaque CONNECT allowlist (boss_egress.connect): the
#: only path to a model is the injecting route.
MODEL_HOSTS: tuple[str, ...] = (
    "api.anthropic.com", "statsig.anthropic.com", "console.anthropic.com", "claude.ai",
    "api.openai.com", "chatgpt.com", "auth.openai.com", "api.x.ai",
    "generativelanguage.googleapis.com", "openrouter.ai", "*.openai.azure.com",
    "*.amazonaws.com",
)


def gateway_url() -> str:
    """Where the central gateway is (BOSS_MODEL_GATEWAY_URL); "" when there is none."""
    return (os.environ.get(URL_ENV) or "").strip().rstrip("/")


def is_ours(url: str) -> bool:
    """A URL that already points at a BOSS model proxy or gateway (never an upstream)."""
    url = (url or "").rstrip("/")
    if url.startswith("http://127.0.0.1:") and any(f"/{name}" in url for name in ADAPTERS):
        return True
    base = gateway_url()
    return bool(base) and url.startswith(base + "/")


def bedrock_upstream(region: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com"


def upstream_for(adapter: ProviderAdapter, override: Optional[str]) -> str:
    """The upstream base of a request; "" when the adapter has none configured (503)."""
    if adapter is BEDROCK:
        if override and override.startswith("https://"):
            return override.rstrip("/")
        region = (override or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
                  or "").strip()
        return bedrock_upstream(region) if region else ""
    return adapter.resolve_upstream(override)


# ── Header rewrite and signing ────────────────────────────────────────────────────────────────

class PlaceholderRefused(ValueError):
    """A `boss_mdl_` value was about to be sent upstream as if it were a key."""


def outbound_headers(adapter: ProviderAdapter, headers: Mapping[str, str], credential: str, *,
                     method: str = "POST", url: str = "", body: bytes = b"",
                     now: Optional[_dt.datetime] = None) -> dict[str, str]:
    """`headers` with every guest credential, cookie, forwarding and BOSS header removed and the
    REAL credential attached in the provider's own form — or, for a SigV4 provider, the request
    signed. `accept-encoding: identity` so the usage can be read from the bytes as they pass."""
    out = {k: v for k, v in headers.items() if not dropped_header(k)}
    if not credential or credential.startswith(TOKEN_PREFIX):
        raise PlaceholderRefused("a placeholder is never a credential")
    if adapter.sign == "sigv4" and _looks_like_access_pair(credential):
        access_key, secret_key, session = _split_access_pair(credential)
        out.update(sigv4_headers(method, url, out, body, access_key=access_key,
                                 secret_key=secret_key, session_token=session,
                                 region=_region_of(url), service=adapter.service, now=now))
    else:
        name, value = adapter.credential_header(credential)
        out[name] = value
    out["accept-encoding"] = "identity"
    return out


def _looks_like_access_pair(credential: str) -> bool:
    head = credential.split(":", 1)[0]
    return ":" in credential and bool(re.fullmatch(r"(AKIA|ASIA)[A-Z0-9]{16}", head))


def _split_access_pair(credential: str) -> tuple[str, str, Optional[str]]:
    parts = credential.split(":", 2)
    return parts[0], parts[1], (parts[2] if len(parts) > 2 and parts[2] else None)


def _region_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    found = re.match(r"bedrock-runtime(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com$", host)
    return found.group(1) if found else (os.environ.get("AWS_REGION") or "us-east-1")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, headers: Mapping[str, str], body: bytes, *,
                  access_key: str, secret_key: str, session_token: Optional[str], region: str,
                  service: str, now: Optional[_dt.datetime] = None,
                  amz_date: Optional[str] = None) -> dict[str, str]:
    """The AWS Signature Version 4 headers for `method url headers body` (AWS General Reference,
    "Signing AWS API requests"): `x-amz-date`, `x-amz-security-token` when a session token is
    held, and `authorization`. Every header in `headers` is signed, plus `host`. Verified against
    botocore's own signer in the tests. The secret key never leaves this function."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if amz_date is None:
        at = now or _dt.datetime.now(_dt.timezone.utc)
        amz_date = at.strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]
    signed: dict[str, str] = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    if session_token:
        signed["x-amz-security-token"] = session_token
    names = sorted(signed)
    canonical_headers = "".join(f"{n}:{signed[n]}\n" for n in names)
    signed_headers = ";".join(names)
    # The path is signed as it is sent (percent-encoded once by the client), encoded once more
    # for the canonical form — what botocore does for every service but S3.
    canonical_uri = quote(parts.path or "/", safe="/~")
    pairs = []
    for item in (parts.query.split("&") if parts.query else []):
        if not item:
            continue
        key, _, value = item.partition("=")
        pairs.append((quote(unquote(key), safe="-_.~"), quote(unquote(value), safe="-_.~")))
    canonical_query = "&".join(f"{k}={v}" for k, v in sorted(pairs))
    canonical_request = "\n".join([method.upper(), canonical_uri, canonical_query,
                                   canonical_headers, signed_headers, _sha256_hex(body or b"")])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                _sha256_hex(canonical_request.encode("utf-8"))])
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {"x-amz-date": amz_date,
           "authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                             f"SignedHeaders={signed_headers}, Signature={signature}")}
    if session_token:
        out["x-amz-security-token"] = session_token
    return out


# ── Usage, read from the stream ───────────────────────────────────────────────────────────────

class UsageReader:
    """Reads the provider's own `usage` out of an answer as it streams past: SSE events
    (Anthropic message_start / message_delta, OpenAI's final chunk), one JSON body, or a Bedrock
    converse body (`inputTokens` / `outputTokens`). Never buffers a streamed body: SSE lines are
    parsed as they complete; a JSON body keeps at most _USAGE_KEEP bytes; any other body type
    keeps nothing."""

    def __init__(self, content_type: str) -> None:
        content_type = (content_type or "").lower()
        self.sse = "text/event-stream" in content_type
        #: Only a JSON answer is kept (bounded) to read its `usage`; any other body type has
        #: no usage to read and is relayed without a byte retained.
        self.json = not self.sse and "json" in content_type
        self.buf = b""
        self.parts: list[bytes] = []
        self.kept = 0
        self.found: dict[str, Any] = {}

    def feed(self, chunk: bytes) -> None:
        if self.sse:
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                self._sse_line(line)
        elif self.json and self.kept < _USAGE_KEEP:
            self.parts.append(chunk[: _USAGE_KEEP - self.kept])
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
            for key in ("message", "response"):
                inner = event.get(key)
                if isinstance(inner, dict):
                    self._take(inner.get("usage"))
                    if isinstance(inner.get("model"), str):
                        self.found["model"] = inner["model"]
            if isinstance(event.get("model"), str):
                self.found["model"] = event["model"]

    def _take(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        names = {"input_tokens": ("input_tokens", "prompt_tokens", "inputTokens"),
                 "output_tokens": ("output_tokens", "completion_tokens", "outputTokens"),
                 "cache_read_input_tokens": ("cache_read_input_tokens",
                                             "cacheReadInputTokens"),
                 "cache_creation_input_tokens": ("cache_creation_input_tokens",
                                                 "cacheWriteInputTokens")}
        for key, sources in names.items():
            for src in sources:
                value = usage.get(src)
                if isinstance(value, int) and value >= 0:
                    # message_delta carries the running total, so the last value wins.
                    self.found[key] = value
                    break

    def finish(self) -> dict[str, Any]:
        if self.sse:
            self._sse_line(self.buf)
        elif self.json:
            try:
                body = json.loads(b"".join(self.parts) or b"null")
            except ValueError:
                body = None
            self.parts = []
            if isinstance(body, dict):
                self._take(body.get("usage"))
                if isinstance(body.get("model"), str):
                    self.found["model"] = body["model"]
        return dict(self.found)


def as_ledger_usage(found: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    """The shape `estimate_cost_usd_micros` and the receipts read (prompt/completion tokens)."""
    out: dict[str, Any] = {"model": str(found.get("model") or model or ""),
                           "prompt_tokens": int(found.get("input_tokens") or 0),
                           "completion_tokens": int(found.get("output_tokens") or 0)}
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens",
                "output_tokens"):
        if key in found:
            out[key] = int(found[key])
    out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


# ── The handle registry (control plane + gateway) ─────────────────────────────────────────────

def hash_handle(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprint(raw: str) -> str:
    """What audit rows carry instead of a handle."""
    return hash_handle(raw)[:16]


@dataclass
class Handle:
    """One row of model_gateway_handles, as the gateway sees it (never the raw handle)."""

    id: str
    token_hash: str
    org_id: str
    project_id: str
    run_id: str
    fence_epoch: int
    vm_id: Optional[str]
    deployment_id: Optional[str]
    provider: str
    upstream: Optional[str]
    credential_ref: str = field(repr=False)
    models: tuple[str, ...] = ()
    expires_at: Optional[_dt.datetime] = None
    revoked_at: Optional[_dt.datetime] = None

    def live(self, now: Optional[_dt.datetime] = None) -> bool:
        at = now or _dt.datetime.now(_dt.timezone.utc)
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > at)


_REF_KINDS = ("secret_ref", "env", "llm_gateway")


def issue_handle(connection: Any, *, org_id: str, project_id: str, run_id: str, epoch: int,
                 vm_id: Optional[str], provider: str, credential_ref: str,
                 upstream: Optional[str] = None, models: Iterable[str] = (),
                 deployment_id: Optional[str] = None,
                 ttl_seconds: int = HANDLE_TTL_SECONDS) -> str:
    """Mint a `boss_mdl_` handle bound to (run, epoch, vm). Returns the raw handle ONCE; only its
    hash is stored, and the value is registered with the redactor. `credential_ref` names where
    the key is read (secret_ref:<id> | env:<NAME> | llm_gateway:<id>) — a value is refused."""
    from sqlalchemy import text

    from ..identity.ids import new_id

    if provider not in ADAPTERS:
        raise ValueError(f"unknown provider {provider!r}")
    kind, _, rest = credential_ref.partition(":")
    if kind not in _REF_KINDS or not rest:
        raise ValueError("credential_ref names a reference (secret_ref:|env:|llm_gateway:), "
                         "never a value")
    raw = TOKEN_PREFIX + secrets.token_hex(32)
    _register_redaction(raw)
    connection.execute(text(
        "INSERT INTO model_gateway_handles (id, token_hash, org_id, project_id, run_id, "
        "fence_epoch, vm_id, deployment_id, provider, upstream, credential_ref, models, "
        "expires_at) VALUES (:id, :h, :o, :p, :r, :e, :v, :d, :pr, :u, :c, :m, :x)"),
        {"id": new_id("mdl"), "h": hash_handle(raw), "o": org_id, "p": project_id, "r": run_id,
         "e": int(epoch), "v": vm_id, "d": deployment_id, "pr": provider, "u": upstream,
         "c": credential_ref, "m": [str(m) for m in models],
         "x": _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=max(60, ttl_seconds))})
    return raw


def load_handle(connection: Any, raw: str) -> Optional[Handle]:
    from sqlalchemy import text

    if not raw or not raw.startswith(TOKEN_PREFIX) or len(raw) > 128:
        return None
    row = connection.execute(text(
        "SELECT id, token_hash, org_id, project_id, run_id, fence_epoch, vm_id, deployment_id, "
        "provider, upstream, credential_ref, models, expires_at, revoked_at "
        "FROM model_gateway_handles WHERE token_hash = :h"), {"h": hash_handle(raw)}
    ).mappings().first()
    if row is None:
        return None
    return Handle(id=row["id"], token_hash=row["token_hash"], org_id=row["org_id"],
                  project_id=row["project_id"], run_id=row["run_id"],
                  fence_epoch=int(row["fence_epoch"] or 0), vm_id=row["vm_id"],
                  deployment_id=row["deployment_id"], provider=row["provider"],
                  upstream=row["upstream"], credential_ref=row["credential_ref"],
                  models=tuple(row["models"] or ()), expires_at=row["expires_at"],
                  revoked_at=row["revoked_at"])


def revoke_handle(connection: Any, token_hash: str, reason: str) -> None:
    from sqlalchemy import text

    connection.execute(text(
        "UPDATE model_gateway_handles SET revoked_at = now(), revoked_reason = :r "
        "WHERE token_hash = :h AND revoked_at IS NULL"), {"h": token_hash, "r": reason[:200]})


def revoke_run(connection: Any, run_id: str, reason: str = "run ended") -> int:
    """Every handle of a run (and its children) dies with it; returns how many."""
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            result = connection.execute(text(
                "UPDATE model_gateway_handles SET revoked_at = now(), revoked_reason = :re "
                "WHERE (run_id = :r OR run_id LIKE :c) AND revoked_at IS NULL"),
                {"r": run_id, "c": run_id + "/%", "re": reason[:200]})
            return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001 — an install mid-upgrade: the table arrives with the schema
        log.debug("model_gateway_handles not available; nothing revoked for %s", run_id)
        return 0


def current_epoch(connection: Any, run_id: str) -> Optional[int]:
    """The epoch the run's lease is at NOW (jobs.fence_epoch, else code_task_runs.fence_epoch);
    None when the run has no fenced lease (a local-lane run)."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT fence_epoch FROM jobs WHERE payload->>'run_id' = :r "
        "ORDER BY created_at DESC LIMIT 1"), {"r": run_id}).scalar()
    if row is not None:
        return int(row)
    row = connection.execute(text(
        "SELECT fence_epoch FROM code_task_runs WHERE id = :r"), {"r": run_id}).scalar()
    return int(row) if row is not None else None


def credential_ref_for(connection: Any, settings: Any, project_id: str, provider: str
                       ) -> Optional[str]:
    """WHERE the gateway reads this project's key for `provider`, as a reference (never a
    value): an active operator LLM gateway for the protocol first (llm_gateway), then the
    project's or org's stored reference (secret_refs), then the gateway's own environment."""
    try:
        from . import llm_gateway

        harness = {"anthropic": "claude_code", "openai": "codex"}.get(provider)
        row = llm_gateway._active_for(connection, project_id, harness) if harness else None  # noqa: SLF001
        if row:
            return f"llm_gateway:{row['id']}"
    except Exception:  # noqa: BLE001 — no gateway table, no gateway
        pass
    if provider == "anthropic":
        try:
            from .claude_credential import _project_ref

            ref = _project_ref(connection, project_id, token=False)
            if ref:
                return f"secret_ref:{ref}"
        except Exception:  # noqa: BLE001
            pass
    adapter = ADAPTERS.get(provider)
    if adapter is None or not adapter.key_env:
        return None
    try:
        from sqlalchemy import text

        ref = connection.execute(text(
            "SELECT id FROM secret_refs WHERE ref_key = ANY(:k) AND status = 'active' "
            "AND (project_id = :p OR scope_type IN ('org', 'workspace')) "
            "ORDER BY (project_id = :p) DESC, created_at DESC LIMIT 1"),
            {"k": list(adapter.key_env), "p": project_id}).scalar()
        if ref:
            return f"secret_ref:{ref}"
    except Exception:  # noqa: BLE001
        pass
    return f"env:{adapter.key_env[0]}"


def provider_for_project(connection: Any, settings: Any, project_id: str) -> str:
    """Which provider a deployed agent's kernel calls: from the project's routed source."""
    try:
        from .model_routing import choose, routed_settings

        engine = (choose(connection, routed_settings(connection, settings, project_id),
                         project_id).engine or "").lower()
    except Exception:  # noqa: BLE001
        engine = ""
    if "azure" in engine:
        return "azure_openai"
    if "openai" in engine or "codex" in engine:
        return "openai"
    if "bedrock" in engine:
        return "bedrock"
    if "openrouter" in engine:
        return "openrouter"
    return "anthropic"


def guest_handles(connection: Any, settings: Any, *, org_id: str, project_id: str, run_id: str,
                  epoch: int, vm_id: Optional[str], deployment_id: Optional[str] = None,
                  provider: Optional[str] = None, models: Iterable[str] = ()) -> dict[str, str]:
    """The `handles` a guest gets for its run (contract v1.1 `handles`, the work item's
    `handles`): today `mdl`, with the provider it is for. Empty when the project has no
    readable model source."""
    provider = provider or provider_for_project(connection, settings, project_id)
    ref = credential_ref_for(connection, settings, project_id, provider)
    if not ref:
        return {}
    raw = issue_handle(connection, org_id=org_id, project_id=project_id, run_id=run_id,
                       epoch=epoch, vm_id=vm_id, provider=provider, credential_ref=ref,
                       models=models, deployment_id=deployment_id)
    return {"mdl": raw, "mdl_provider": provider}


def _register_redaction(value: str) -> None:
    try:
        from ..identity.redaction import register_secrets

        register_secrets([value])
    except Exception:  # noqa: BLE001 — redaction must never stop a run
        pass


# ── Reading the real key ──────────────────────────────────────────────────────────────────────

class KeyUnavailable(RuntimeError):
    """The gateway could not read the key a handle refers to. Names the reference kind only."""


class KeyResolver:
    """Real keys, in memory only, read through `resolve_secret_ref` as the gateway's own
    principal (every read is a secret_access_events row) and RE-READ ON ROTATION: the
    reference's `updated_at` and status are checked on every call, so a rotated or revoked
    reference is noticed on the next request, not after a TTL."""

    def __init__(self, engine: Any, settings: Any, *, ttl: float = KEY_CACHE_SECONDS) -> None:
        self.engine, self.settings, self.ttl = engine, settings, ttl
        self._cache: dict[str, tuple[float, str, str]] = {}   # ref -> (expires, version, value)
        self._lock = threading.Lock()

    def resolve(self, ref: str, project_id: str) -> str:
        kind, _, rest = ref.partition(":")
        if kind == "env":
            value = (os.environ.get(rest) or "").strip()
            if not value:
                raise KeyUnavailable(f"env:{rest} is not set in the gateway's environment")
            if value.startswith(TOKEN_PREFIX):
                raise KeyUnavailable(f"env:{rest} holds a placeholder, not a key")
            _register_redaction(value)
            return value
        if kind == "secret_ref":
            return self._secret_ref(rest, project_id)
        if kind == "llm_gateway":
            return self._llm_gateway(rest)
        raise KeyUnavailable("unknown credential reference kind")

    @staticmethod
    def _version(connection: Any, ref_id: str) -> str:
        from sqlalchemy import text

        row = connection.execute(text(
            "SELECT status, updated_at, revoked_at FROM secret_refs WHERE id = :i"),
            {"i": ref_id}).mappings().first()
        if row is None:
            raise KeyUnavailable("the secret reference no longer exists")
        if row["status"] not in ("active", "rotating", "rotation_due") or row["revoked_at"]:
            raise KeyUnavailable(f"the secret reference is {row['status']}")
        return f"{row['updated_at']}"

    def _secret_ref(self, ref_id: str, project_id: str) -> str:
        from .secret_backends import SecretUnavailable, resolve_secret_ref

        with self.engine.begin() as connection:
            version = self._version(connection, ref_id)
            with self._lock:
                hit = self._cache.get(f"secret_ref:{ref_id}")
            if hit and hit[0] > time.monotonic() and hit[1] == version:
                return hit[2]
            try:
                value = (resolve_secret_ref(connection, ref_id, self.settings, project_id,
                                            principal=GATEWAY_PRINCIPAL) or "").strip()
            except SecretUnavailable as exc:
                raise KeyUnavailable(f"the secret reference could not be read: {exc}") from None
        if not value:
            raise KeyUnavailable("the secret reference is empty")
        with self._lock:
            self._cache[f"secret_ref:{ref_id}"] = (time.monotonic() + self.ttl, version, value)
        return value

    def _llm_gateway(self, gateway_id: str) -> str:
        from . import llm_gateway

        with self._lock:
            hit = self._cache.get(f"llm_gateway:{gateway_id}")
        if hit and hit[0] > time.monotonic():
            return hit[2]
        try:
            value = (llm_gateway._store(self.settings).get(  # noqa: SLF001
                llm_gateway.secret_key_for(gateway_id)) or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise KeyUnavailable(f"the LLM gateway's key could not be read: {type(exc).__name__}") \
                from None
        if not value:
            raise KeyUnavailable("the LLM gateway has no key saved")
        _register_redaction(value)
        with self._lock:
            self._cache[f"llm_gateway:{gateway_id}"] = (time.monotonic() + self.ttl, "", value)
        return value

    def forget(self, ref: Optional[str] = None) -> None:
        with self._lock:
            if ref is None:
                self._cache.clear()
            else:
                self._cache.pop(ref, None)


# ── Budget ────────────────────────────────────────────────────────────────────────────────────

class GovernedBudget:
    """Per-run budget through the same gate the kernel's turns use (model_routing.governed →
    BudgetGovernor.reserve): one reservation per model call, settled with the usage read from
    the stream, released when the call fails. `reserve` raises ModelSpendRefused at exhaustion."""

    SKILL = "model.gateway"

    def __init__(self, engine: Any, settings: Any) -> None:
        self.engine, self.settings = engine, settings

    def reserve(self, project_id: str, run_id: str, model: str) -> tuple[Any, tuple[str, str]]:
        from .model_routing import governed

        with self.engine.begin() as connection:
            gate = governed(None, connection, self.settings, project_id, skill_id=self.SKILL,
                            ref=run_id)
            held = gate._reserve(connection)  # noqa: SLF001 — the kernel's own seam (run_plane)
        return gate, held

    def settle(self, token: tuple[Any, tuple[str, str]], usage: Mapping[str, Any]) -> None:
        gate, held = token
        with self.engine.begin() as connection:
            gate._settle_on(connection, held, dict(usage))  # noqa: SLF001

    def fail(self, token: tuple[Any, tuple[str, str]], error: BaseException) -> None:
        gate, held = token
        gate._fail(held, error)  # noqa: SLF001


# ── Audit ─────────────────────────────────────────────────────────────────────────────────────

_AUDIT_SQL = ("INSERT INTO guest_calls (org_id, host_id, call_id, at, vm_id, run_id, epoch, "
              "deployment_id, route, dest_host, method, path_template, status, bytes_in, "
              "bytes_out, duration_ms, handle_fp, verdict, rule_id, rejected_by, trace_id) "
              "VALUES (:org_id, :host_id, :call_id, :at, :vm_id, :run_id, :epoch, "
              ":deployment_id, 'model', :dest_host, :method, :path_template, :status, :bytes_in, "
              ":bytes_out, :duration_ms, :handle_fp, :verdict, :rule_id, :rejected_by, "
              ":trace_id) ON CONFLICT (host_id, call_id) DO NOTHING")


def path_template(path: str) -> str:
    """At most three segments, model ids and deployment names replaced by `*`."""
    parts = [p for p in path.split("/") if p]
    out = []
    for i, part in enumerate(parts[:3]):
        prev = parts[i - 1] if i else ""
        out.append("*" if prev in ("model", "deployments", "models") else part)
    return "/" + "/".join(out)


class AuditQueue:
    """One guest_calls row per call, enqueued on the request path and written in batches by a
    background thread (never on the request path, §2.15.3). `flush()` writes synchronously
    (tests, shutdown). Rows never carry a body, a query string or a handle."""

    def __init__(self, engine: Any, *, interval: float = 1.0, batch: int = 200,
                 keep: int = 10_000) -> None:
        self.engine, self.interval, self.batch = engine, interval, batch
        self._rows: collections.deque = collections.deque(maxlen=keep)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0

    def put(self, row: Mapping[str, Any]) -> None:
        with self._lock:
            if len(self._rows) == self._rows.maxlen:
                self.dropped += 1
            self._rows.append(dict(row))

    def pending(self) -> int:
        with self._lock:
            return len(self._rows)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="model-gateway-audit")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 — the database is away; rows wait
                log.warning("guest_calls audit batch could not be written")

    def flush(self) -> int:
        from sqlalchemy import text

        written = 0
        while True:
            with self._lock:
                rows = [self._rows.popleft() for _ in range(min(self.batch, len(self._rows)))]
            if not rows:
                return written
            try:
                with self.engine.begin() as connection:
                    connection.execute(text(_AUDIT_SQL), rows)
            except Exception:
                with self._lock:
                    self._rows.extendleft(reversed(rows))
                raise
            written += len(rows)


# ── The gateway ───────────────────────────────────────────────────────────────────────────────

class Refused(Exception):
    """A request that goes no further. `reason` is the named code the client and the audit row
    carry; `status` the HTTP status."""

    def __init__(self, status: int, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.status, self.reason = status, reason


def refusal_body(status: int, reason: str, message: str = "") -> bytes:
    kind = {401: "authentication_error", 402: "billing_error", 403: "permission_error",
            404: "not_found_error", 409: "conflict_error", 413: "request_too_large",
            429: "rate_limit_error"}.get(status, "api_error")
    return json.dumps({"type": "error", "error": {
        "type": kind, "code": reason,
        "message": f"boss model gateway: {message or reason}"}}).encode()


@dataclass
class Caller:
    """Who forwarded the request: an enrolled host (with its word on the VM, run and epoch), or
    the local lane's thin client (`direct`)."""

    host_id: str
    org_id: Optional[str]
    direct: bool = False
    vm_id: Optional[str] = None
    run_id: Optional[str] = None
    epoch: Optional[int] = None
    deployment_id: Optional[str] = None
    trace_id: Optional[str] = None


#: (method, url, headers, body) -> an async context manager yielding
#: (status, headers, async chunk iterator). Chunks are yielded as the upstream sends them.
AsyncUpstream = Callable[[str, str, Mapping[str, str], bytes], Any]


@contextlib.asynccontextmanager
async def httpx_upstream(method: str, url: str, headers: Mapping[str, str], body: bytes
                         ) -> AsyncIterator[tuple[int, dict, AsyncIterator[bytes]]]:
    """The default upstream: one streamed httpx request; `aiter_raw` yields each chunk as it
    arrives, and the response is closed when the relay ends, however it ends."""
    import httpx

    timeout = httpx.Timeout(connect=30.0, read=900.0, write=120.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        request = client.build_request(method, httpx.URL(url), headers=dict(headers),
                                       content=body or None)
        response = await client.send(request, stream=True)
        try:
            yield response.status_code, dict(response.headers), response.aiter_raw()
        finally:
            await response.aclose()


def presented_handle(headers: Mapping[str, str]) -> str:
    """The handle a direct client presents: x-api-key, api-key, or a bearer."""
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in ("x-api-key", "api-key", HANDLE_HEADER):
        key = (lowered.get(name) or "").strip()
        if key:
            return key
    scheme, _, value = (lowered.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _budget_refusal(exc: BaseException) -> tuple[int, str]:
    name = type(exc).__name__
    if name == "ModelSpendRefused":
        return 402, "spend_refused"
    if name == "ModelSpendDeferred":
        return 429, "spend_deferred"
    return 503, "budget_unavailable"


class ModelGateway:
    """The request path (module doc). Built once per process by apps/model-gateway; the local
    lane's `model_proxy` shares the pieces above, not this class."""

    def __init__(self, engine: Any, settings: Any, *, upstream: Optional[AsyncUpstream] = None,
                 keys: Optional[KeyResolver] = None, budget: Any = None,
                 audit: Optional[AuditQueue] = None, direct: Optional[bool] = None,
                 verify_host: Optional[Callable[..., Any]] = None,
                 ledger: Optional[Callable[..., Any]] = None) -> None:
        self.engine, self.settings = engine, settings
        self.upstream = upstream or httpx_upstream
        self.keys = keys or KeyResolver(engine, settings)
        self.budget = budget if budget is not None else GovernedBudget(engine, settings)
        self.audit = audit or AuditQueue(engine)
        self.direct = (os.environ.get(DIRECT_ENV, "").strip().lower() in ("1", "true", "yes")
                       if direct is None else bool(direct))
        self.verify_host = verify_host or self._verify_host_proof
        self.ledger = ledger if ledger is not None else self._ledger_row

    # ---- who is calling --------------------------------------------------------------------------
    def _verify_host_proof(self, token: str, *, method: str, path: str) -> tuple[str, str]:
        """(host_id, org_id) of a `BossHost` proof, or Refused (fleet_ca's own codes)."""
        from . import fleet_ca

        try:
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                host = fleet_ca.verify_proof(c, token, htm=method, path=path)
        except fleet_ca.FleetAuthError as exc:
            raise Refused(exc.status, exc.code, "host authentication refused") from None
        return host.host_id, host.org_id

    def caller_of(self, headers: Mapping[str, str], *, method: str, path: str
                  ) -> tuple[Caller, str]:
        """The caller and the handle it presents. A host moves the guest's handle into
        X-Boss-Handle and speaks for the VM; a direct caller presents the handle itself."""
        h = {k.lower(): v for k, v in headers.items()}
        scheme, _, value = (h.get("authorization") or "").partition(" ")
        if scheme.lower() == HOST_AUTH_SCHEME and value.strip():
            host_id, org_id = self.verify_host(value.strip(), method=method, path=path)
            raw = (h.get(HANDLE_HEADER) or "").strip()
            epoch_raw = (h.get(EPOCH_HEADER) or "").strip()
            try:
                epoch = int(epoch_raw) if epoch_raw else None
            except ValueError:
                raise Refused(400, "bad_epoch", "X-Boss-Fence-Epoch is an integer") from None
            caller = Caller(host_id=host_id, org_id=org_id,
                            vm_id=(h.get(VM_HEADER) or "").strip() or None,
                            run_id=(h.get(RUN_HEADER) or "").strip() or None, epoch=epoch,
                            deployment_id=(h.get(DEPLOYMENT_HEADER) or "").strip() or None,
                            trace_id=(h.get(TRACE_HEADER) or "").strip() or None)
            self._cross_check_assertion(h.get(ASSERTION_HEADER), caller)
            return caller, raw
        if self.direct:
            raw = presented_handle(h)
            if raw:
                return Caller(host_id="local", org_id=None, direct=True,
                              trace_id=(h.get(TRACE_HEADER) or "").strip() or None), raw
            raise Refused(401, "no_handle", "no run handle was presented")
        raise Refused(401, "host_proof_required",
                      "the model gateway answers enrolled agent hosts (Authorization: BossHost)")

    @staticmethod
    def _cross_check_assertion(assertion: Optional[str], caller: Caller) -> None:
        """boss-hostd's run assertion (HOST_API.md §4.2) names the same run, epoch and VM as its
        headers; a disagreement is a bug on the host or a tampered forward, so it is refused.
        The signature is the VM SVID's; its chain is verified once the host forwards the SVID
        leaf (P8b S13, HOST_API.md §4.6)."""
        if not assertion:
            return
        try:
            payload = assertion.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except Exception:  # noqa: BLE001
            raise Refused(400, "assertion_malformed") from None
        if caller.run_id and claims.get("run") and str(claims["run"]) != caller.run_id:
            raise Refused(409, "assertion_mismatch", "the assertion names another run")
        if caller.vm_id and claims.get("vm") and str(claims["vm"]) != caller.vm_id:
            raise Refused(409, "assertion_mismatch", "the assertion names another VM")
        if caller.epoch is not None and claims.get("epoch") is not None and \
                int(claims["epoch"]) != caller.epoch:
            raise Refused(409, "assertion_mismatch", "the assertion names another epoch")
        exp = claims.get("exp")
        if isinstance(exp, int) and exp < time.time() - 30:
            raise Refused(401, "assertion_expired")

    # ---- the handle, bound ----------------------------------------------------------------------
    def resolve(self, connection: Any, caller: Caller, raw: str) -> Handle:
        """The handle's row, checked against the host's word: wrong VM or run → replay (revoked,
        reported, refused); stale epoch (the host's or the run's current one) → 409 fenced."""
        if not raw or not raw.startswith(TOKEN_PREFIX):
            raise Refused(401, "no_handle", "no run handle was presented")
        handle = load_handle(connection, raw)
        if handle is None:
            raise Refused(401, "unknown_handle")
        if handle.revoked_at is not None:
            raise Refused(401, "handle_revoked")
        if not handle.live():
            raise Refused(401, "handle_expired")
        if caller.org_id and handle.org_id != caller.org_id:
            self._replay(connection, caller, handle, "another_org")
        if not caller.direct:
            if handle.vm_id and caller.vm_id and handle.vm_id != caller.vm_id:
                self._replay(connection, caller, handle, "another_vm")
            if caller.run_id and handle.run_id != caller.run_id:
                self._replay(connection, caller, handle, "another_run")
            if caller.epoch is not None and caller.epoch != handle.fence_epoch:
                raise Refused(409, "fenced", f"epoch {caller.epoch} is not the handle's")
        now_epoch = current_epoch(connection, handle.run_id)
        if now_epoch is not None and now_epoch != handle.fence_epoch:
            raise Refused(409, "fenced", f"the run moved to epoch {now_epoch}")
        return handle

    def _replay(self, connection: Any, caller: Caller, handle: Handle, why: str) -> None:
        from . import fleet

        revoke_handle(connection, handle.token_hash, f"handle_replay:{why}")
        try:
            fleet.host_event(connection, org_id=handle.org_id, host_id=caller.host_id,
                             vm_id=caller.vm_id, kind="handle_replay", actor="model_gateway",
                             detail={"why": why, "run_id": handle.run_id,
                                     "bound_vm": handle.vm_id, "handle_fp": handle.token_hash[:16],
                                     "presented_run": caller.run_id})
        except Exception:  # noqa: BLE001 — the refusal stands without the event
            log.warning("handle replay on %s could not be recorded", caller.host_id)
        raise Refused(401, "handle_replay", "that handle belongs to another VM; it is revoked")

    def _resolve_in_tx(self, caller: Caller, raw: str) -> Handle:
        with self.engine.begin() as connection:
            return self.resolve(connection, caller, raw)

    # ---- one request ------------------------------------------------------------------------------
    async def handle(self, request: Any) -> Any:
        """Starlette: the whole request path; returns a StreamingResponse or a refusal."""
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import Response, StreamingResponse

        started = time.monotonic()
        raw_path = request.scope.get("raw_path")
        full_path = (bytes(raw_path).split(b"?", 1)[0].decode("latin-1")
                     if isinstance(raw_path, (bytes, bytearray)) else request.url.path)
        provider, _, rest = full_path.lstrip("/").partition("/")
        rest = "/" + rest
        method = request.method.upper()
        headers = {k.lower(): v for k, v in request.headers.items()}
        audit: dict[str, Any] = {
            "org_id": "", "host_id": "unknown", "call_id": secrets.token_hex(12),
            "at": _dt.datetime.now(_dt.timezone.utc), "vm_id": None, "run_id": None,
            "epoch": None, "deployment_id": None, "dest_host": None, "method": method,
            "path_template": path_template(full_path), "status": None, "bytes_in": 0,
            "bytes_out": 0, "duration_ms": 0, "handle_fp": None, "verdict": "deny",
            "rule_id": f"model-{provider}" if provider in ADAPTERS else "default-deny",
            "rejected_by": None, "trace_id": (headers.get(TRACE_HEADER) or "")[:64] or None}

        def refuse(status: int, reason: str, message: str = "") -> Response:
            audit.update(status=status, rejected_by=f"model_gateway:{reason}",
                         duration_ms=int((time.monotonic() - started) * 1000))
            if audit["org_id"]:
                self.audit.put(audit)
            log.info("model gateway refused %s %s: %s", method, audit["path_template"], reason)
            return Response(refusal_body(status, reason, message), status_code=status,
                            media_type="application/json", headers={"connection": "close"})

        adapter = ADAPTERS.get(provider)
        try:
            caller, raw = await run_in_threadpool(self.caller_of, headers, method=method,
                                                  path=full_path)
        except Refused as exc:
            return refuse(exc.status, exc.reason, str(exc))
        audit.update(host_id=caller.host_id, vm_id=caller.vm_id, run_id=caller.run_id,
                     epoch=caller.epoch, deployment_id=caller.deployment_id,
                     org_id=caller.org_id or "", handle_fp=fingerprint(raw) if raw else None)
        if adapter is None:
            return refuse(404, "unknown_provider")
        if not raw or not raw.startswith(TOKEN_PREFIX):
            return refuse(401, "no_handle", "no run handle was presented")
        try:
            handle = await run_in_threadpool(self._resolve_in_tx, caller, raw)
        except Refused as exc:
            return refuse(exc.status, exc.reason, str(exc))
        audit.update(org_id=handle.org_id, run_id=handle.run_id, epoch=handle.fence_epoch,
                     deployment_id=handle.deployment_id or caller.deployment_id,
                     vm_id=caller.vm_id or handle.vm_id)
        if handle.provider != provider:
            return refuse(401, "foreign_provider", "that handle is for another provider")
        if not adapter.allows(method, rest):
            return refuse(403, "not_a_model_path", "only the provider's inference paths")
        base = upstream_for(adapter, handle.upstream)
        if not base:
            return refuse(503, "upstream_unconfigured", "the provider's endpoint is not set")
        audit["dest_host"] = urlsplit(base).netloc
        body = await self._read_body(request)
        if body is None:
            return refuse(413, "body_too_large")
        audit["bytes_in"] = len(body)
        model = adapter.model_of(rest, body)
        if handle.models and model not in handle.models:
            return refuse(403, "model_not_allowed", "that model is not routed for this run")
        held = None
        if self.budget:
            try:
                held = await run_in_threadpool(self.budget.reserve, handle.project_id,
                                               handle.run_id, model)
            except Exception as exc:  # noqa: BLE001 — refused in words, nothing sent
                status, reason = _budget_refusal(exc)
                return refuse(status, reason, str(exc)[:300])
        try:
            credential = await run_in_threadpool(self.keys.resolve, handle.credential_ref,
                                                 handle.project_id)
        except KeyUnavailable as exc:
            await self._release(held, exc)
            return refuse(503, "key_unavailable", str(exc))
        query = request.url.query
        url = base + rest + (f"?{query}" if query else "")
        try:
            out_headers = outbound_headers(adapter, headers, credential, method=method, url=url,
                                           body=body)
        except PlaceholderRefused:
            await self._release(held, RuntimeError("placeholder"))
            return refuse(503, "key_unavailable", "the key read is a placeholder")
        del credential
        try:
            cm = self.upstream(method, url, out_headers, body)
            status, resp_headers, chunks = await cm.__aenter__()
        except Exception as exc:  # noqa: BLE001 — the client gets a 502, never a credential
            log.warning("model gateway upstream failed for %s: %s", provider, type(exc).__name__)
            await self._release(held, exc)
            return refuse(502, "upstream_unreachable")
        lowered = {k.lower(): v for k, v in resp_headers.items()}
        reader = UsageReader(str(lowered.get("content-type", "")))
        passthrough = {k: v for k, v in resp_headers.items() if k.lower() not in HOP_HEADERS
                       and k.lower() != "content-encoding"}
        passthrough["connection"] = "close"
        audit.update(status=int(status), verdict="allow")

        async def relay() -> AsyncIterator[bytes]:
            """Chunk by chunk, with back-pressure: the next upstream chunk is pulled only after
            the previous one was handed to the client; nothing is buffered here."""
            sent = 0
            ok = False
            try:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    reader.feed(chunk)
                    sent += len(chunk)
                    yield chunk
                ok = True
            finally:
                await cm.__aexit__(None, None, None)
                usage = as_ledger_usage(reader.finish(), model=model)
                audit.update(bytes_out=sent, duration_ms=int((time.monotonic() - started) * 1000))
                self.audit.put(audit)
                await self._settle(held, usage, ok=ok and int(status) < 400,
                                   handle=handle, status=int(status), model=model,
                                   latency_ms=audit["duration_ms"])

        return StreamingResponse(relay(), status_code=int(status), headers=passthrough)

    @staticmethod
    async def _read_body(request: Any) -> Optional[bytes]:
        total = 0
        parts = []
        async for part in request.stream():
            total += len(part)
            if total > MAX_BODY_BYTES:
                return None
            parts.append(part)
        return b"".join(parts)

    async def _release(self, held: Any, error: BaseException) -> None:
        if held is None or not self.budget:
            return
        from starlette.concurrency import run_in_threadpool

        try:
            await run_in_threadpool(self.budget.fail, held, error)
        except Exception:  # noqa: BLE001
            log.warning("a model-call reservation could not be released")

    async def _settle(self, held: Any, usage: dict, *, ok: bool, handle: Handle, status: int,
                      model: str, latency_ms: int) -> None:
        from starlette.concurrency import run_in_threadpool

        if held is not None and self.budget:
            try:
                if ok:
                    await run_in_threadpool(self.budget.settle, held, usage)
                else:
                    await run_in_threadpool(self.budget.fail, held,
                                            RuntimeError(f"upstream answered {status}"))
            except Exception:  # noqa: BLE001
                log.warning("a model-call reservation could not be settled")
        if self.ledger:
            try:
                await run_in_threadpool(self.ledger, handle, usage, ok, status, model, latency_ms)
            except Exception:  # noqa: BLE001
                log.warning("a model-call ledger row could not be written")

    def _ledger_row(self, handle: Handle, usage: dict, ok: bool, status: int, model: str,
                    latency_ms: int) -> None:
        """One llm_calls row per call, through the one recording seam (BOSSAPP-998)."""
        from .llm_telemetry import record_llm_call, set_llm_context
        from .openai_adapter import estimate_cost_usd_micros

        set_llm_context(run_id=handle.run_id, project_id=handle.project_id,
                        stage="gateway", model=usage.get("model") or model)
        with self.engine.begin() as connection:
            record_llm_call(engine_name="model_gateway", usage=usage,
                            cost_usd_micros=estimate_cost_usd_micros(usage) if ok else 0,
                            latency_ms=latency_ms, ok=ok,
                            error=None if ok else f"upstream_{status}", stage="gateway",
                            project_id=handle.project_id, connection=connection, capacity=False)


# ── The local lane's thin client ──────────────────────────────────────────────────────────────

class GatewayClient:
    """`shield()`'s other half (BOSS_MODEL_PROXY_VIA_GATEWAY=1): a handle is issued at the
    central gateway (server to server, the egress service key) and the session's base URL points
    at the gateway, so a `runtime: local` run crosses the same gateway a VM does. Off by default:
    with the flag unset the loopback proxy behaves exactly as before."""

    FLAG = "BOSS_MODEL_PROXY_VIA_GATEWAY"

    @classmethod
    def enabled(cls) -> bool:
        return (os.environ.get(cls.FLAG, "").strip().lower() in ("1", "true", "yes")
                and bool(gateway_url()))

    def issue(self, *, provider: str, project_id: str, run_id: str, credential_ref: str,
              models: Iterable[str] = (), ttl: int = HANDLE_TTL_SECONDS) -> tuple[str, str]:
        """(handle, base_url) from the gateway's `/_boss/handles`."""
        import httpx

        from ..identity import tokens

        key = tokens.egress_service_key()
        if not key:
            raise RuntimeError("no service key for the model gateway")
        resp = httpx.post(gateway_url() + "/_boss/handles", headers={"X-Boss-Egress-Key": key},
                          json={"provider": provider, "project_id": project_id, "run_id": run_id,
                                "credential_ref": credential_ref, "models": list(models),
                                "ttl_seconds": int(ttl)}, timeout=10.0)
        if resp.status_code != 200:
            raise RuntimeError(f"the model gateway refused to issue a handle ({resp.status_code})")
        raw = str(resp.json()["handle"])
        _register_redaction(raw)
        return raw, f"{gateway_url()}/{provider}"


__all__ = ["ADAPTERS", "ANTHROPIC", "AZURE_OPENAI", "BEDROCK", "CREDENTIAL_HEADERS",
           "HOP_HEADERS", "KEY_ENV_PROVIDER", "MODEL_HOSTS", "AuditQueue", "Caller",
           "GatewayClient", "GovernedBudget", "Handle", "KeyResolver", "KeyUnavailable",
           "ModelGateway", "OPENAI", "OPENROUTER", "PlaceholderRefused", "ProviderAdapter",
           "Refused", "TOKEN_PREFIX", "UsageReader", "as_ledger_usage", "credential_ref_for",
           "current_epoch", "dropped_header", "fingerprint", "gateway_url", "guest_handles",
           "hash_handle", "httpx_upstream", "is_ours", "issue_handle", "load_handle",
           "outbound_headers", "path_template", "presented_handle", "refusal_body",
           "revoke_handle", "revoke_run", "sigv4_headers", "upstream_for"]
