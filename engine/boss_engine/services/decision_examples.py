"""Decision examples: what people decided, kept for evaluating and tuning the decision model
(BOSSAPP-893).

WHY. Typed decisions and recommendations run live, and every time a person overrides one that is
the most valuable label the org produces — and it was spent the moment it was given. This keeps
it: a snapshot of the decision as the person saw it (question, recommendation, options), what BOSS
recommended and what the person did, and their note.

TWO WAYS IN.
* AUTOMATIC CAPTURE OF OVERRIDES (:func:`capture_override`, called by DecisionService.transition):
  a person rejecting or sending back a recommendation, or forcing an approval past BOSS's own gate.
  Best effort, in a savepoint: capturing never fails the answer itself.
* SAVE AS EXAMPLE (:func:`save`): a person marks any answered decision as an example, with a label
  ("good call", "wrong context"...) — including approvals, which are not overrides.

EXPORT IS ANONYMIZED (:func:`export_jsonl`), with portability's discipline (BOSSAPP-837): any value
under a secret-shaped key becomes a typed empty placeholder, every string is passed through the
redaction patterns (credentials, emails, phone numbers, IPs, card numbers), and every person or
actor id becomes a stable pseudonym local to the export (``person_1``). Project ids never appear.
The stored snapshot stays inside BOSS; only the export is meant to leave.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

SOURCES = ("override", "saved")
OUTCOMES = ("approve", "approve_forced", "reject", "revise", "execute")
#: What an answer means in example terms.
_OUTCOME_OF = {"approved": "approve", "rejected": "reject", "revision_requested": "revise",
               "executed": "execute"}
#: Recommendation keys that only route the card (ids, hashes); dropped from the snapshot.
_ROUTING_KEYS = re.compile(r"(^|_)(id|ids|hash|sha|sha256|token|run|url)$", re.IGNORECASE)
_ACTOR_KEYS = ("requested_by", "actor", "created_by", "approved_by", "decided_by", "owner",
               "assignee", "agent", "role", "person")


class ExampleError(ValueError):
    status = 422


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    rec = _json(row.get("recommendation"), {}) or {}
    return {
        "question": str(row.get("question") or "")[:8000],
        "action": rec.get("action"),
        "recommendation": rec,
        "options": _json(row.get("options"), []) or [],
        "risk_tier": int(row.get("risk_tier") or 0),
        "default_action": row.get("default_action"),
    }


def _insert(connection: Connection, row: dict[str, Any], *, source: str, outcome: str,
            actor: str, note: Optional[str], label: str = "") -> Optional[dict[str, Any]]:
    snap = _snapshot(row)
    created = connection.execute(
        text("""
            INSERT INTO decision_examples (id, project_id, decision_id, source, action, outcome,
                                           recommended, snapshot, note, label, decided_by,
                                           created_by)
            VALUES (:id, :p, :d, :src, :action, :outcome, :rec, CAST(:snap AS JSONB), :note,
                    :label, :by, :creator)
            ON CONFLICT (decision_id, source) DO UPDATE SET
              outcome = EXCLUDED.outcome, note = EXCLUDED.note,
              label = CASE WHEN EXCLUDED.label <> '' THEN EXCLUDED.label
                           ELSE decision_examples.label END,
              snapshot = EXCLUDED.snapshot, updated_at = now()
            RETURNING *
        """),
        {"id": make_id("dex"), "p": row["project_id"], "d": row["id"], "src": source,
         "action": snap["action"], "outcome": outcome,
         "rec": str((snap["recommendation"] or {}).get("recommended_action")
                    or row.get("default_action") or "approve"),
         "snap": json.dumps(snap, default=str), "note": (note or "")[:4000],
         "label": (label or "")[:200], "by": actor or "", "creator": actor or ""},
    ).mappings().one()
    emit_event(connection, row["project_id"], "decision_example.captured", actor or "system",
               "decision", row["id"], {"source": source, "outcome": outcome})
    return _out(created)


def capture_override(connection: Connection, row: dict[str, Any], *, action: str, actor: str,
                     note: Optional[str], force: bool = False) -> Optional[dict[str, Any]]:
    """An answer that went against BOSS's recommendation becomes an example. Never raises."""
    if action == "approve" and not force:
        return None
    outcome = "approve_forced" if action == "approve" else action
    if outcome not in ("approve_forced", "reject", "revise"):
        return None
    try:
        with connection.begin_nested():
            return _insert(connection, row, source="override", outcome=outcome, actor=actor,
                           note=note)
    except Exception:  # noqa: BLE001 — a capture never fails the person's answer
        import logging

        logging.getLogger(__name__).warning("decision example capture skipped for %s",
                                            row.get("id"), exc_info=True)
        return None


