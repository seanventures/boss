"""The event-intake contract (P9a, BOSSAPP-1093; boss-eventing-lld.md §3).

One shape for anything that happens in another system (a Slack message, a GitHub push, a signed
webhook) once it has crossed BOSS's front door: a CloudEvents-1.0-shaped :class:`IntakeEvent`
with the ``boss*`` extensions; one adapter Protocol every source implements; one family of typed
errors. The intake route, the processor and the router are written against THIS module and never
against a source.

Rules the contract carries:
  * Verify on the raw bytes before anything else is read. A refusal is a :class:`VerifyRefused`
    with a one-word reason; the route answers it, nothing is stored.
  * The front door does verify + one insert + ack, and nothing more. Hooks in the PUSH group are
    pure CPU (secret resolution aside); the PROCESSOR group may do I/O.
  * A capability an adapter does not have raises :class:`CapabilityNotSupported`; nothing is
    faked. The registry refuses a manifest that claims a hook its adapter lacks, or the reverse.
  * Secrets are resolved by the caller at the moment of use and never printed: ``IntakeConnection``
    hides them behind a resolver and its ``repr`` shows none.
  * Attachments are never downloaded at intake (the tickets rule).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

#: How a source reaches BOSS. `stream` is contract-only in P9 (Kafka/SQS/Pub/Sub later).
MODES = ("push", "poll", "subscribe", "socket", "stream")

#: Capability flag -> the adapter hooks the flag promises. The manifest is the truth the console
#: shows; the registry holds it equal to what the class implements (both directions).
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "verify": ("verify", "dedupe_key"),
    "handshake": ("handshake",),
    "parse": ("parse",),
    "poll": ("poll",),
    "subscribe": ("subscribe", "renew", "unsubscribe"),
    "stream": ("open_stream", "consume", "commit"),
    "reply": ("send",),
    "stream_reply": ("send_stream",),
    "identity": ("resolve_person",),
    "conversation": ("conversation_key",),
    "health": ("health",),
}

#: Envelope fields a manifest's `dedupe` list may name (in order; the adapter's `dedupe_key`
#: hook is what actually computes the key — the list documents it for the console).
DEDUPE_FIELDS = ("id", "source", "subject", "time", "delivery_id", "body_sha256")

#: The statuses an `intake_events` row moves through (db/schema.sql, P9a).
EVENT_STATUSES = ("received", "processing", "routed", "ignored", "filtered", "refused",
                  "failed", "dead", "shed", "digest")
TERMINAL_STATUSES = ("routed", "ignored", "filtered", "refused", "dead", "shed", "digest")

#: Outcomes the front door answers (§3.5).
ACK_RECEIVED = "received"
ACK_DUPLICATE = "duplicate"
ACK_PAUSED = "paused"
ACK_SHED = "shed"
ACK_REFUSED = "refused"
ACK_HANDSHAKE = "handshake"
ACK_UNAVAILABLE = "unavailable"
ACK_NOT_FOUND = "not_found"
ACK_TOO_LARGE = "too_large"

#: Actor kinds on the envelope.
ACTOR_KINDS = ("user", "bot", "service", "unknown")

#: `data` is bounded; a larger raw body is kept on the row (≤ MAX_RAW_BYTES) and hashed.
MAX_DATA_BYTES = 64 * 1024
MAX_RAW_BYTES = 256 * 1024
#: The front door reads at most this much (413 beyond). Slack/Teams/GitHub bodies are small.
MAX_BODY_BYTES = 1024 * 1024
#: Signed timestamps older than this (either way) are stale.
TIMESTAMP_TOLERANCE_SECONDS = 300
#: Extension attribute names (CloudEvents extension attributes are lowercase, no separators).
EXTENSIONS = ("bossconnection", "bossconversation", "bossactor", "bosscausality", "bossretry",
              "bossscreen", "bossdedupe")


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------


class IntakeError(RuntimeError):
    """An intake call failed. The message never contains a credential."""

    def __init__(self, message: str, *, provider: str = ""):
        super().__init__(message)
        self.provider = provider


class VerifyRefused(IntakeError):
    """The request did not prove it came from the source. `reason` is ONE word for the log and
    the refusal counter: missing_signature | bad_signature | stale_timestamp | bad_timestamp |
    not_configured | unsupported_scheme."""

    def __init__(self, reason: str, *, provider: str = ""):
        super().__init__(reason, provider=provider)
        self.reason = reason


class AuthFailed(IntakeError):
    """The source refused OUR credential (poll/subscribe/reply/enrich)."""


class RateLimited(IntakeError):
    """The source said slow down. `retry_after` is its advice in seconds."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class HostNotAllowed(IntakeError):
    """A call would leave for a host that is not the source's official host."""


