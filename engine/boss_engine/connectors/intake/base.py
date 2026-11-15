"""The base every intake adapter extends.

An adapter implements only the hooks its manifest claims (:data:`contract.CAPABILITIES`); every
hook it leaves alone raises :class:`CapabilityNotSupported`, and the registry refuses a manifest
that claims one anyway. The few things every source shares are here once: the success ack from
the manifest, a body-hash fallback for the dedupe key, "not ours" as the default self check, and
the screen of the text an outsider wrote.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from .contract import (
    ACK_RECEIVED,
    CAPABILITIES,
    CapabilityNotSupported,
    IntakeConnection,
    IntakeEvent,
    RawRequest,
    RawResponse,
)


def _unsupported(capability: str, operation: str):
    def hook(self, *args: Any, **kwargs: Any) -> Any:
        raise CapabilityNotSupported(self.key, capability, operation)

    hook.__intake_unsupported__ = True  # type: ignore[attr-defined]
    hook.__name__ = operation
    return hook


def implements(adapter_cls: type, hook: str) -> bool:
    """Whether an adapter class overrides ``hook`` (rather than inheriting the refusal)."""
    function = getattr(adapter_cls, hook, None)
    return function is not None and not getattr(function, "__intake_unsupported__", False)


def body_hash_key(req: RawRequest, prefix: str = "sha256") -> str:
    return f"{prefix}:{hashlib.sha256(req.body).hexdigest()}"


def screen_event(conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent:
    """Screen the text an outsider wrote for prompt injection and record the verdict on the
    envelope (``bossscreen``). The text itself is never rewritten; mode per project."""
    from ...services.tool_result_screen import screen_result

    payload = ev.text_fields()
    if not payload:
        ev.bossscreen = {"mode": "patterns", "verdict": "clean", "rules": []}
        return ev
    app_settings = conn.app_settings
    if app_settings is None and conn.db is not None:
        from ...config import get_settings

        app_settings = get_settings()
    _, verdict = screen_result(conn.db, app_settings, conn.project_id, payload,
                               ref=f"intake:{conn.kind}:{ev.id}")
    ev.bossscreen = dict(verdict)
    return ev


class IntakeAdapterBase:
    """Mixin/base for intake adapters. The registry sets ``manifest`` and
    ``intake_capabilities`` from the adapter's manifest.yaml."""

    key: str = ""
    display_name: str = ""
    manifest: Any = None
    intake_capabilities: dict[str, bool] = {}

    def can(self, capability: str) -> bool:
        return bool(self.intake_capabilities.get(capability))

    def _require(self, capability: str, operation: str) -> None:
        if not self.can(capability):
            raise CapabilityNotSupported(self.key, capability, operation)

    # -- defaults every source shares ----------------------------------------------------------

    def ack(self, conn: IntakeConnection, req: RawRequest, outcome: str) -> RawResponse:
        """The source's own success code (manifest `ack.success_status`) for every accepted
        outcome; the route maps refusals and outages itself (§3.5)."""
        status = int(((getattr(self.manifest, "ack", None) or {}).get("success_status")) or 202)
        return RawResponse(status=status, body={"status": outcome or ACK_RECEIVED})

    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]:
        return None

    def enrich(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent:
        return ev

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        """Loop prevention, layer 1: did our own bot write this? The default knows the
        connection's declared self identity (manifest `self_identity` -> config keys)."""
        ours = {str(conn.get(name)) for name in (getattr(self.manifest, "self_identity", ())
                                                  or ()) if conn.get(name)}
        actor = ev.bossactor
        return bool(ours) and (actor.external_id in ours or actor.display in ours)

    # -- hooks an adapter implements (each refuses until it does) ------------------------------

    verify = _unsupported("verify", "verify")
    dedupe_key = _unsupported("verify", "dedupe_key")
    parse = _unsupported("parse", "parse")
    poll = _unsupported("poll", "poll")
    subscribe = _unsupported("subscribe", "subscribe")
    renew = _unsupported("subscribe", "renew")
    unsubscribe = _unsupported("subscribe", "unsubscribe")
    open_stream = _unsupported("stream", "open_stream")
    consume = _unsupported("stream", "consume")
    commit = _unsupported("stream", "commit")
    send = _unsupported("reply", "send")
    send_stream = _unsupported("stream_reply", "send_stream")
    resolve_person = _unsupported("identity", "resolve_person")
    conversation_key = _unsupported("conversation", "conversation_key")
    health = _unsupported("health", "health")


# `handshake` has a soft default (None: no handshake) and is claimed by overriding it; the base's
# own definition does not count as an implementation.
IntakeAdapterBase.handshake.__intake_unsupported__ = True  # type: ignore[attr-defined]

#: Hook names the base refuses by default (the registry's two-way check).
HOOKS = tuple(sorted({hook for hooks in CAPABILITIES.values() for hook in hooks}))

__all__ = ["HOOKS", "IntakeAdapterBase", "body_hash_key", "implements", "screen_event"]