def save(connection: Connection, *, project_id: str, decision_id: str, actor: str,
         label: str = "", note: str = "") -> dict[str, Any]:
    """Save as example: a person keeps any ANSWERED decision, with their label."""
    row = connection.execute(
        text("SELECT * FROM decisions WHERE id = :d AND project_id = :p"),
        {"d": decision_id, "p": project_id},
    ).mappings().first()
    if row is None:
        err = ExampleError(f"no decision {decision_id!r} in this project")
        err.status = 404
        raise err
    outcome = _OUTCOME_OF.get(str(row["status"]))
    if outcome is None:
        raise ExampleError(f"decision {decision_id} is {row['status']}; only an answered "
                           "decision can be saved as an example")
    return _insert(connection, dict(row), source="saved", outcome=outcome, actor=actor,
                   note=note or row.get("resolution_note"), label=label) or {}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["snapshot"] = _json(out.get("snapshot"), {})
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def list_examples(connection: Connection, project_id: str, *, source: Optional[str] = None,
                  limit: int = 500) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM decision_examples WHERE project_id = :p "
             "AND (CAST(:s AS TEXT) IS NULL OR source = :s) ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "s": source, "n": max(1, min(int(limit), 5000))},
    ).mappings().all()
    return [_out(r) for r in rows]


def delete(connection: Connection, *, project_id: str, example_id: str, actor: str) -> bool:
    gone = connection.execute(
        text("DELETE FROM decision_examples WHERE id = :i AND project_id = :p RETURNING id"),
        {"i": example_id, "p": project_id}).first()
    if gone:
        emit_event(connection, project_id, "decision_example.deleted", actor, "decision_example",
                   example_id, {})
    return gone is not None


# ── export ──────────────────────────────────────────────────────────────────────────────────


class _Pseudonyms:
    def __init__(self) -> None:
        self.seen: dict[str, str] = {}
        self.count = 0

    def __call__(self, value: str) -> str:
        if not value:
            return value
        if value not in self.seen:
            self.count += 1
            self.seen[value] = f"person_{self.count}"
        return self.seen[value]


def _anonymize(value: Any, people: _Pseudonyms, known: Iterable[str], key: str = "") -> Any:
    from .redaction import redact

    if isinstance(value, dict):
        return {k: _anonymize(v, people, known, str(k)) for k, v in value.items()
                if not _ROUTING_KEYS.search(str(k))}
    if isinstance(value, list):
        return [_anonymize(v, people, known, key) for v in value]
    if isinstance(value, str):
        if key in _ACTOR_KEYS:
            return people(value)
        out = value
        for name in known:            # actor ids the decision names, wherever they appear
            if name and len(name) >= 3 and name in out:
                out = out.replace(name, people(name))
        return redact(out)[0]
    return value


def anonymize_example(example: dict[str, Any], people: Optional[_Pseudonyms] = None,
                      project_ids: Iterable[str] = ()) -> dict[str, Any]:
    from .portability import _scrub

    people = people or _Pseudonyms()
    snap = example.get("snapshot") or {}
    known = [example.get("decided_by") or "", *[str(p) for p in project_ids]]
    record = {
        "question": snap.get("question") or "",
        "action": snap.get("action"),
        "context": {k: v for k, v in (snap.get("recommendation") or {}).items() if k != "action"},
        "options": snap.get("options") or [],
        "risk_tier": snap.get("risk_tier"),
        "recommended": example.get("recommended"),
        "outcome": example.get("outcome"),
        "source": example.get("source"),
        "label": example.get("label") or "",
        "note": example.get("note") or "",
        "decided_by": example.get("decided_by") or "",
    }
    found: list[str] = []
    scrubbed = _scrub(record, "", found)          # secret-shaped keys -> typed placeholders
    for pid in project_ids:                         # project ids never leave
        people.seen.setdefault(str(pid), "project")
    return _anonymize(scrubbed, people, known)


def export_jsonl(connection: Connection, project_id: str, *,
                 source: Optional[str] = None) -> str:
    """One anonymized example per line (JSONL), newest last."""
    examples = list(reversed(list_examples(connection, project_id, source=source, limit=5000)))
    people = _Pseudonyms()
    lines = []
    for i, example in enumerate(examples, start=1):
        record = anonymize_example(example, people, project_ids=[project_id])
        lines.append(json.dumps({"example": i, **record}, sort_keys=True, default=str))
    return "\n".join(lines) + ("\n" if lines else "")
