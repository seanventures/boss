"""The base every ticket connector extends.

The PUBLIC contract methods (get_ticket, search, comment, transition, ...) are implemented here,
once: each checks the manifest's capability flag, calls the connector's hook, and screens text
written outside BOSS before it is returned. A connector implements only the hooks
(fetch_ticket, search_tickets, post_comment, apply_transition, ...); a hook it leaves alone raises
:class:`CapabilityNotSupported`, and the registry refuses a manifest that claims it anyway.
"""

from __future__ import annotations

import ssl
from typing import Any, Iterable, Optional

from ...services.work_tracker_adapters import (
    ConnectionField,
    IssueCreateResult,
    TrackerCatalogEntry,
)
from .contract import (
    CAPABILITIES,
    STATUS_CATEGORIES,
    CapabilityNotSupported,
    InvalidTicketRef,
    LinkResult,
    Ticket,
    TicketComment,
    TicketConnection,
    TicketEvent,
    TicketProject,
    TicketRef,
    TransitionResult,
)
from .http import TicketHttp
from .refs import find_keys, find_urls


def _unsupported(capability: str, operation: str):
    def hook(self, *args: Any, **kwargs: Any) -> Any:
        raise CapabilityNotSupported(self.provider_key, capability, operation)

    hook.__ticket_unsupported__ = True  # type: ignore[attr-defined]
    hook.__name__ = operation
    return hook


def implements(adapter_cls: type, hook: str) -> bool:
    """Whether a connector class overrides ``hook`` (rather than inheriting the refusal)."""
    function = getattr(adapter_cls, hook, None)
    return function is not None and not getattr(function, "__ticket_unsupported__", False)


_TYPE_WORDS = {
    "bug": "bug", "defect": "bug", "incident": "bug", "problem": "bug",
    "story": "story", "user story": "story", "feature": "story", "enhancement": "story",
    "task": "task", "sub-task": "task", "subtask": "task", "chore": "task",
    "epic": "epic", "initiative": "epic",
}


def normalise_type(raw: Optional[str]) -> str:
    """bug|story|task|epic|other from a provider's type (or label) name."""
    return _TYPE_WORDS.get(str(raw or "").strip().lower(), "other")


def screen_ticket(conn: TicketConnection, ticket: Ticket) -> Ticket:
    """Screen the text a person outside BOSS wrote (title, body, comments) for prompt injection
    and record the verdict on the ticket. The text itself is never rewritten."""
    from ...services.tool_result_screen import screen_result

    payload = {"title": ticket.title, "body": ticket.body_markdown,
               "acceptance_criteria": list(ticket.acceptance_criteria),
               "comments": [c.body for c in ticket.comments]}
    app_settings = conn.app_settings
    if app_settings is None and conn.db is not None:
        from ...config import get_settings

        app_settings = get_settings()
    _, verdict = screen_result(conn.db, app_settings, conn.project_id, payload,
                               ref=f"ticket:{ticket.provider}:{ticket.key or ticket.id}")
    ticket.screen = dict(verdict)
    return ticket


