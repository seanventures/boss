"""Plane on the intake layer (P9b-4, BOSSAPP-1103): delegates to PlaneTicketAdapter."""
from __future__ import annotations

import json
from typing import Any, Optional

from ...tickets.contract import TicketConnection, TicketRef
from ...tickets.plane.adapter import PlaneTicketAdapter
from ..base import IntakeAdapterBase
from ..contract import (
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    ParseRefused,
    PersonHint,
    RawRequest,
    StoredRaw,
    Verified,
    VerifyRefused,
    lower_headers,
)

_DELIVERY_HEADERS = (
    "x-plane-delivery",
    "x-boss-delivery",
    "webhook-id",
)


class PlaneIntakeAdapter(IntakeAdapterBase):
    key = "plane"

    def __init__(self) -> None:
        self._tickets = PlaneTicketAdapter()

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        secret = conn.secret("webhook_secret") or ""
        if not secret:
            raise VerifyRefused("not_configured", provider=self.key)
        headers = lower_headers(req.headers)
        if not headers.get("x-plane-signature", "").strip():
            raise VerifyRefused("missing_signature", provider=self.key)
        if not self._tickets.check_webhook(headers, req.body, secret):
            raise VerifyRefused("bad_signature", provider=self.key)
        return Verified(scheme="plane_sha256")

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        for header in _DELIVERY_HEADERS:
            value = req.header(header).strip()[:200]
            if value:
                return f"plane:{value}"
        return f"plane:sha256:{req.body_sha256()}"

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            payload = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as error:
            raise ParseRefused(
                f"Plane body is not JSON: {type(error).__name__}", provider=self.key
            ) from None
        if not isinstance(payload, dict):
            raise ParseRefused("Plane body is not an object", provider=self.key)
        try:
            te = self._tickets.read_webhook(payload)
        except Exception as error:  # noqa: BLE001
            raise ParseRefused(
                f"Plane body unreadable: {type(error).__name__}", provider=self.key
            ) from None

        inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        title = str(inner.get("name") or "")
        desc = inner.get("description_stripped") or inner.get("description_html") or ""
        comment = inner.get("comment_stripped") or inner.get("comment_html") or ""
        subject = (te.ref.key or te.ref.id) if te.ref else None
        event_type = f"so.plane.issue.{te.kind}"
        data: dict[str, Any] = {
            "record_type": "issue",
            "record_id": (te.ref.id or "") if te.ref else "",
            "record_key": te.ref.key if te.ref else None,
            "record_url": te.ref.url if te.ref else None,
            "operation": te.kind,
            "raw_event": te.raw_event,
            "changed_fields": list(te.changed),
        }
        if title:
            data["title"] = title
        if isinstance(desc, str) and desc:
            data["body"] = desc[:20000]
        if isinstance(comment, str) and comment:
            data["comment"] = comment[:20000]
        if te.ref is not None:
            data["ref"] = {
                "provider": te.ref.provider,
                "key": te.ref.key,
                "id": te.ref.id,
                "project": te.ref.project,
                "url": te.ref.url,
            }

        activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
        actor_info = activity.get("actor") if isinstance(activity.get("actor"), dict) else {}
        actor = ExternalActor(
            kind="user" if (te.actor or actor_info) else "unknown",
            external_id=str(actor_info.get("id") or ""),
            display=str(te.actor or actor_info.get("display_name") or ""),
            email=actor_info.get("email") if isinstance(actor_info.get("email"), str) else None,
        )
        delivery_id = ""
        headers = lower_headers(raw.headers)
        for header in _DELIVERY_HEADERS:
            if headers.get(header):
                delivery_id = headers[header].strip()
                break
        event_id = delivery_id or raw.dedupe_key.split(":", 1)[-1]
        return [
            IntakeEvent(
                id=event_id,
                source=conn.source_uri,
                type=event_type,
                subject=subject,
                time=None,
                data=data,
                bossactor=actor,
                bossconnection=conn.id,
                bossdedupe=raw.dedupe_key,
            )
        ]

    def enrich(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent:
        token = conn.secret("api_token")
        workspace_slug = str(conn.get("workspace_slug") or "").strip()
        ref_data = ev.data.get("ref") if isinstance(ev.data.get("ref"), dict) else {}
        issue_id = str(ref_data.get("id") or ev.data.get("record_id") or "").strip()
        if not (token and workspace_slug and issue_id):
            return ev
        tconn = TicketConnection(
            "plane",
            {
                "api_base_url": str(conn.get("api_base_url") or "https://api.plane.so"),
                "workspace_slug": workspace_slug,
                "project_id": str(ref_data.get("project") or conn.get("project_id") or ""),
            },
            project_id=conn.project_id,
            db=conn.db,
            app_settings=conn.app_settings,
            credential_resolver=lambda: token,
        )
        tref = TicketRef(
            "plane",
            id=issue_id,
            key=ref_data.get("key"),
            project=ref_data.get("project") or conn.get("project_id") or None,
        )
        ticket = self._tickets.get_ticket(tconn, tref)
        ev.data["title"] = ticket.title or ev.data.get("title") or ""
        if ticket.body_markdown:
            ev.data["body"] = ticket.body_markdown[:20000]
        if ticket.labels:
            ev.data["labels"] = list(ticket.labels)
        if ticket.url:
            ev.data["record_url"] = ticket.url
        ev.data["ticket"] = {
            "key": ticket.key,
            "id": ticket.id,
            "status": ticket.status,
            "status_category": ticket.status_category,
            "type": ticket.type,
            "labels": list(ticket.labels),
            "url": ticket.url,
        }
        return ev

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display, is_bot=actor.kind == "bot")

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        return ev.subject


__all__ = ["PlaneIntakeAdapter"]
