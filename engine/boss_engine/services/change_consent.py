"""Diff-bound consent: an agent's change to a skill or a prompt is written only when a person
approved EXACTLY that change (BOSSAPP-843).

THE BUG THIS CLOSES (BOSSAPP-896). `draft_skill` told the model "the operator approves the
draft with one press" and then saved it — the draft WAS the live skill. A later fix stored a
create disabled, but it still wrote, and the approval that followed re-saved whatever the decision
row carried, never checking it was still the change the person had looked at.

HOW THE BINDING WORKS.

* :func:`request` stores what the agent proposes (``proposed``), the diff a person reads, and
  ``diff_hash = sha256(kind, key, base content hash, proposed content)``, and opens a decision
  whose question carries the diff.
* Approving the decision marks the consent ``approved`` (:func:`approve_for_decision`).
* The write site — ``skill_definitions.save_skill`` for every agent-origin save — recomputes the
  hash from the content it is ABOUT TO WRITE and the content it would REPLACE, and calls
  :func:`consume`: one ``UPDATE … WHERE status = 'approved' AND diff_hash = :h`` that flips the
  row to ``consumed``. No row, no write. So a proposal edited after approval, a skill someone else
  changed in between (the base moved) and a replay of the same approval all find nothing.

People are exempt: an edit made in the console by a person is its own consent.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

SUBJECT_KINDS = ("skill", "prompt")


class ConsentRequired(PermissionError):
    """An agent-origin write with no matching approved consent. Raised by the write site."""


def diff_hash(kind: str, key: str, base_hash: str, proposed: dict[str, Any]) -> str:
    body = json.dumps({"kind": kind, "key": key, "base": base_hash or "", "after": proposed},
                      sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _row(row: Any) -> dict[str, Any]:
    proposed = row["proposed"]
    if isinstance(proposed, str):
        proposed = json.loads(proposed)
    return {
        "id": row["id"], "project_id": row["project_id"], "subject_kind": row["subject_kind"],
        "subject_key": row["subject_key"], "diff_hash": row["diff_hash"],
        "base_hash": row["base_hash"], "diff": row["diff"], "proposed": proposed or {},
        "decision_id": row["decision_id"], "status": row["status"],
        "requested_by": row["requested_by"], "decided_by": row["decided_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def request(
    connection: Connection,
    *,
    project_id: str,
    subject_kind: str,
    subject_key: str,
    base_hash: str,
    proposed: dict[str, Any],
    diff: str,
    requested_by: str,
    question: str,
    action: str,
    recommendation: dict[str, Any],
    reason: str,
    risk_tier: int,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Open a consent and the decision that answers it. Returns {consent_id, decision_id, hash}."""
    if subject_kind not in SUBJECT_KINDS:
        raise ValueError(f"subject_kind must be one of {SUBJECT_KINDS}")
    digest = diff_hash(subject_kind, subject_key, base_hash, proposed)
    consent_id = make_id("ccn")
    decision_id = create_blocking_decision(
        connection, project_id, None, question,
        {"diff": diff, **recommendation, "action": action, "consent_id": consent_id,
         "diff_hash": digest},
        {}, reason, risk_tier=risk_tier)
    connection.execute(
        text("""
            INSERT INTO change_consents (id, project_id, subject_kind, subject_key, diff_hash,
                                         base_hash, diff, proposed, decision_id, requested_by)
            VALUES (:id, :p, :kind, :key, :h, :base, :diff, CAST(:proposed AS JSONB), :d, :by)
        """),
        {"id": consent_id, "p": project_id, "kind": subject_kind, "key": subject_key,
         "h": digest, "base": base_hash or "", "diff": diff[:20000],
         "proposed": json.dumps({**proposed, "_meta": meta or {}}, default=str),
         "d": decision_id, "by": requested_by},
    )
    emit_event(connection, project_id, "change_consent.requested", requested_by,
               "change_consent", consent_id,
               {"subject_kind": subject_kind, "subject_key": subject_key, "diff_hash": digest,
                "decision_id": decision_id})
    return {"consent_id": consent_id, "decision_id": decision_id, "diff_hash": digest}


def for_decision(connection: Connection, decision_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM change_consents WHERE decision_id = :d"), {"d": decision_id},
    ).mappings().first()
    return _row(row) if row else None


def approve_for_decision(connection: Connection, decision_id: str,
                         actor: str) -> Optional[dict[str, Any]]:
    """The person approved the decision: its consent may now be consumed, once."""
    row = connection.execute(
        text("UPDATE change_consents SET status = 'approved', decided_by = :a, decided_at = now() "
             "WHERE decision_id = :d AND status = 'pending' RETURNING *"),
        {"d": decision_id, "a": actor},
    ).mappings().first()
    return _row(row) if row else None


def reject_for_decision(connection: Connection, decision_id: str, actor: str) -> None:
    connection.execute(
        text("UPDATE change_consents SET status = 'rejected', decided_by = :a, decided_at = now() "
             "WHERE decision_id = :d AND status IN ('pending', 'approved')"),
        {"d": decision_id, "a": actor},
    )


def consume(connection: Connection, *, project_id: str, subject_kind: str, subject_key: str,
            digest: str) -> Optional[str]:
    """Spend one approved consent whose hash is `digest`. The consent id, or None (no write)."""
    row = connection.execute(
        text("""
            UPDATE change_consents SET status = 'consumed', consumed_at = now()
            WHERE id = (
                SELECT id FROM change_consents
                WHERE project_id = :p AND subject_kind = :kind AND subject_key = :key
                  AND diff_hash = :h AND status = 'approved'
                ORDER BY decided_at NULLS LAST, created_at
                LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING id
        """),
        {"p": project_id, "kind": subject_kind, "key": subject_key, "h": digest},
    ).first()
    if row is None:
        return None
    emit_event(connection, project_id, "change_consent.consumed", "system", "change_consent",
               str(row[0]), {"subject_kind": subject_kind, "subject_key": subject_key,
                             "diff_hash": digest})
    return str(row[0])


def pending(connection: Connection, project_id: str, subject_kind: str,
            subject_key: Optional[str] = None) -> list[dict[str, Any]]:
    """Consents still waiting for a person: their decision is one the Approvals inbox shows
    (decision_actions.waiting_on_operator — the one reader of "open")."""
    from .decision_actions import waiting_on_operator

    rows = connection.execute(
        text("SELECT * FROM change_consents WHERE project_id = :p AND subject_kind = :kind "
             "AND status = 'pending' AND (CAST(:key AS TEXT) IS NULL OR subject_key = :key) "
             "ORDER BY created_at"),
        {"p": project_id, "kind": subject_kind, "key": subject_key},
    ).mappings().all()
    if not rows:
        return []
    open_ids = {d["id"] for d in waiting_on_operator(connection, project_id,
                                                      include_expired=True)}
    return [_row(r) for r in rows if r["decision_id"] in open_ids]