class TicketAdapterBase:
    """Mixin/base for ticket connectors. The registry sets ``manifest`` and
    ``ticket_capabilities`` from the connector's manifest.yaml."""

    provider_key: str = ""
    display_name: str = ""
    capabilities: tuple[str, ...] = ()
    manifest: Any = None
    ticket_capabilities: dict[str, bool] = {}

    # -- manifest-derived defaults ------------------------------------------------------------

    def catalog_entry(self) -> TrackerCatalogEntry:
        m = self.manifest
        return TrackerCatalogEntry(
            provider_key=m.key, display_name=m.label, description=m.description,
            docs_url=m.docs_url, capabilities=tuple(self.capabilities),
            connection_fields=tuple(
                ConnectionField(f["name"], f["label"], required=f.get("required", True),
                                secret=f.get("secret", False),
                                placeholder=f.get("placeholder", ""), help=f.get("help", ""))
                for f in m.auth_fields))

    def status_defaults(self) -> dict[str, Any]:
        return dict(getattr(self.manifest, "status_mapping_defaults", {}) or {})

    def settings(self, conn: TicketConnection) -> dict[str, Any]:
        return conn.settings(self.status_defaults())

    def category_for(self, conn: TicketConnection, status_name: Optional[str],
                     natural: str) -> str:
        """A status's category: the connection's mapping names it first (e.g. 'In Review' ->
        review), else the provider's own grouping."""
        name = str(status_name or "").strip().lower()
        for category, mapped in self.settings(conn).get("status_mapping", {}).items():
            if name and str(mapped).strip().lower() == name:
                return category
        return natural if natural in STATUS_CATEGORIES else "todo"

    def can(self, capability: str) -> bool:
        return bool(self.ticket_capabilities.get(capability))

    def _require(self, capability: str, operation: str) -> None:
        if not self.can(capability):
            raise CapabilityNotSupported(self.provider_key, capability, operation)

    # -- HTTP: one session per call site, all through the shared helper -----------------------

    def api_base(self, conn: TicketConnection) -> str:
        field = getattr(self.manifest, "base_url_field", "") or ""
        return str(conn.get(field) or (self.manifest.default_base_url if self.manifest else ""))

    def auth_headers(self, conn: TicketConnection, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def ssl_context(self, conn: TicketConnection) -> Optional[ssl.SSLContext]:
        return None

    def request_auth(self, conn: TicketConnection, credential: Any
                     ) -> tuple[dict[str, str], Optional[str]]:
        """Headers for one credential, and the API base it addresses (None = the connection's).
        An OAuth credential with a resource id uses the manifest's ``api_base_template``."""
        if credential.kind == "oauth2":
            from .auth import oauth_spec

            spec = oauth_spec(self) or {}
            template = spec.get("api_base_template")
            base = template.format(resource_id=credential.extra.get("resource_id", "")) \
                if template else None
            return {"Authorization": f"Bearer {credential.token}"}, base
        return self.auth_headers(conn, credential.token), None

    def session(self, conn: TicketConnection) -> TicketHttp:
        from .auth import select_credential

        m = self.manifest
        return TicketHttp(
            conn, base_url=self.api_base(conn),
            auth=lambda credential: self.request_auth(conn, credential),
            pick_credential=lambda: select_credential(conn, self),
            default_hosts=tuple(m.default_hosts), base_url_fields=tuple(m.base_url_fields),
            guard_provider=m.guard_provider, ssl_context=self.ssl_context(conn))

    # -- refs ---------------------------------------------------------------------------------

    def known_keys(self, conn: Optional[TicketConnection]) -> set[str]:
        if conn is None:
            return set()
        keys = set(self.settings(conn).get("project_keys") or [])
        for name in ("project_key", "project_identifier"):
            value = str(conn.get(name) or "").strip().upper()
            if value:
                keys.add(value)
        return keys

    def parse_ref(self, text: str, *, conn: Optional[TicketConnection] = None,
                  known_keys: Optional[Iterable[str]] = None) -> list[TicketRef]:
        """Every reference in ``text`` this connector owns: its URLs, and (when the system
        uses keys) keys whose project prefix is known for the connection."""
        if not self.can("read"):
            return []
        refs: list[TicketRef] = []
        seen: set[tuple[Any, ...]] = set()
        for url in find_urls(text):
            ref = self.ref_from_url(url, conn)
            if ref is not None:
                marker = (ref.key, ref.id, ref.project)
                if marker not in seen:
                    seen.add(marker)
                    refs.append(ref)
        if getattr(self.manifest, "key_refs", False):
            known = set(known_keys or ()) | self.known_keys(conn)
            for key, project in find_keys(text, known):
                marker = (key, None, None)
                if not any(r.key == key for r in refs) and marker not in seen:
                    seen.add(marker)
                    refs.append(TicketRef(self.provider_key, key=key, project=project))
        return refs

    def coerce_ref(self, ref: Any, conn: Optional[TicketConnection] = None) -> TicketRef:
        """A TicketRef from a ref, a key or a URL (what Phase B's routes hand in)."""
        if isinstance(ref, TicketRef):
            return ref
        text = str(ref or "").strip()
        if "://" in text:
            found = self.ref_from_url(text, conn)
            if found is None:
                raise InvalidTicketRef(f"{self.provider_key}: not a ticket URL of this system",
                                       provider=self.provider_key)
            return found
        if not text:
            raise InvalidTicketRef(f"{self.provider_key}: which ticket?",
                                   provider=self.provider_key)
        return TicketRef(self.provider_key, key=text)

    # -- the public contract (implemented once) -----------------------------------------------

    def get_ticket(self, conn: TicketConnection, ref: Any) -> Ticket:
        self._require("read", "get_ticket")
        return screen_ticket(conn, self.fetch_ticket(conn, self.coerce_ref(ref, conn)))

    def search(self, conn: TicketConnection, query: str = "", project: Optional[str] = None,
               filters: Optional[dict[str, Any]] = None, *, limit: int = 50) -> list[Ticket]:
        self._require("browse", "search")
        found = self.search_tickets(conn, query or "", project, dict(filters or {}),
                                    max(1, min(int(limit), 500)))
        return [screen_ticket(conn, t) for t in found]

    def list_projects(self, conn: TicketConnection, query: Optional[str] = None, *,
                      counts: bool = False) -> list[TicketProject]:
        self._require("browse", "list_projects")
        return list(self.fetch_projects(conn, query, counts))

    def list_epics(self, conn: TicketConnection, project: str) -> list[Ticket]:
        self._require("browse", "list_epics")
        return [screen_ticket(conn, t) for t in self.fetch_epics(conn, project)]

    def list_children(self, conn: TicketConnection, epic_ref: Any) -> list[Ticket]:
        self._require("browse", "list_children")
        return [screen_ticket(conn, t)
                for t in self.fetch_children(conn, self.coerce_ref(epic_ref, conn))]

    def list_unparented(self, conn: TicketConnection, project: str) -> list[Ticket]:
        self._require("browse", "list_unparented")
        return [screen_ticket(conn, t) for t in self.fetch_unparented(conn, project)]

    def comment(self, conn: TicketConnection, ref: Any, markdown: str) -> TicketComment:
        self._require("write_comment", "comment")
        if not str(markdown or "").strip():
            raise ValueError("an empty comment is not posted")
        return self.post_comment(conn, self.coerce_ref(ref, conn), str(markdown))

    def transition(self, conn: TicketConnection, ref: Any, to_category: str) -> TransitionResult:
        self._require("transition", "transition")
        category = str(to_category or "").strip().lower()
        if category not in STATUS_CATEGORIES:
            raise ValueError(f"to_category must be one of {', '.join(STATUS_CATEGORIES)}")
        return self.apply_transition(conn, self.coerce_ref(ref, conn), category)

    def link(self, conn: TicketConnection, ref: Any, url: str, title: str) -> LinkResult:
        self._require("link", "link")
        if not str(url or "").lower().startswith(("https://", "http://")):
            raise ValueError("a link is an http(s) URL")
        return self.add_link(conn, self.coerce_ref(ref, conn), str(url), str(title or url))

    def create(self, conn: TicketConnection, *, title: str, body_markdown: str = "",
               type: str = "task", labels: Optional[list[str]] = None,
               project: Optional[str] = None,
               parent: Optional[TicketRef] = None) -> IssueCreateResult:
        self._require("create", "create")
        return self.create_ticket(conn, title=title, body_markdown=body_markdown, type=type,
                                  labels=list(labels or []), project=project, parent=parent)

    def verify_webhook(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        self._require("webhook", "verify_webhook")
        if not secret:
            return False
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        try:
            return bool(self.check_webhook(lowered, body or b"", secret))
        except Exception:  # noqa: BLE001 — a malformed delivery is simply not verified
            return False

    def parse_webhook(self, body: Any) -> TicketEvent:
        self._require("webhook", "parse_webhook")
        return self.read_webhook(body)

    # -- default create: the existing create_issue, with the credential resolved server-side --

    def create_ticket(self, conn: TicketConnection, *, title: str, body_markdown: str,
                      type: str, labels: list[str], project: Optional[str],
                      parent: Optional[TicketRef]) -> IssueCreateResult:
        create_issue = getattr(self, "create_issue", None)
        if create_issue is None:
            raise CapabilityNotSupported(self.provider_key, "create", "create")
        from .text import markdown_to_html

        result = create_issue(conn.resolved_configuration(), title=title,
                              description_html=markdown_to_html(body_markdown),
                              description_text=body_markdown)
        if result is None:
            from .contract import TicketSystemError

            raise TicketSystemError(f"{self.provider_key}: the ticket was not created",
                                    provider=self.provider_key)
        return result

    # -- hooks a connector implements (each refuses until it does) ----------------------------

    ref_from_url = _unsupported("read", "ref_from_url")
    fetch_ticket = _unsupported("read", "fetch_ticket")
    search_tickets = _unsupported("browse", "search_tickets")
    fetch_projects = _unsupported("browse", "fetch_projects")
    fetch_epics = _unsupported("browse", "fetch_epics")
    fetch_children = _unsupported("browse", "fetch_children")
    fetch_unparented = _unsupported("browse", "fetch_unparented")
    post_comment = _unsupported("write_comment", "post_comment")
    apply_transition = _unsupported("transition", "apply_transition")
    add_link = _unsupported("link", "add_link")
    check_webhook = _unsupported("webhook", "check_webhook")
    read_webhook = _unsupported("webhook", "read_webhook")


#: Hook names the base refuses by default (used by the registry's two-way check).
HOOKS = tuple(sorted({hook for hooks in CAPABILITIES.values() for hook in hooks}))
