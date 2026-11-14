"""Ticket-system connectors (BOSSAPP-1047). See docs/infra/TICKET_CONNECTORS.md.

Phase B entry points:
    get_ticket_adapter(provider) -> adapter with the TicketSystemAdapter contract
    connection_for(db, project_id, provider="", person_id=...) -> TicketConnection | None
    parse_refs(text, connections) -> [TicketRef]
    begin_oauth / complete_oauth / disconnect_person   (a person's own sign-in)
"""

from .auth import begin_oauth, complete_oauth, disconnect_person, select_credential
from .contract import (
    CAPABILITIES,
    STATUS_CATEGORIES,
    TICKET_TYPES,
    ActingCredential,
    AuthFailed,
    CapabilityNotSupported,
    ConnectorManifestError,
    HostNotAllowed,
    InvalidTicketRef,
    LinkResult,
    NotFound,
    RateLimited,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketConnection,
    TicketEvent,
    TicketProject,
    TicketRef,
    TicketSystemAdapter,
    TicketSystemError,
    TransitionResult,
    connection_settings,
)
from .registry import REGISTRY, get_ticket_adapter
from .service import connection_for, connections_for, parse_refs

__all__ = [
    "ActingCredential", "AuthFailed", "CAPABILITIES", "CapabilityNotSupported",
    "ConnectorManifestError", "HostNotAllowed", "InvalidTicketRef", "LinkResult", "NotFound",
    "REGISTRY", "RateLimited", "STATUS_CATEGORIES", "TICKET_TYPES", "Ticket",
    "TicketAttachment", "TicketComment", "TicketConnection", "TicketEvent", "TicketProject",
    "TicketRef", "TicketSystemAdapter", "TicketSystemError", "TransitionResult",
    "begin_oauth", "complete_oauth", "connection_for", "connections_for", "connection_settings",
    "disconnect_person", "get_ticket_adapter", "parse_refs", "select_credential",
]
