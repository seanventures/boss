"""Evidence-only AI SRE incident postmortems.

Assembles a durable artifact from *existing* incident evidence only.
Never invents timeline, impact, or root cause. Deterministic — no LLM.
Does not resolve or dismiss the incident. Not public GA.

This is not break-glass ``postmortem_task_id``. Do not reuse that path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc
from .reliability import ReliabilityService

_DDL = """
CREATE TABLE IF NOT EXISTS sre_postmortems (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (incident_id)
);
CREATE INDEX IF NOT EXISTS idx_sre_postmortems_project
  ON sre_postmortems(project_id, created_at DESC)
"""

_tables_ready = False


def ensure_tables(connection: Connection) -> None:
    """Idempotent DDL so ad-hoc test DBs and pre-migration envs still work."""
    global _tables_ready
    if _tables_ready:
        return
    for stmt in _DDL.split(";"):
        s = stmt.strip()
        if s:
            connection.execute(text(s))
    _tables_ready = True


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    text_value = str(value).strip()
    return text_value or None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _latest_evidence(evidence: list[dict[str, Any]], kind: str) -> Optional[dict[str, Any]]:
    found: Optional[dict[str, Any]] = None
    for item in evidence:
        if item.get("kind") == kind:
            payload = item.get("payload")
            if isinstance(payload, dict) and payload:
                found = payload
    return found


def _timeline(incident: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    first_seen = _iso(incident.get("first_seen"))
    if first_seen:
        items.append({"at": first_seen, "kind": "first_seen"})
    for signal in incident.get("signals") or []:
        at = _iso(signal.get("ts"))
        if not at:
            continue
        items.append(
            {
                "at": at,
                "kind": "signal",
                "signal_kind": signal.get("kind"),
                "signature": signal.get("signature"),
            }
        )
    for ev in incident.get("evidence") or []:
        at = _iso(ev.get("created_at"))
        if not at:
            continue
        items.append(
            {
                "at": at,
                "kind": "evidence",
                "evidence_kind": ev.get("kind"),
                "ref": ev.get("ref") or "",
            }
        )
    last_seen = _iso(incident.get("last_seen"))
    if last_seen:
        items.append({"at": last_seen, "kind": "last_seen"})
    items.sort(key=lambda row: row["at"])
    return items


def _verify_outcome(incident: dict[str, Any]) -> Optional[dict[str, Any]]:
    recorded = _latest_evidence(incident.get("evidence") or [], "verification")
    if recorded:
        return _jsonable(recorded)
    ctx = incident.get("finalize_ctx") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            ctx = {}
    if not isinstance(ctx, dict):
        return None
    verification = ctx.get("verification")
    if isinstance(verification, dict) and verification:
        return _jsonable(verification)
    return None


def _assemble(incident: dict[str, Any]) -> dict[str, Any]:
    """Build the artifact from recorded evidence only. No invented RCA or impact."""
    winner = _latest_evidence(incident.get("evidence") or [], "rca_hypothesis")
    winner_hypothesis = _jsonable(winner) if winner else None
    remediations = _jsonable(list(incident.get("remediations") or []))
    verify_outcome = _verify_outcome(incident)
    missing: list[str] = []
    if winner_hypothesis is None:
        missing.append("winner_hypothesis")
    if not remediations:
        missing.append("remediations")
    if verify_outcome is None:
        missing.append("verify_outcome")
    return {
        "incident_id": incident["id"],
        "service": incident.get("service") or "",
        "severity": incident.get("severity") or "",
        "timeline": _timeline(incident),
        "winner_hypothesis": winner_hypothesis,
        "remediations": remediations,
        "verify_outcome": verify_outcome,
        "status": "complete_enough" if winner_hypothesis is not None else "draft",
        "missing": missing,
    }


def _persist(
    connection: Connection,
    project_id: str,
    incident_id: str,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    postmortem_id = make_id("spm")
    row = connection.execute(
        text(
            """
            INSERT INTO sre_postmortems (id, project_id, incident_id, payload, created_at)
            VALUES (:id, :p, :inc, CAST(:payload AS JSONB), :now)
            ON CONFLICT (incident_id) DO UPDATE SET
                payload = EXCLUDED.payload
            RETURNING id, created_at
            """
        ),
        {
            "id": postmortem_id,
            "p": project_id,
            "inc": incident_id,
            "payload": json.dumps(artifact, default=str),
            "now": now_utc(),
        },
    ).mappings().one()
    out = dict(artifact)
    out["id"] = row["id"]
    out["created_at"] = _iso(row["created_at"])
    return out


def draft_postmortem(
    connection: Connection,
    project_id: str,
    incident_id: str,
) -> Optional[dict[str, Any]]:
    """Draft (or refresh) a postmortem from existing evidence. Does not resolve."""
    ensure_tables(connection)
    incident = ReliabilityService().get_incident(connection, project_id, incident_id)
    if not incident:
        return None
    artifact = _assemble(incident)
    stored = _persist(connection, project_id, incident_id, artifact)
    emit_event(
        connection,
        project_id,
        "sre.postmortem.drafted",
        "system",
        "sre_postmortem",
        stored["id"],
        {
            "incident_id": incident_id,
            "status": stored["status"],
            "missing": stored["missing"],
        },
    )
    return stored


def get_postmortem(
    connection: Connection,
    project_id: str,
    incident_id: str,
) -> Optional[dict[str, Any]]:
    """Return the persisted artifact, or None if never drafted."""
    ensure_tables(connection)
    row = connection.execute(
        text(
            """
            SELECT id, payload, created_at
            FROM sre_postmortems
            WHERE project_id = :p AND incident_id = :inc
            """
        ),
        {"p": project_id, "inc": incident_id},
    ).mappings().first()
    if not row:
        return None
    payload = row["payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    out = dict(payload)
    out["id"] = row["id"]
    out["created_at"] = _iso(row["created_at"])
    return out
