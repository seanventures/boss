"""The ticket-system contract, v2 (BOSSAPP-1047, Phase A).

One normalised shape for a ticket wherever it lives (Jira, Plane, GitHub Issues, ...), one
adapter Protocol every connector implements, and one family of typed errors. Phase B (API
routes, console, agent tools) is written against THIS module and never against a provider.

Rules the contract carries:
  * Agents never see a token. A :class:`TicketConnection` resolves its credential server-side,
    at the moment the shared HTTP helper attaches it, and never prints it (``repr`` hides it).
  * A capability an adapter does not have raises :class:`CapabilityNotSupported`; nothing is
    faked or silently skipped.
  * Text written outside BOSS (title, body, comments) is screened for prompt injection before a
    :class:`Ticket` leaves the adapter layer; the ticket carries the verdict (``screen``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from ...services.work_tracker_adapters import (
    IssueCreateResult,
    WorkTrackerAdapter,
    WorkTrackerHttpError,
)

# ---------------------------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------------------------

TICKET_TYPES = ("bug", "story", "task", "epic", "other")
STATUS_CATEGORIES = ("todo", "doing", "review", "done")

#: Capability flag -> the adapter hooks that flag promises. The registry refuses a connector
#: whose manifest claims a flag its adapter does not implement, or implements one it does not
#: claim (the manifest is the truth the console shows).
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "read": ("fetch_ticket", "ref_from_url"),
    "browse": ("search_tickets", "fetch_projects", "fetch_epics", "fetch_children",
               "fetch_unparented"),
    "write_comment": ("post_comment",),
    "transition": ("apply_transition",),
    "link": ("add_link",),
    "create": ("create_ticket",),
    "webhook": ("check_webhook", "read_webhook"),
}


# ---------------------------------------------------------------------------------------------
# Errors — every one is a WorkTrackerHttpError, so existing callers that catch that still do.
# ---------------------------------------------------------------------------------------------


class TicketSystemError(WorkTrackerHttpError):
    """A ticket-system call failed. The message never contains a credential."""

    def __init__(self, message: str, *, status: Optional[int] = None, provider: str = ""):
        super().__init__(message, status=status)
        self.provider = provider


class AuthFailed(TicketSystemError):
    """401/403: the credential is missing, wrong, expired or lacks permission.

    ``reason``: ``rejected`` (the provider said no), ``no_credential`` (nothing configured),
    or ``needs_person_sign_in`` (this call must act as a person who has not signed in, or whose
    sign-in expired, and no bot token may stand in)."""

    def __init__(self, message: str, *, reason: str = "rejected", **kw: Any):
        super().__init__(message, **kw)
        self.reason = reason


class NotFound(TicketSystemError):
    """404: the ticket/project does not exist, or the credential cannot see it."""


class RateLimited(TicketSystemError):
    """429 after every retry. ``retry_after`` is the provider's last advice, in seconds."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class HostNotAllowed(TicketSystemError):
    """The request would leave for a host that is neither the connector's official host nor
    the base URL configured for this connection (or by the operator)."""


class CapabilityNotSupported(TicketSystemError):
    """This connector does not do that (its manifest says so)."""

    def __init__(self, provider: str, capability: str, operation: str = ""):
        what = f"{operation} " if operation else ""
        super().__init__(f"{provider} does not support {what}({capability})".replace("  ", " "),
                         provider=provider)
        self.capability = capability
        self.operation = operation


class InvalidTicketRef(TicketSystemError):
    """The reference does not name a ticket this connector owns."""


class ConnectorManifestError(ValueError):
    """A connector folder's manifest is invalid or disagrees with its adapter."""


# ---------------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TicketRef:
    """Names one ticket: a key (``KAN-12``), a provider id, and/or its URL."""

    provider: str
    key: Optional[str] = None
    url: Optional[str] = None
    id: Optional[str] = None
    project: Optional[str] = None

    def label(self) -> str:
        return self.key or self.id or self.url or "?"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class TicketComment:
    author: Optional[str]
    at: Optional[str]
    body: str


@dataclass(frozen=True)
class TicketAttachment:
    """Name, URL and size only — BOSS never downloads an attachment by reading a ticket."""

    name: str
    url: Optional[str] = None
    size: Optional[int] = None


@dataclass
class Ticket:
    provider: str
    key: str
    id: str
    url: Optional[str]
    title: str
    body_markdown: str = ""
    type: str = "other"
    raw_type: Optional[str] = None
    status: Optional[str] = None
    status_category: str = "todo"
    priority: Optional[str] = None
    labels: list[str] = field(default_factory=list)
    assignee: Optional[str] = None
    reporter: Optional[str] = None
    acceptance_criteria: list[str] = field(default_factory=list)
    comments: list[TicketComment] = field(default_factory=list)
    parent_key: Optional[str] = None
    children_count: Optional[int] = None
    attachments: list[TicketAttachment] = field(default_factory=list)
    updated_at: Optional[str] = None
    #: The prompt-injection screen's verdict over title/body/comments:
    #: {"mode", "verdict": clean|suspicious|unscreened, "rules": [...]}.
    screen: dict[str, Any] = field(default_factory=dict)

    @property
    def untrusted(self) -> bool:
        return self.screen.get("verdict") == "suspicious"

    @property
    def ref(self) -> TicketRef:
        return TicketRef(self.provider, key=self.key, url=self.url, id=self.id)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["untrusted"] = self.untrusted
        return out


