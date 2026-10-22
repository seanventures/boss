"""Event-driven lifecycle ingest (BOSSAPP-622; operator decision 2026-10-21, ported from PR #13).

A database trigger queues a code-task run the moment its status becomes completed or failed,
and an incident the moment it becomes resolved (``kg_ingest_queue``), whatever code path
finished it. The worker drains the queue every ``QUEUE_POLL_SECONDS`` through
POST /v1/knowledge/ingest/tick; every ``SWEEP_SECONDS`` the same call also sweeps the last
``SWEEP_DAYS`` for anything the triggers missed (a record finished before the trigger existed),
and reconciles each project's AGE projection with the tables (knowledge/age.py).

Each record becomes a lifecycle node through ``capture_lifecycle`` (scrubbed, stored once per
body). No model is called: the structure is captured, and distilling facts from it is the
Archivist's, behind the knowledge budget. Each record is ingested in its own transaction, so one
bad record cannot hold the others back; a failure stays queued with its error and is retried on
the next tick, at most ``MAX_ATTEMPTS`` times.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

QUEUE_POLL_SECONDS = 30
SWEEP_SECONDS = 600
SWEEP_DAYS = 7
MAX_ATTEMPTS = 3
MAX_BODY_CHARS = 4000


def _excerpt(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, default=str, sort_keys=True)
    return value[:limit]


def record_for(connection: Connection, project_id: str, source_kind: str, source_id: str) -> dict | None:
    """The lifecycle record for one finished run or resolved incident, or None if it is gone."""
    params = {"p": project_id, "i": source_id}
    if source_kind == "run":
        row = connection.execute(text(
            "SELECT title, status, spec, state, pull_request_url, error FROM code_task_runs "
            "WHERE id = :i AND project_id = :p"), params).mappings().first()
        if row is None:
            return None
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        state = row["state"] if isinstance(row["state"], dict) else {}
        body = "\n".join(part for part in (
            f"Run {source_id} {row['status']}: {row['title']}",
            f"Instructions: {_excerpt(spec.get('instructions'))}" if spec.get("instructions") else "",
            f"Outcome: {_excerpt(state.get('summary') or state.get('diff_summary'))}"
            if (state.get("summary") or state.get("diff_summary")) else "",
            f"Pull request: {row['pull_request_url']}" if row["pull_request_url"] else "",
            f"Error: {_excerpt(row['error'], 600)}" if row["error"] else "",
        ) if part)
        return {"kind": "run", "source_id": source_id, "title": row["title"], "body": body[:MAX_BODY_CHARS]}
    if source_kind == "incident":
        row = connection.execute(text(
            "SELECT title, service, severity, status, finalize_ctx FROM incidents "
            "WHERE id = :i AND project_id = :p"), params).mappings().first()
        if row is None:
            return None
        body = "\n".join(part for part in (
            f"Incident {source_id} {row['status']} ({row['severity']}) on {row['service'] or 'unknown service'}: "
            f"{row['title']}",
            f"Resolution: {_excerpt(row['finalize_ctx'])}" if row["finalize_ctx"] else "",
        ) if part)
        return {"kind": "incident", "source_id": source_id, "title": row["title"], "body": body[:MAX_BODY_CHARS]}
    raise ValueError(f"unknown ingest source {source_kind!r}")


def _ingest_one(engine: Any, project_id: str, source_kind: str, source_id: str) -> str:
    from .capture import capture_lifecycle
    from .store import PostgresKnowledgeStore

    with engine.begin() as connection:
        record = record_for(connection, project_id, source_kind, source_id)
        if record is not None:
            capture_lifecycle(connection, PostgresKnowledgeStore(), project_id=project_id, record=record)
        connection.execute(text(
            "DELETE FROM kg_ingest_queue WHERE project_id = :p AND source_kind = :k AND source_id = :i"),
            {"p": project_id, "k": source_kind, "i": source_id})
    return "captured" if record is not None else "gone"


def _drain(engine: Any, rows) -> dict[str, int]:
    out = {"captured": 0, "gone": 0, "failed": 0}
    for project_id, source_kind, source_id in rows:
        try:
            out[_ingest_one(engine, project_id, source_kind, source_id)] += 1
        except Exception as error:  # noqa: BLE001 - one bad record never stops the rest
            out["failed"] += 1
            with engine.begin() as connection:
                connection.execute(text(
                    """
                    INSERT INTO kg_ingest_queue (project_id, source_kind, source_id, attempts, last_error)
                    VALUES (:p, :k, :i, 1, :e)
                    ON CONFLICT (project_id, source_kind, source_id) DO UPDATE SET
                      attempts = kg_ingest_queue.attempts + 1, last_error = EXCLUDED.last_error
                    """), {"p": project_id, "k": source_kind, "i": source_id,
                           "e": f"{type(error).__name__}: {str(error)[:200]}"})
    return out


def ingest_queued(engine: Any, *, limit: int = 20) -> dict[str, int]:
    """The event-driven half: what the finish triggers queued, oldest first."""
    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT project_id, source_kind, source_id FROM kg_ingest_queue "
            "WHERE attempts < :max ORDER BY finished_at LIMIT :n"),
            {"max": MAX_ATTEMPTS, "n": limit}).all()
    return _drain(engine, [tuple(r) for r in rows])


def ingest_due(engine: Any, *, limit: int = 20, days: int = SWEEP_DAYS) -> dict[str, int]:
    """The fallback sweep: finished runs and resolved incidents with no capture yet."""
    with engine.begin() as connection:
        rows = connection.execute(text(
            """
            SELECT * FROM (
              SELECT r.project_id, 'run' AS kind, r.id, r.updated_at AS at FROM code_task_runs r
              WHERE r.status IN ('completed', 'failed') AND r.updated_at > now() - make_interval(days => :d)
              UNION ALL
              SELECT i.project_id, 'incident', i.id, i.last_seen FROM incidents i
              WHERE i.status = 'resolved' AND i.last_seen > now() - make_interval(days => :d)
            ) s
            WHERE NOT EXISTS (SELECT 1 FROM kg_captures c WHERE c.project_id = s.project_id
                                AND c.source_kind = s.kind AND c.source_id = s.id)
              AND NOT EXISTS (SELECT 1 FROM kg_ingest_queue q WHERE q.project_id = s.project_id
                                AND q.source_kind = s.kind AND q.source_id = s.id)
            ORDER BY at LIMIT :n
            """), {"d": days, "n": limit}).all()
    return _drain(engine, [(r[0], r[1], r[2]) for r in rows])


def tick(engine: Any, *, sweep: bool = False) -> dict[str, Any]:
    """The worker's call: drain the queue; on a sweep tick, also catch what the triggers missed."""
    out: dict[str, Any] = {"queued": ingest_queued(engine)}
    if sweep:
        out["sweep"] = ingest_due(engine)
        out["projection"] = reconcile_projections(engine)
    return out


def reconcile_projections(engine: Any) -> dict[str, int]:
    """Rebuild any project's AGE projection whose counts drifted from the tables."""
    from .age import available, reconcile

    out = {"current": 0, "rebuilt": 0, "failed": 0}
    with engine.begin() as connection:
        if not available(connection):
            return out
        projects = connection.execute(text("SELECT DISTINCT project_id FROM kg_nodes")).scalars().all()
    for project_id in projects:
        try:
            with engine.begin() as connection:
                done = reconcile(connection, project_id)
            out["current" if done.get("mode") == "current" else "rebuilt"] += 1
        except Exception:  # noqa: BLE001 - one project never blocks the others
            out["failed"] += 1
    return out