class CursorExpired(IntakeError):
    """The source no longer holds history at our cursor (Gmail historyId too old, Salesforce
    replay id past retention, Graph delta token expired): the poller must resync."""


class ParseRefused(IntakeError):
    """The verified body is not something this adapter can turn into events (permanent: the
    event goes dead at once, never retried)."""


class CapabilityNotSupported(IntakeError):
    """This adapter does not do that (its manifest says so)."""

    def __init__(self, provider: str, capability: str, operation: str = ""):
        what = f"{operation} " if operation else ""
        super().__init__(f"{provider} does not support {what}({capability})".replace("  ", " "),
                         provider=provider)
        self.capability = capability
        self.operation = operation


class ConnectorManifestError(ValueError):
    """An intake adapter folder's manifest is invalid or disagrees with its adapter."""


# ---------------------------------------------------------------------------------------------
# What crosses the front door
# ---------------------------------------------------------------------------------------------


def lower_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


#: Headers an `intake_events.headers` column may keep. Never a signature or an Authorization.
KEPT_HEADERS = frozenset({
    "content-type", "user-agent", "webhook-id", "webhook-timestamp", "x-boss-delivery",
    "x-boss-timestamp", "x-github-delivery", "x-github-event", "x-github-hook-id",
    "x-github-hook-installation-target-id", "x-github-hook-installation-target-type",
    "x-slack-retry-num", "x-slack-retry-reason", "x-slack-request-timestamp",
    "x-request-id", "traceparent", "ce-id", "ce-type", "ce-source", "ce-specversion",
})


def kept_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """The allow-listed headers of a request (signatures and credentials never reach a row)."""
    return {k: v[:500] for k, v in lower_headers(headers).items() if k in KEPT_HEADERS}


@dataclass(frozen=True)
class RawRequest:
    """One inbound HTTP request, as the front door hands it to an adapter."""

    method: str
    headers: Mapping[str, str]
    body: bytes = field(repr=False)
    query: Mapping[str, str] = field(default_factory=dict)
    path: str = ""

    def header(self, name: str, default: str = "") -> str:
        return str(self.headers.get(name.lower(), default))

    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True)
class Verified:
    """How a request proved itself: the scheme name and (for the audit) nothing secret."""

    scheme: str
    timestamp: Optional[int] = None
    retry: Optional[dict[str, Any]] = None     # {num, reason} from X-Slack-Retry-* and the like


@dataclass(frozen=True)
class RawResponse:
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    media_type: str = "application/json"


@dataclass(frozen=True)
class StoredRaw:
    """The verified body and allow-listed headers as the processor reads them from the row."""

    body: bytes = field(repr=False)
    headers: Mapping[str, str]
    dedupe_key: str
    received_at: Any = None
    event_row_id: str = ""


# ---------------------------------------------------------------------------------------------
# The envelope (CloudEvents 1.0 + boss* extensions)
# ---------------------------------------------------------------------------------------------


@dataclass
class ExternalActor:
    kind: str = "unknown"                 # user | bot | service | unknown
    external_id: str = ""
    display: str = ""
    email: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")} | {"kind": self.kind}