@dataclass(frozen=True)
class TicketProject:
    key: str
    name: str
    id: str
    url: Optional[str] = None
    open_count: Optional[int] = None
    epic_count: Optional[int] = None


@dataclass(frozen=True)
class TicketEvent:
    """One webhook delivery, normalised. ``kind``: created|updated|deleted|commented|other."""

    provider: str
    kind: str
    ref: Optional[TicketRef]
    actor: Optional[str] = None
    changed: tuple[str, ...] = ()
    raw_event: str = ""


@dataclass(frozen=True)
class TransitionResult:
    ref: TicketRef
    from_status: Optional[str]
    to_status: Optional[str]
    to_category: str
    changed: bool


@dataclass(frozen=True)
class LinkResult:
    ref: TicketRef
    url: str
    created: bool


@dataclass(frozen=True)
class ActingCredential:
    """The credential ONE call carries, and whose it is. ``token`` never prints.

    kind: ``api_token`` (the connection's bot/API key) | ``oauth2`` (a person's own sign-in).
    subject_kind/subject_id: ``user``/usr_... for a person, ``org``/<connection> for the bot."""

    kind: str
    subject_kind: str
    subject_id: str
    token: str = field(repr=False)
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {"auth_kind": self.kind, "subject_kind": self.subject_kind,
                "subject_id": self.subject_id}


# ---------------------------------------------------------------------------------------------
# A connection: configuration + a credential resolved only when a request is sent
# ---------------------------------------------------------------------------------------------

#: Configuration keys that can hold a token. Stripped from anything printed.
TOKEN_KEYS = ("api_token", "access_token", "token", "pat", "password", "oauth_client_secret",
              "refresh_token")

#: Per-connection behaviour, stored in ``work_tracker_providers.configuration["settings"]``
#: (non-secret JSON beside the connection it configures; see docs/infra/TICKET_CONNECTORS.md).
SETTINGS_DEFAULTS: dict[str, Any] = {
    "status_mapping": {},          # category -> provider status name; manifest fills defaults
    "pickup_label": "boss",        # tickets carrying this label are offered to BOSS
    "auto_start": False,           # False = "ask me first"
    "poll_interval_s": 300,
    "acceptance_criteria_field": "",   # e.g. Jira customfield_10035
    "epic_label": "epic",          # Plane: a label that marks an epic
    "project_keys": [],            # known project keys (ref parsing); refreshed from list_projects
    "comments_limit": 10,
}

#: Whose credential a call carries (connection_credentials.POLICIES, minus per_agent):
#:   shared             the connection's bot/API token only
#:   per_user           the acting person's own sign-in only
#:   per_user_fallback  the person's own when they have one, else the bot token
CREDENTIAL_POLICIES = ("shared", "per_user", "per_user_fallback")
MIN_POLL_INTERVAL_S = 60


