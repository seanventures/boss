"""Event-driven lifecycle ingest (BOSSAPP-622; operator decision 2026-10-21, ported from PR #13).

A database trigger queues a code-task run the moment its status becomes completed or failed,
and an incident the moment it becomes resolved (``kg_ingest_queue``), whatever code path
finished it. The worker drains the queue every ``QUEUE_POLL_SECONDS`` through
POST /v1/knowledge/ingest/tick; every ``SWEEP_SECONDS`` the same call also sweeps the last
``SWEEP_DAYS`` for anything the triggers missed (a record finished before the trigger existed),
and reconciles each project's AGE projection with the tables (knowledge/age.py). Every tick
also embeds what a project's embedding model has not embedded yet (embeddings.embed_due).

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
    if record is not None and source_kind == "incident":
        # A resolved incident is new evidence for the Recommended roadmap: re-evaluate the
        # incident detectors now (event-driven), whoever resolved it.
        from ..ambient import roadmap

        roadmap.on_event(engine, project_id=project_id, evidence_source="incident")
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


def ingest_queued(engine: Any, *, limit: int = 20, project_id: str | None = None) -> dict[str, int]:
    """The event-driven half: what the finish triggers queued, oldest first. An archived
    project's rows stay queued until it is restored (projects_archive)."""
    from .. import projects_archive

    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT project_id, source_kind, source_id FROM kg_ingest_queue "
            "WHERE attempts < :max AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) "
            "AND " + projects_archive.live_sql("project_id") + " "
            "ORDER BY finished_at LIMIT :n"),
            {"max": MAX_ATTEMPTS, "n": limit, "p": project_id}).all()
    return _drain(engine, [tuple(r) for r in rows])


def ingest_due(engine: Any, *, limit: int = 20, days: int = SWEEP_DAYS,
               project_id: str | None = None) -> dict[str, int]:
    """The fallback sweep: finished runs and resolved incidents with no capture yet (an
    archived project's are left for after its restore, projects_archive)."""
    from .. import projects_archive

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
            WHERE (CAST(:p AS TEXT) IS NULL OR s.project_id = :p)
              AND """ + projects_archive.live_sql("s.project_id") + """
              AND NOT EXISTS (SELECT 1 FROM kg_captures c WHERE c.project_id = s.project_id
                                AND c.source_kind = s.kind AND c.source_id = s.id)
              AND NOT EXISTS (SELECT 1 FROM kg_ingest_queue q WHERE q.project_id = s.project_id
                                AND q.source_kind = s.kind AND q.source_id = s.id)
            ORDER BY at LIMIT :n
            """), {"d": days, "n": limit, "p": project_id}).all()
    return _drain(engine, [(r[0], r[1], r[2]) for r in rows])


def tick(engine: Any, *, sweep: bool = False, project_id: str | None = None) -> dict[str, Any]:
    """The worker's call: drain the queue; on a sweep tick, also catch what the triggers missed.
    ``project_id`` narrows it to one project (every project when None). An archived project is
    skipped by every part of it (projects_archive)."""
    from .. import projects_archive
    from .embeddings import embed_due

    if project_id:
        with engine.begin() as connection:
            if projects_archive.is_archived(connection, project_id):
                return projects_archive.skipped(project_id)
    out: dict[str, Any] = {"queued": ingest_queued(engine, project_id=project_id)}
    # Registered repositories whose code graph is due (repo_index.py): one governed index job
    # each. Its own transaction; a failure here never holds the lifecycle ingest back.
    try:
        from ...config import get_settings
        from .repo_index import enqueue_due

        out["repositories"] = enqueue_due(engine, get_settings(), project_id=project_id, sweep=sweep)
    except Exception as error:  # noqa: BLE001 - reported in the tick's answer, retried next tick
        out["repositories"] = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    # Re-embedding runs in the background here: what the project's embedding slot lacks, after
    # a new model or dimension dropped the old space (embeddings.embed_due). Governed calls.
    out["embedded"] = embed_due(engine, project_id=project_id)
    if sweep:
        out["sweep"] = ingest_due(engine, project_id=project_id)
        out["usage_edges"] = derive_usage_edges(engine, project_id=project_id)
        out["projection"] = reconcile_projections(engine, project_id=project_id)
    return out


def derive_usage_edges(engine: Any, *, project_id: str | None = None) -> dict[str, int]:
    """The sweep's half of "consulted" and "reused" (spec §2.5): every project's uses that have
    no edge yet — a workflow run is never captured as a run record, and a tool may log a use
    before anything derives its edge. Idempotent; one project's failure never stops the rest."""
    from .. import projects_archive
    from .learning_writeback import sync_usage_edges

    out = {"projects": 0, "edges": 0, "failed": 0}
    with engine.begin() as connection:
        projects = projects_archive.live_ids(connection, [project_id] if project_id else connection.execute(
            text("SELECT DISTINCT project_id FROM kg_usage WHERE use_kind <> 'shown' "
                 "AND created_at > now() - make_interval(days => :d)"), {"d": SWEEP_DAYS}).scalars().all())
    for pid in projects:
        try:
            with engine.begin() as connection:
                out["edges"] += sync_usage_edges(connection, project_id=pid)
            out["projects"] += 1
        except Exception:  # noqa: BLE001 - one project never blocks the others
            out["failed"] += 1
    return out


def reconcile_projections(engine: Any, *, project_id: str | None = None) -> dict[str, Any]:
    """Rebuild any project's AGE projection whose counts drifted from the tables, and any Graph
    area rollup (view_rollup.py) whose node count drifted from the live code graph (not an
    archived project's: projects_archive)."""
    from .. import projects_archive
    from .age import available, reconcile
    from .view_rollup import reconcile as reconcile_views

    out: dict[str, Any] = {"current": 0, "rebuilt": 0, "failed": 0,
                           "views": reconcile_views(engine, project_id=project_id)}
    with engine.begin() as connection:
        if not available(connection):
            return out
        projects = projects_archive.live_ids(connection, [project_id] if project_id else connection.execute(
            text("SELECT DISTINCT project_id FROM kg_nodes")).scalars().all())
    for project_id in projects:
        try:
            with engine.begin() as connection:
                done = reconcile(connection, project_id)
            out["current" if done.get("mode") == "current" else "rebuilt"] += 1
        except Exception:  # noqa: BLE001 - one project never blocks the others
            out["failed"] += 1
    return out