@dataclass
class IntakeEvent:
    """One normalised event. Field names follow CloudEvents 1.0 so a later stream source can
    carry CloudEvents natively and an outbound bridge can emit them (§3.4)."""

    id: str
    source: str
    type: str
    subject: Optional[str] = None
    time: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)
    datacontenttype: str = "application/json"
    specversion: str = "1.0"
    # extensions
    bossconnection: str = ""
    bossconversation: Optional[dict[str, Any]] = None
    bossactor: ExternalActor = field(default_factory=ExternalActor)
    bosscausality: Optional[dict[str, Any]] = None     # {run_id, deployment_id, hops}
    bossretry: Optional[dict[str, Any]] = None         # {num, reason}
    bossscreen: Optional[dict[str, Any]] = None        # {mode, verdict, rules}
    bossdedupe: str = ""

    @property
    def untrusted(self) -> bool:
        return bool(self.bossscreen) and self.bossscreen.get("verdict") == "suspicious"

    def text_fields(self) -> dict[str, Any]:
        """The strings an outsider wrote (what the injection screen reads)."""
        out: dict[str, Any] = {}
        for key in ("text", "title", "body", "subject", "comment", "description", "message"):
            value = self.data.get(key)
            if isinstance(value, str) and value:
                out[key] = value
        blocks = self.data.get("text_blocks")
        if isinstance(blocks, list):
            out["text_blocks"] = [b for b in blocks if isinstance(b, str)]
        fields = self.data.get("fields")
        if isinstance(fields, dict):
            out["fields"] = {k: v for k, v in fields.items() if isinstance(v, str)}
        if self.subject:
            out["event_subject"] = self.subject
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "specversion": self.specversion, "id": self.id, "source": self.source,
            "type": self.type, "datacontenttype": self.datacontenttype, "data": self.data,
        }
        if self.subject is not None:
            out["subject"] = self.subject
        if self.time is not None:
            out["time"] = self.time
        out["bossconnection"] = self.bossconnection
        out["bossactor"] = self.bossactor.to_dict()
        for name in ("bossconversation", "bosscausality", "bossretry", "bossscreen"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        out["bossdedupe"] = self.bossdedupe
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IntakeEvent":
        actor = raw.get("bossactor") or {}
        return cls(
            id=str(raw.get("id") or ""), source=str(raw.get("source") or ""),
            type=str(raw.get("type") or ""), subject=raw.get("subject"), time=raw.get("time"),
            data=dict(raw.get("data") or {}),
            datacontenttype=str(raw.get("datacontenttype") or "application/json"),
            specversion=str(raw.get("specversion") or "1.0"),
            bossconnection=str(raw.get("bossconnection") or ""),
            bossconversation=raw.get("bossconversation"),
            bossactor=ExternalActor(kind=str(actor.get("kind") or "unknown"),
                                    external_id=str(actor.get("external_id") or ""),
                                    display=str(actor.get("display") or ""),
                                    email=actor.get("email")),
            bosscausality=raw.get("bosscausality"), bossretry=raw.get("bossretry"),
            bossscreen=raw.get("bossscreen"), bossdedupe=str(raw.get("bossdedupe") or ""))


# ---------------------------------------------------------------------------------------------
# Pull / subscribe / stream / reply / identity shapes
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonHint:
    """What an adapter can say about who acted: an email or IdP subject BOSS may match (§6).
    Never an authority by itself."""

    email: Optional[str] = None
    idp_subject: Optional[str] = None
    display: str = ""
    is_guest: bool = False
    is_bot: bool = False


@dataclass(frozen=True)
class Cursor:
    stream_key: str
    value: Optional[str] = None
    cursor_time: Optional[str] = None


@dataclass(frozen=True)
class PollResult:
    """What one poll found. `deliveries` are the source-shaped bodies the events were parsed
    from: the poller stores THOSE in the inbox, so a polled change is processed, replayed and
    deduplicated exactly as a pushed one is (GitHub, BOSSAPP-1175)."""

    events: tuple[IntakeEvent, ...]
    next_cursor: Cursor
    more: bool = False
    deliveries: tuple["StoredRaw", ...] = ()


@dataclass(frozen=True)
class SubscriptionSpec:
    resource: str
    client_state: str = field(repr=False, default="")
    notification_url: str = ""
    lifetime_seconds: int = 0


@dataclass(frozen=True)
class Subscription:
    provider_subscription_id: str
    resource: str
    expires_at: Optional[str] = None


@dataclass
class StreamHandle:
    stream_key: str
    position: Cursor
    native: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class StreamRecord:
    event: IntakeEvent
    position: Cursor


@dataclass(frozen=True)
class OutboxOp:
    op: str                                 # post | update | status | react | dm | ephemeral
    conversation: dict[str, Any]
    message: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""


@dataclass(frozen=True)
class Receipt:
    ok: bool
    ref: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str = ""
    checks: dict[str, Any] = field(default_factory=dict)


class SecretView:
    """A read-once view over a connection's secrets, by field name. Never printed."""

    def __init__(self, resolver: Callable[[str], Optional[str]]):
        self._resolver = resolver

    def get(self, name: str) -> Optional[str]:
        return self._resolver(name)

    def __repr__(self) -> str:
        return "SecretView(<hidden>)"

    __str__ = __repr__


# ---------------------------------------------------------------------------------------------
# A connection: configuration + secrets resolved only when asked
# ---------------------------------------------------------------------------------------------


class IntakeConnection:
    """One connected source install, as adapters use it. ``config`` is the row's non-secret JSON
    (team id, instance URL, bot user id, ...); a secret is read through :meth:`secret` from the
    resolver the caller supplied (the secret backend in the API; a dict in tests)."""

    def __init__(self, id: str, project_id: str, kind: str, *, mode: str = "push",
                 config: Optional[Mapping[str, Any]] = None,
                 secret_resolver: Optional[Callable[[str], Optional[str]]] = None,
                 org_id: Optional[str] = None, name: str = "", status: str = "active",
                 limits: Optional[Mapping[str, Any]] = None, app_settings: Any = None,
                 db: Any = None):
        self.id = id
        self.project_id = project_id
        self.org_id = org_id
        self.kind = (kind or "").strip().lower()
        self.mode = mode
        self.name = name
        self.status = status
        self.config: dict[str, Any] = dict(config or {})
        self.limits: dict[str, Any] = dict(limits or {})
        self.app_settings = app_settings
        self.db = db
        self._secrets = SecretView(secret_resolver or (lambda _name: None))

    def __repr__(self) -> str:  # never a secret: secrets are not in config, and the view hides
        return (f"IntakeConnection({self.id!r}, kind={self.kind!r}, project_id="
                f"{self.project_id!r}, mode={self.mode!r})")

    __str__ = __repr__

    def get(self, key: str, default: Any = "") -> Any:
        value = self.config.get(key)
        return default if value in (None, "") else value

    def secret(self, name: str) -> Optional[str]:
        return self._secrets.get(name)

    @property
    def secrets(self) -> SecretView:
        return self._secrets

    @property
    def source_uri(self) -> str:
        """`<kind>://<tenant>`: the CloudEvents `source` for this connection."""
        tenant = str(self.get("tenant") or self.get("team_id") or self.get("instance")
                     or self.get("repository") or self.id)
        return f"{self.kind}://{tenant}"


# ---------------------------------------------------------------------------------------------
# The adapter Protocol
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class IntakeAdapter(Protocol):
    """What every intake adapter offers. The hooks a manifest's capabilities promise are listed
    in :data:`CAPABILITIES`; :class:`~.base.IntakeAdapterBase` refuses the rest."""

    key: str
    manifest: Any
    intake_capabilities: dict[str, bool]

    # PUSH (route thread; pure CPU, no I/O except secret resolution)
    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified: ...
    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]: ...
    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str: ...
    def ack(self, conn: IntakeConnection, req: RawRequest, outcome: str) -> RawResponse: ...

    # PROCESSOR (claimed row; may do I/O)
    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]: ...
    def enrich(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent: ...
    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint: ...
    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool: ...
    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]: ...

    # PULL
    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult: ...
    def subscribe(self, conn: IntakeConnection, spec: SubscriptionSpec) -> Subscription: ...
    def renew(self, conn: IntakeConnection, sub: Subscription) -> Subscription: ...
    def unsubscribe(self, conn: IntakeConnection, sub: Subscription) -> None: ...

    # STREAM (contract only in P9)
    def open_stream(self, conn: IntakeConnection, cursor: Cursor) -> StreamHandle: ...
    def consume(self, h: StreamHandle, max_events: int, max_wait_ms: int) -> list[StreamRecord]: ...
    def commit(self, h: StreamHandle, upto: Cursor) -> None: ...

    # REPLY (outbox sender only; never from a run)
    def send(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt: ...
    def send_stream(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt: ...

    def health(self, conn: IntakeConnection) -> Health: ...


__all__ = [
    "ACK_DUPLICATE", "ACK_HANDSHAKE", "ACK_NOT_FOUND", "ACK_PAUSED", "ACK_RECEIVED",
    "ACK_REFUSED", "ACK_SHED", "ACK_TOO_LARGE", "ACK_UNAVAILABLE", "ACTOR_KINDS", "AuthFailed",
    "CAPABILITIES", "CapabilityNotSupported", "ConnectorManifestError", "Cursor", "CursorExpired",
    "DEDUPE_FIELDS", "EVENT_STATUSES", "EXTENSIONS", "ExternalActor", "Health", "HostNotAllowed",
    "IntakeAdapter", "IntakeConnection", "IntakeError", "IntakeEvent", "KEPT_HEADERS",
    "MAX_BODY_BYTES", "MAX_DATA_BYTES", "MAX_RAW_BYTES", "MODES", "OutboxOp", "ParseRefused",
    "PersonHint", "PollResult", "RateLimited", "RawRequest", "RawResponse", "Receipt",
    "SecretView", "StoredRaw", "StreamHandle", "StreamRecord", "Subscription",
    "SubscriptionSpec", "TERMINAL_STATUSES", "TIMESTAMP_TOLERANCE_SECONDS", "Verified",
    "VerifyRefused", "kept_headers", "lower_headers",
]
