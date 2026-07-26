"""Slack comms shaping (spec §D: per-department channels, persona prefix/signature rendering,
digest batching). Pure rendering + a batcher that collapses many draft Slack outbox messages into
one digest per channel — so a busy department gets a single readable update instead of a stream.

Rendering is pure and testable; the batcher operates over the existing outbox so digests flow
through the same delivery backbone (and the source messages are marked 'batched' so they are not
also delivered individually).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .comms import CommsService
from .core import emit_event

DEFAULT_CHANNEL_PREFIX = "#boss-"
DEFAULT_MIN_BATCH = 2


def department_channel(department: str, prefix: str = DEFAULT_CHANNEL_PREFIX) -> str:
    """Map a department to its Slack channel deterministically, e.g. 'Engineering' → '#boss-engineering'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (department or "").strip().lower()).strip("-")
    return f"{prefix}{slug}" if slug else f"{prefix}general"


def render_persona_message(
    persona: str,
    role: Optional[str],
    subject: Optional[str],
    body: str,
    *,
    signature: bool = True,
) -> str:
    """Render a Slack message with a persona prefix and (optional) signature footer."""
    persona = (persona or "BOSS").strip()
    role = (role or "").strip()
    header = f"*[{persona} · {role}]*" if role else f"*[{persona}]*"
    parts = [header]
    if subject and subject.strip():
        parts.append(f"*{subject.strip()}*")
    parts.append((body or "").strip())
    rendered = "\n".join(parts)
    if signature:
        rendered += f"\n\n— {persona}" + (f", {role}" if role else "") + " · BOSS"
    return rendered


def build_digest(channel: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Render a single digest message summarizing a batch of updates for one channel."""
    lines = [f"*Digest — {len(messages)} update(s) for {channel}*", ""]
    for index, message in enumerate(messages, start=1):
        head = (message.get("subject") or "(no subject)").strip()
        persona = (message.get("persona") or "").strip()
        tag = f" _{persona}_" if persona else ""
        lines.append(f"{index}. *{head}*{tag}")
        body = (message.get("body") or "").strip()
        if body:
            lines.append(f"   {body}")
    return {"channel": channel, "count": len(messages), "text": "\n".join(lines)}


class SlackCommsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.comms = CommsService(settings)

    def batch_digests(
        self,
        connection: Connection,
        project_id: str,
        actor: str = "comms_worker",
        *,
        channel: Optional[str] = None,
        min_batch: int = DEFAULT_MIN_BATCH,
    ) -> dict[str, Any]:
        """Collapse draft Slack outbox messages into one digest per channel. Channels with fewer than
        `min_batch` pending messages are left untouched. Source messages are marked 'batched'."""
        clauses = ["project_id = :project_id", "status = 'draft'", "channel LIKE 'slack%'"]
        params: dict[str, Any] = {"project_id": project_id}
        if channel:
            clauses.append("channel = :channel")
            params["channel"] = channel
        rows = connection.execute(
            text(
                f"""
                SELECT id, channel, subject, body
                FROM outbox_messages
                WHERE {' AND '.join(clauses)}
                ORDER BY channel ASC, created_at ASC
                """
            ),
            params,
        ).mappings().all()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["channel"], []).append(dict(row))

        digests: list[dict[str, Any]] = []
        for chan, messages in grouped.items():
            if len(messages) < min_batch:
                continue
            digest = build_digest(chan, messages)
            source_ids = [m["id"] for m in messages]
            dedupe = hashlib.sha1(
                ("|".join(source_ids)).encode("utf-8")
            ).hexdigest()[:16]
            created = self.comms.create_outbox_message(
                connection,
                project_id,
                channel=chan,
                subject=f"Digest: {digest['count']} updates",
                body=digest["text"],
                dedupe_key=f"slack-digest:{chan}:{dedupe}",
                actor=actor,
            )
            connection.execute(
                text(
                    """
                    UPDATE outbox_messages
                    SET status = 'batched', updated_at = now()
                    WHERE id = ANY(:ids)
                    """
                ),
                {"ids": source_ids},
            )
            emit_event(
                connection,
                project_id,
                "outbox.digest_batched",
                actor,
                "outbox_message",
                created["id"],
                {"channel": chan, "batched": len(source_ids), "source_ids": source_ids},
            )
            digests.append(
                {
                    "channel": chan,
                    "digest_message_id": created["id"],
                    "batched": len(source_ids),
                    "source_ids": source_ids,
                }
            )

        return {
            "project_id": project_id,
            "digests": digests,
            "channels_batched": len(digests),
            "messages_batched": sum(d["batched"] for d in digests),
        }
