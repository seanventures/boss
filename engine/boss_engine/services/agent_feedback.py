"""Agent feedback: an agent's complaints and suggestions, as typed records (BOSSAPP-889).

WHY. An agent that hits a broken tool, a grant it lacks or a brief that contradicts itself works
round it and the signal is lost in a transcript. This is the cheap channel for it: one row per
thing the agent wants a person to know, typed by what it is about, shown in Activity › Feedback
grouped by target, and read by the reflection agent (BOSSAPP-888) as evidence.

WHAT IT IS NOT. Not a platform gap (file_platform_gap is a diagnosed defect in BOSS with tests
for fixed), not a decision (it asks nobody for anything), not an action: filing changes nothing
but this table.

HONEST BY CONSTRUCTION
* WHO FILED IT is the pipeline's, never the model's: the role the run acts as and the run it was
  filed in come from the LLM context (llm_telemetry) or the actor, not from the arguments.
* IDEMPOTENT PER RUN: the dedupe key is (run, role, kind, target). The same complaint from the
  same run is the same row — an agent stuck in a loop files once — and the answer says so.
* NO CREDENTIALS: the body and every evidence line are passed through redact_secrets before they
  are stored; an agent quoting a failing curl with a key in it stores `[LABEL]`, not the key.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

KINDS = ("complaint", "suggestion")
TARGET_KINDS = ("tool", "skill", "grant", "brief", "workflow", "model", "platform", "other")
STATUSES = ("open", "acknowledged", "resolved", "dismissed")

BODY_MAX = 2_000
TARGET_MAX = 200
EVIDENCE_MAX = 8
EVIDENCE_LINE_MAX = 400


class AgentFeedbackError(ValueError):
    """A filing or a change that cannot be taken, in words an agent or a person can act on."""


def _clean(value: Any, limit: int) -> str:
    from .redaction import redact_secrets

    said = " ".join(str(value or "").split())
    return redact_secrets(said)[:limit]


def dedupe_key(run_ref: str, role_key: str, kind: str, target_kind: str, target: str) -> str:
    body = "\x1f".join((run_ref or "", role_key or "", kind, target_kind, target.lower()))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _row(row: Any) -> dict[str, Any]:
    evidence = row["evidence"]
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            evidence = []
    iso = lambda v: v.isoformat() if v is not None and hasattr(v, "isoformat") else v  # noqa: E731
    return {"id": row["id"], "project_id": row["project_id"], "role_key": row["role_key"],
            "run_ref": row["run_ref"] or None, "kind": row["kind"],
            "target_kind": row["target_kind"], "target": row["target"], "body": row["body"],
            "evidence": evidence or [], "status": row["status"], "filed_by": row["filed_by"],
            "resolved_by": row["resolved_by"], "resolution": row["resolution"],
            "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"])}


def file_feedback(connection: Connection, project_id: str, *, kind: str, target_kind: str,
                  target: str, body: str, evidence: Optional[list[Any]] = None,
                  role_key: Optional[str] = None, run_ref: Optional[str] = None,
                  actor: str) -> dict[str, Any]:
    """Record one complaint or suggestion. Returns {created, feedback}. Idempotent per run."""
    kind = str(kind or "").strip().lower()
    target_kind = str(target_kind or "").strip().lower()
    if kind not in KINDS:
        raise AgentFeedbackError(f"kind must be one of: {', '.join(KINDS)}")
    if target_kind not in TARGET_KINDS:
        raise AgentFeedbackError(f"target_kind must be one of: {', '.join(TARGET_KINDS)}")
    target = _clean(target, TARGET_MAX)
    said = _clean(body, BODY_MAX)
    if not target:
        raise AgentFeedbackError("name the target: the tool, skill, grant, brief or model it is about")
    if not said:
        raise AgentFeedbackError("say what happened, or what would help, in a sentence or two")
    lines = [_clean(e, EVIDENCE_LINE_MAX) for e in (evidence or [])][:EVIDENCE_MAX]
    lines = [e for e in lines if e]
    role = (str(role_key or "").strip().lower() or None)
    run = str(run_ref or "").strip()
    key = dedupe_key(run, role or "", kind, target_kind, target)
    row = connection.execute(text(
        "INSERT INTO agent_feedback (id, project_id, role_key, run_ref, kind, target_kind, target, "
        "body, evidence, dedupe_key, filed_by) VALUES (:i, :p, :r, :run, :k, :tk, :t, :b, "
        "CAST(:e AS JSONB), :d, :a) ON CONFLICT (project_id, dedupe_key) DO NOTHING RETURNING *"),
        {"i": make_id("afb"), "p": project_id, "r": role, "run": run, "k": kind, "tk": target_kind,
         "t": target, "b": said, "e": json.dumps(lines), "d": key, "a": actor}).mappings().first()
    if row is None:
        existing = connection.execute(text(
            "SELECT * FROM agent_feedback WHERE project_id = :p AND dedupe_key = :d"),
            {"p": project_id, "d": key}).mappings().one()
        return {"created": False, "feedback": _row(existing)}
    item = _row(row)
    # The event carries what it is about, never the body (it is the agent's words).
    emit_event(connection, project_id, "agent.feedback_filed", actor, "agent_feedback",
               item["id"], {"kind": kind, "target_kind": target_kind, "target": target,
                            "role_key": role, "run_ref": run or None})
    return {"created": True, "feedback": item}


def list_feedback(connection: Connection, project_id: str, *, status: Optional[str] = None,
                  role_key: Optional[str] = None, target_kind: Optional[str] = None,
                  limit: int = 200) -> list[dict[str, Any]]:
    where = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "lim": max(1, min(int(limit or 200), 500))}
    if status:
        if status not in STATUSES:
            raise AgentFeedbackError(f"status must be one of: {', '.join(STATUSES)}")
        where.append("status = :s")
        params["s"] = status
    if role_key:
        where.append("role_key = :r")
        params["r"] = role_key
    if target_kind:
        where.append("target_kind = :tk")
        params["tk"] = target_kind
    rows = connection.execute(text(
        f"SELECT * FROM agent_feedback WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC, id LIMIT :lim"), params).mappings().all()
    return [_row(r) for r in rows]


def grouped(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same records, one group per target (target_kind + target): the most-reported first."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        k = (item["target_kind"], item["target"])
        g = groups.setdefault(k, {"target_kind": k[0], "target": k[1], "complaints": 0,
                                  "suggestions": 0, "open": 0, "roles": [], "latest_at": None,
                                  "items": []})
        g["complaints" if item["kind"] == "complaint" else "suggestions"] += 1
        g["open"] += 1 if item["status"] == "open" else 0
        if item["role_key"] and item["role_key"] not in g["roles"]:
            g["roles"].append(item["role_key"])
        if g["latest_at"] is None or str(item["created_at"]) > str(g["latest_at"]):
            g["latest_at"] = item["created_at"]
        g["items"].append(item)
    return sorted(groups.values(),
                  key=lambda g: (-g["open"], -len(g["items"]), str(g["target_kind"]), g["target"]))


def set_status(connection: Connection, project_id: str, feedback_id: str, *, status: str,
               actor: str, resolution: Optional[str] = None) -> dict[str, Any]:
    """A person acknowledges, resolves or dismisses one record. Agents never close their own."""
    if status not in STATUSES:
        raise AgentFeedbackError(f"status must be one of: {', '.join(STATUSES)}")
    note = _clean(resolution, BODY_MAX) or None
    closing = status in ("resolved", "dismissed")
    row = connection.execute(text(
        "UPDATE agent_feedback SET status = :s, updated_at = now(), "
        "resolved_by = CASE WHEN :closing THEN :a ELSE NULL END, "
        "resolution = CASE WHEN :closing THEN :n ELSE NULL END "
        "WHERE project_id = :p AND id = :i RETURNING *"),
        {"s": status, "closing": closing, "a": actor, "n": note, "p": project_id,
         "i": feedback_id}).mappings().first()
    if row is None:
        raise LookupError(f"no feedback {feedback_id!r} in this project")
    item = _row(row)
    emit_event(connection, project_id, "agent.feedback_status", actor, "agent_feedback",
               item["id"], {"status": status})
    return item
