"""Event intake adapters (P9a): one folder per source, one contract, one registry.

    from boss_engine.connectors.intake import REGISTRY, IntakeEvent, get_intake_adapter
"""
from __future__ import annotations

from .base import IntakeAdapterBase, implements, screen_event
from .contract import (
    CAPABILITIES,
    EVENT_STATUSES,
    MODES,
    AuthFailed,
    CapabilityNotSupported,
    ConnectorManifestError,
    Cursor,
    CursorExpired,
    ExternalActor,
    Health,
    HostNotAllowed,
    IntakeAdapter,
    IntakeConnection,
    IntakeError,
    IntakeEvent,
    OutboxOp,
    ParseRefused,
    PersonHint,
    PollResult,
    RateLimited,
    RawRequest,
    RawResponse,
    Receipt,
    SecretView,
    StoredRaw,
    Verified,
    VerifyRefused,
)
from .registry import REGISTRY, Manifest, get_intake_adapter

__all__ = [
    "CAPABILITIES", "EVENT_STATUSES", "MODES", "REGISTRY", "AuthFailed", "CapabilityNotSupported",
    "ConnectorManifestError", "Cursor", "CursorExpired", "ExternalActor", "Health",
    "HostNotAllowed", "IntakeAdapter", "IntakeAdapterBase", "IntakeConnection", "IntakeError",
    "IntakeEvent", "Manifest", "OutboxOp", "ParseRefused", "PersonHint", "PollResult",
    "RateLimited", "RawRequest", "RawResponse", "Receipt", "SecretView", "StoredRaw", "Verified",
    "VerifyRefused", "get_intake_adapter", "implements", "screen_event",
]
