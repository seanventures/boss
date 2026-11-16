"""Audit rows for builder prompts and for the commit a publish put on the live URL.

BOSSAPP-1248. Writes go through ``identity.audit.record`` (the public chain writer).
This module does not hash rows itself. A publish that has no commit is stored as
``no commit``; nothing here mints a SHA.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..identity import audit
from ..identity.audit import Outcome, SecurityEvent

PROMPT_EVENT = "builder.prompt"
PUBLISH_EVENT = "publish.live"
NO_COMMIT = "no commit"

_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class BuilderAuditError(ValueError):
    """The prompt or publish cannot be recorded. ``status`` is the HTTP code."""

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RecordedAudit:
    seq: int
    event_type: str
    org_id: str
    prompt: Optional[str] = None
    project_id: Optional[str] = None
    live_url: Optional[str] = None
    commit: Optional[str] = None
    commit_stated: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        if self.event_type == PROMPT_EVENT:
            body: dict[str, Any] = {
                "event_type": self.event_type, "seq": self.seq, "org_id": self.org_id,
                "prompt": self.prompt,
            }
            if self.project_id:
                body["project_id"] = self.project_id
            return body
        body = {
            "event_type": self.event_type, "seq": self.seq, "org_id": self.org_id,
            "live_url": self.live_url, "commit": self.commit,
            "commit_stated": self.commit_stated,
        }
        if self.project_id:
            body["project_id"] = self.project_id
        return body


def commit_sha(value: Any) -> Optional[str]:
    """A full git SHA the caller supplied, or None. Never invents one."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or _SHA.fullmatch(text) is None:
        return None
    return text


def record_builder_prompt(db: Any, *, org_id: str, prompt: str, actor: Any = None,
                          project_id: Optional[str] = None) -> RecordedAudit:
    """One chain row for one builder prompt."""
    text = str(prompt or "").strip()
    if not text:
        raise BuilderAuditError("A builder prompt is required.")
    org = _org(org_id)
    details: dict[str, Any] = {"kind": "builder_prompt", "prompt": text}
    if project_id:
        details["project_id"] = str(project_id)
    recorded = _write(db, org_id=org, event_type=PROMPT_EVENT, actor=actor,
                      target_type="builder_prompt", target_id=project_id, details=details)
    return RecordedAudit(seq=recorded.seq, event_type=PROMPT_EVENT, org_id=org, prompt=text,
                         project_id=str(project_id) if project_id else None)


def record_publish(db: Any, *, org_id: str, live_url: str, commit: Any = None, actor: Any = None,
                   project_id: Optional[str] = None) -> RecordedAudit:
    """One chain row for the commit a publish put on ``live_url``.

    ``commit`` is kept only when it is a full 40- or 64-character hex SHA. Anything else
    is recorded as no commit. The missing SHA is not filled in.
    """
    url = str(live_url or "").strip()
    if not url:
        raise BuilderAuditError("A publish needs the live URL.")
    org = _org(org_id)
    sha = commit_sha(commit)
    stated = sha if sha else NO_COMMIT
    details: dict[str, Any] = {
        "kind": "publish", "live_url": url, "commit": sha, "commit_stated": stated,
    }
    if project_id:
        details["project_id"] = str(project_id)
    recorded = _write(db, org_id=org, event_type=PUBLISH_EVENT, actor=actor,
                      target_type="live_url", target_id=url, details=details)
    return RecordedAudit(seq=recorded.seq, event_type=PUBLISH_EVENT, org_id=org, live_url=url,
                         commit=sha, commit_stated=stated,
                         project_id=str(project_id) if project_id else None)


def rows_for(db: Any, org_id: str) -> list[dict[str, Any]]:
    """The builder-prompt and publish rows for one org, oldest first."""
    from sqlalchemy import text

    found = db.execute(text(
        "SELECT seq, event_type, actor_principal_id, details FROM identity_security_events "
        "WHERE org_id = :o AND event_type IN (:prompt, :publish) ORDER BY seq"),
        {"o": org_id, "prompt": PROMPT_EVENT, "publish": PUBLISH_EVENT}).mappings().all()
    out: list[dict[str, Any]] = []
    for row in found:
        details = _details(row["details"])
        item: dict[str, Any] = {
            "seq": int(row["seq"]), "event_type": row["event_type"], "org_id": org_id,
            "actor_principal_id": row["actor_principal_id"],
        }
        if row["event_type"] == PROMPT_EVENT:
            item["prompt"] = details.get("prompt")
            item["project_id"] = details.get("project_id")
        else:
            item["live_url"] = details.get("live_url")
            item["commit"] = details.get("commit")
            item["commit_stated"] = details.get("commit_stated")
            item["project_id"] = details.get("project_id")
        out.append(item)
    return out


def _org(org_id: str) -> str:
    org = str(org_id or "").strip()
    if not org:
        raise BuilderAuditError("An org is required.")
    return org


def _details(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    if raw is None:
        return {}
    return dict(raw)


def _write(db: Any, *, org_id: str, event_type: str, actor: Any, target_type: str,
           target_id: Optional[str], details: dict[str, Any]) -> audit.RecordedEvent:
    if actor is not None and getattr(actor, "principal", None) is not None:
        if str(actor.principal.org_id) != org_id:
            raise BuilderAuditError(
                "The org on the request does not match the signed-in org.", status=403)
        event = SecurityEvent.from_context(
            actor, event_type, Outcome.SUCCESS, target_type=target_type,
            target_id=None if target_id is None else str(target_id), details=details)
    else:
        event = SecurityEvent(
            org_id=org_id, event_type=event_type, outcome=Outcome.SUCCESS,
            target_type=target_type, target_id=None if target_id is None else str(target_id),
            details=details)
    return audit.record(event, db=db)
