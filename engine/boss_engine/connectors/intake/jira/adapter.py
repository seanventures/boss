"""Jira Cloud on the intake layer (P9b-4, BOSSAPP-1103): delegates to JiraTicketAdapter."""
from __future__ import annotations

import json
from typing import Any, Optional

from ...tickets.contract import TicketConnection, TicketRef
from ...tickets.jira.adapter import JiraTicketAdapter
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
    "x-atlassian-webhook-identifier",
    "x-boss-delivery",
    "webhook-id",
)


class JiraIntakeAdapter(IntakeAdapterBase):
    key = "jira"

    def __init__(self) -> None:
        self._tickets = JiraTicketAdapter()

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        secret = conn.secret("webhook_secret") or ""
        if not secret:
            raise VerifyRefused("not_configured", provider=self.key)
        headers = lower_headers(req.headers)
        token = str((req.query or {}).get("token") or "").strip()
        if token and "x-boss-webhook-token" not in headers:
            headers["x-boss-webhook-token"] = token
        if not headers.get("x-hub-signature") and not headers.get("x-boss-webhook-token"):
            raise VerifyRefused("missing_signature", provider=self.key)
        if not self._tickets.check_webhook(headers, req.body, secret):
            raise VerifyRefused("bad_signature", provider=self.key)
        scheme = "jira_sha256" if headers.get("x-hub-signature") else "jira_token"
        return Verified(scheme=scheme)

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        for header in _DELIVERY_HEADERS:
            value = req.header(header).strip()[:200]
            if value:
                return f"jira:{value}"
        return f"jira:sha256:{req.body_sha256()}"

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            payload = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as error:
            raise ParseRefused(
                f"Jira body is not JSON: {type(error).__name__}", provider=self.key
            ) from None
        if not isinstance(payload, dict):
            raise ParseRefused("Jira body is not an object", provider=self.key)
        try:
            te = self._tickets.read_webhook(payload)
        except Exception as error:  # noqa: BLE001
            raise ParseRefused(
                f"Jira body unreadable: {type(error).__name__}", provider=self.key
            ) from None

        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
        summary = str(fields.get("summary") or "")
        desc = fields.get("description")
        body_text = desc if isinstance(desc, str) else ""
        comment_body = comment.get("body")
        comment_text = comment_body if isinstance(comment_body, str) else ""
        labels = [str(lb) for lb in (fields.get("labels") or []) if lb]
        subject = (te.ref.key or te.ref.id) if te.ref else None
        event_type = f"com.atlassian.jira.issue.{te.kind}"
        data: dict[str, Any] = {
            "record_type": "issue",
            "record_id": (te.ref.id or te.ref.key or "") if te.ref else "",
            "record_key": te.ref.key if te.ref else None,
            "record_url": te.ref.url if te.ref else None,
            "operation": te.kind,
            "raw_event": te.raw_event,
            "changed_fields": list(te.changed),
        }
        if summary:
            data["title"] = summary
        if body_text:
            data["body"] = body_text[:20000]
        if comment_text:
            data["comment"] = comment_text[:20000]
        if labels:
            data["labels"] = labels
        if te.ref is not None:
            data["ref"] = {
                "provider": te.ref.provider,
                "key": te.ref.key,
                "id": te.ref.id,
                "url": te.ref.url,
            }

        user = payload.get("user") if isinstance(payload.get("user"), dict) else (
            comment.get("author") if isinstance(comment.get("author"), dict) else {}
        )
        actor = ExternalActor(
            kind="user" if (te.actor or user) else "unknown",
            external_id=str(user.get("accountId") or ""),
            display=str(te.actor or user.get("displayName") or ""),
            email=user.get("emailAddress") if isinstance(user.get("emailAddress"), str) else None,
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
        base_url = str(conn.get("base_url") or "").strip()
        token = conn.secret("api_token")
        ref_data = ev.data.get("ref") if isinstance(ev.data.get("ref"), dict) else {}
        key = str(ref_data.get("key") or ev.data.get("record_key") or ev.subject or "").strip()
        issue_id = str(ref_data.get("id") or ev.data.get("record_id") or "").strip()
        if not (base_url and token and (key or issue_id)):
            return ev
        tconn = TicketConnection(
            "jira",
            {
                "base_url": base_url,
                "email": conn.get("email"),
                "project_key": conn.get("project_key"),
            },
            project_id=conn.project_id,
            db=conn.db,
            app_settings=conn.app_settings,
            credential_resolver=lambda: token,
        )
        tref = TicketRef("jira", key=key or None, id=issue_id or None, url=ref_data.get("url"))
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


__all__ = ["JiraIntakeAdapter"]