def connection_settings(configuration: Optional[dict[str, Any]],
                        status_defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The effective settings of one connection: defaults <- manifest status defaults <- saved.
    Wrong types fall back to the default (a typo never turns ``auto_start`` on)."""
    saved = (configuration or {}).get("settings")
    saved = saved if isinstance(saved, dict) else {}
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in SETTINGS_DEFAULTS.items()}
    # The connection's own mapping first (it wins when two categories name one status), then
    # the manifest's defaults for categories it does not map.
    mapping: dict[str, str] = {}
    raw_map = saved.get("status_mapping")
    if isinstance(raw_map, dict):
        mapping.update({k: str(v).strip() for k, v in raw_map.items()
                        if k in STATUS_CATEGORIES and isinstance(v, str) and v.strip()})
    for k, v in (status_defaults or {}).items():
        if k in STATUS_CATEGORIES and isinstance(v, str) and v:
            mapping.setdefault(k, v)
    out["status_mapping"] = mapping
    if isinstance(saved.get("pickup_label"), str):
        out["pickup_label"] = saved["pickup_label"].strip()
    if saved.get("auto_start") is True:
        out["auto_start"] = True
    poll = saved.get("poll_interval_s")
    if isinstance(poll, int) and not isinstance(poll, bool):
        out["poll_interval_s"] = max(MIN_POLL_INTERVAL_S, poll)
    for name in ("acceptance_criteria_field", "epic_label"):
        if isinstance(saved.get(name), str):
            out[name] = saved[name].strip()
    keys = saved.get("project_keys")
    if isinstance(keys, list):
        out["project_keys"] = [str(k).strip().upper() for k in keys if str(k).strip()]
    limit = saved.get("comments_limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and 0 <= limit <= 100:
        out["comments_limit"] = limit
    return out


class TicketConnection:
    """One configured ticket system, as adapters use it.

    ``configuration`` is the row's JSON (base URL, workspace, ``secret_ref_id`` ...). The token
    is resolved by :meth:`credential` — from the secret store when a DB connection is given, else
    by ``work_tracker_adapters.resolve_credential`` — only inside the shared HTTP helper."""

    def __init__(self, provider: str, configuration: Optional[dict[str, Any]] = None, *,
                 project_id: Optional[str] = None, db: Any = None, app_settings: Any = None,
                 credential_resolver: Optional[Callable[[], Optional[str]]] = None,
                 person_id: Optional[str] = None, principal: Any = None):
        self.provider = (provider or "").strip().lower()
        self.configuration = dict(configuration or {})
        self.project_id = project_id
        self.db = db
        self.app_settings = app_settings
        self._resolver = credential_resolver
        #: Per-connection lookups an adapter may reuse within one piece of work (states,
        #: project ids). Never holds a credential.
        self.cache: dict[str, Any] = {}
        #: The person this work acts for (usr_...), when known. With ``principal`` (a
        #: connection_credentials.CredentialPrincipal) it decides whose credential a call carries.
        self.person_id = person_id
        self.principal = principal
        #: Every call's identity: {"method", "path", "auth_kind", "subject_kind", "subject_id"}.
        self.attribution: list[dict[str, Any]] = []

    @property
    def connection_id(self) -> str:
        """The id per-person credentials are kept under (connection_credentials.connection_id;
        the vault provider is the same string)."""
        return f"kit:tickets:{self.provider}"

    def __repr__(self) -> str:  # never the token
        shown = {k: v for k, v in self.configuration.items() if k not in TOKEN_KEYS}
        return f"TicketConnection({self.provider!r}, project_id={self.project_id!r}, {shown!r})"

    __str__ = __repr__

    def resolved_configuration(self) -> dict[str, Any]:
        """The configuration with its token, for ONE call (the legacy create/test paths)."""
        config = dict(self.configuration)
        if self.db is not None and self.project_id and not config.get("api_token"):
            from ...services.work_orchestration import with_credential

            config = with_credential(self.db, self.project_id, config)
        return config

    def credential(self) -> Optional[str]:
        if self._resolver is not None:
            return self._resolver()
        from ...services.work_tracker_adapters import resolve_credential

        return resolve_credential(self.resolved_configuration(), provider=self.provider)

    def get(self, key: str, default: Any = "") -> Any:
        value = self.configuration.get(key)
        return default if value in (None, "") else value

    def settings(self, status_defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return connection_settings(self.configuration, status_defaults)


# ---------------------------------------------------------------------------------------------
# The adapter Protocol
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class TicketSystemAdapter(WorkTrackerAdapter, Protocol):
    """What every ticket connector offers (the legacy create/test surface plus v2).

    ``ticket_capabilities`` is filled from the manifest by the registry. The public methods
    below are implemented ONCE in :class:`~.base.TicketAdapterBase` (capability check, screening);
    a connector implements the hooks listed in :data:`CAPABILITIES`."""

    ticket_capabilities: dict[str, bool]

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]: ...

    def parse_ref(self, text: str, *, conn: Optional[TicketConnection] = None,
                  known_keys: Optional[set[str]] = None) -> list[TicketRef]: ...

    def get_ticket(self, conn: TicketConnection, ref: TicketRef) -> Ticket: ...

    def search(self, conn: TicketConnection, query: str = "", project: Optional[str] = None,
               filters: Optional[dict[str, Any]] = None, *, limit: int = 50) -> list[Ticket]: ...

    def list_projects(self, conn: TicketConnection, query: Optional[str] = None, *,
                      counts: bool = False) -> list[TicketProject]: ...

    def list_epics(self, conn: TicketConnection, project: str) -> list[Ticket]: ...

    def list_children(self, conn: TicketConnection, epic_ref: TicketRef) -> list[Ticket]: ...

    def list_unparented(self, conn: TicketConnection, project: str) -> list[Ticket]: ...

    def comment(self, conn: TicketConnection, ref: TicketRef, markdown: str) -> TicketComment: ...

    def transition(self, conn: TicketConnection, ref: TicketRef,
                   to_category: str) -> TransitionResult: ...

    def link(self, conn: TicketConnection, ref: TicketRef, url: str, title: str) -> LinkResult: ...

    def create(self, conn: TicketConnection, *, title: str, body_markdown: str = "",
               type: str = "task", labels: Optional[list[str]] = None,
               project: Optional[str] = None,
               parent: Optional[TicketRef] = None) -> IssueCreateResult: ...

    def verify_webhook(self, headers: dict[str, str], body: bytes, secret: str) -> bool: ...

    def parse_webhook(self, body: Any) -> TicketEvent: ...
