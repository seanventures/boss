"""Confluence as a LIVE knowledge source (BOSSAPP-1384).

The operator picks the spaces (``knowledge_spaces`` on the org's Confluence connection). A sync
indexes each page into the project's knowledge store with its provenance — space, page id,
version, URL and last-modified — and re-syncs only what changed: a page whose version is the one
already held is skipped, a newer one is captured again and its node updated in place.

Pages are scrubbed (services/knowledge/scrub) before they are stored, exactly like every other
connector document, and written as ``spec`` nodes with trust tier ``observed`` (they are
somebody's words, not governed facts). The first sync of a space reads everything (up to the page
cap); later syncs ask Confluence only for pages modified since the last one.

THE SYNC IS A BACKGROUND JOB, never a request: :func:`start` claims the one job of the org's
connection (``configuration.sync_jobs[<project>]``: status, a heartbeat, per-space progress) and runs it in a
thread (:func:`run`) whose every database step is its own short transaction, with the Confluence
HTTP calls made between them. A job whose heartbeat stopped (a restart) is taken over by the next
``start``, which RESUMES: spaces already done are skipped, and pages already held at their
version are skipped inside the rest.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.tickets.contract import TicketSystemError
from . import docs_connections
from .core import make_id
from .knowledge.sources import accept_connector_document
from .knowledge.store import PostgresKnowledgeStore

SOURCE = "confluence"
MAX_PAGES_PER_SPACE = 300
NODE_BODY_CHARS = 4000


logger = logging.getLogger(__name__)
#: "thread": the job runs in a background thread. "manual": the caller runs :func:`run` (tests).
DISPATCH = "thread"
STALE = "5 minutes"


def node_key(site: str, page_id: str) -> str:
    return f"confluence:{site}:{page_id}"


def _site(base_url: str) -> str:
    return (base_url or "").replace("https://", "").replace("http://", "").split("/")[0]


def _held_version(db: Connection, project_id: str, key: str) -> Optional[int]:
    attrs = db.execute(text("SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'spec' "
                            "AND node_key = :k"), {"p": project_id, "k": key}).scalar()
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    try:
        return int((attrs or {}).get("version"))
    except (TypeError, ValueError):
        return None


class JobOvertaken(Exception):
    """A newer job took over this project's sync; this one stops writing."""


_PUT = ("UPDATE docs_connections SET configuration = jsonb_set(CASE WHEN configuration ? :sec "
        "THEN configuration ELSE configuration || jsonb_build_object(CAST(:sec AS TEXT), "
        "'{}'::jsonb) END, ARRAY[CAST(:sec AS TEXT), CAST(:p AS TEXT)], CAST(:v AS JSONB), true), "
        "updated_at = now() WHERE id = :i")


def _put(db: Any, row_id: str, section: str, project_id: str, value: dict[str, Any],
         *, only_job: Optional[str] = None) -> bool:
    """Write ``configuration[section][project]``. Sync state and job are PER PROJECT (knowledge is
    per project; one project's cursor never decides what another is missing). With ``only_job`` the
    write happens only while that job is still the project's current one."""
    guard = (" AND configuration->'sync_jobs'->CAST(:p AS TEXT)->>'id' = :jid" if only_job else "")
    return db.execute(text(_PUT + guard), {"sec": section, "p": project_id, "i": row_id,
                                           "v": json.dumps(value), "jid": only_job}).rowcount > 0


def _save(engine: Any, row_id: str, project_id: str, job: dict[str, Any]) -> None:
    """Record progress, only while this job is still the project's current one (a job taken over
    after a stalled heartbeat stops here instead of overwriting the newer job's progress)."""
    job["heartbeat"] = datetime.now(timezone.utc).isoformat()
    with engine.begin() as db:
        if not _put(db, row_id, "sync_jobs", project_id, job, only_job=job["id"]):
            raise JobOvertaken(job["id"])


def _job_of(config: dict[str, Any], project_id: str) -> dict[str, Any]:
    return dict(((config.get("sync_jobs") or {}).get(project_id)) or {})


def _state_of(config: dict[str, Any], project_id: str) -> dict[str, Any]:
    return dict(((config.get("sync_states") or {}).get(project_id)) or {})


def status(db: Connection, project_id: str) -> dict[str, Any]:
    """The sync job of the org's connection as this project sees it (or {} when none ran)."""
    row = docs_connections.get_row(db, project_id, include_disconnected=True)
    job = _job_of((row or {}).get("configuration", {}), project_id)
    if job.get("status") == "running":
        beat = job.get("heartbeat")
        stale = db.execute(text(f"SELECT CAST(:b AS timestamptz) < now() - interval '{STALE}'"),
                           {"b": beat}).scalar() if beat else True
        if stale:
            job["status"] = "stalled"
    return job


def start(engine: Any, app_settings: Any, *, project_id: str, spaces: Optional[list[str]] = None,
          full: bool = False, actor: str = "system") -> dict[str, Any]:
    """Claim the connection's one sync job and start it. Returns the job (status ``running``);
    when one is already running (heartbeat fresh) it is returned as it is, nothing new starts."""
    with engine.begin() as db:
        row, _client = docs_connections.client_for(db, app_settings, project_id)
        chosen = docs_connections._spaces(spaces if spaces is not None  # noqa: SLF001
                                          else row["configuration"].get("knowledge_spaces"))
        if not chosen:
            raise docs_connections.DocsConnectionError(
                "invalid_request", "Pick the Confluence spaces to keep in knowledge first "
                                  "(Setup › Connections › Confluence).")
        previous = _job_of(row["configuration"], project_id)
        resume = (not full and previous.get("status") in ("running", "failed")
                  and sorted(previous.get("spaces") or {}) == sorted(chosen))
        carried = {k: v for k, v in (previous.get("spaces") or {}).items()
                   if resume and v.get("status") == "done"}
        job = {"id": make_id("sjob"), "status": "running", "project_id": project_id,
               "actor": actor, "full": full, "started_at": datetime.now(timezone.utc).isoformat(),
               "heartbeat": datetime.now(timezone.utc).isoformat(), "resumed": bool(carried),
               "spaces": {sp: carried.get(sp) or {"status": "waiting"} for sp in chosen}}
        mine = "configuration->'sync_jobs'->CAST(:p AS TEXT)"
        claimed = db.execute(text(
            _PUT + f" AND ({mine} IS NULL OR {mine}->>'status' <> 'running' OR "
            f"({mine}->>'heartbeat')::timestamptz < now() - interval '{STALE}') RETURNING id"),
            {"sec": "sync_jobs", "p": project_id, "i": row["id"], "v": json.dumps(job)}).first()
        if claimed is None:
            return dict(_job_of(row["configuration"], project_id), already_running=True)
        from .core import emit_event

        emit_event(db, project_id, "knowledge.confluence_sync_started", actor, "docs_connection",
             row["id"], {"job": job["id"], "spaces": chosen, "full": full})
    if DISPATCH == "thread":
        from ..db import spawn_thread

        spawn_thread(_safe_run, args=(engine, app_settings, project_id, job["id"]),
                     name="confluence-sync", daemon=True, as_background=True)
    return job


def _safe_run(engine: Any, app_settings: Any, project_id: str, job_id: str) -> None:
    try:
        # Catch-up: an approved Confluence page whose write never finished is finished first.
        from . import confluence_publish

        confluence_publish.reconcile_pending(engine, app_settings, project_id=project_id)
    except Exception:  # noqa: BLE001
        logger.warning("confluence publish catch-up failed", exc_info=True)
    try:
        run(engine, app_settings, project_id, job_id)
    except Exception:  # noqa: BLE001 — recorded on the job
        logger.warning("confluence sync failed", exc_info=True)


def run(engine: Any, app_settings: Any, project_id: str, job_id: str) -> dict[str, Any]:
    """The job body. Every database step is its own short transaction; HTTP runs between them."""
    with engine.begin() as db:
        row, client = docs_connections.client_for(db, app_settings, project_id)
    job = _job_of(row["configuration"], project_id)
    if job.get("id") != job_id:
        return job
    store = PostgresKnowledgeStore()
    state = _state_of(row["configuration"], project_id)
    site = _site(client.base)
    try:
        for space, progress in job["spaces"].items():
            if progress.get("status") == "done":
                continue
            started = datetime.now(timezone.utc)
            since = None if job.get("full") else (state.get(space) or {}).get("synced_at")
            progress.update(status="running", seen=0, indexed=0, updated=0, unchanged=0, failed=[])
            _save(engine, row["id"], project_id, job)
            summaries = client.changed_since(space, since, limit=MAX_PAGES_PER_SPACE)
            progress["seen"] = len(summaries)
            for n, summary in enumerate(summaries, 1):
                _one_page(engine, store, client, project_id, site, space, summary, progress)
                if n % 10 == 0:
                    _save(engine, row["id"], project_id, job)
            progress["status"] = "done"
            if not progress["failed"]:
                state[space] = {"synced_at": started.strftime("%Y-%m-%d %H:%M"),
                                "pages": progress["seen"] + ((state.get(space) or {}).get("pages", 0)
                                                             if since else 0)}
            _save(engine, row["id"], project_id, job)
        job["status"] = "done"
    except JobOvertaken:
        return job            # a newer job owns this project's sync now; write nothing more
    except Exception as error:  # noqa: BLE001 — a failed job keeps what it finished, for a resume
        job["status"] = "failed"
        job["error"] = str(error)[:300]
    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _save(engine, row["id"], project_id, job)
    except JobOvertaken:
        return job
    with engine.begin() as db:
        _put(db, row["id"], "sync_states", project_id, state, only_job=job_id)
        from .core import emit_event

        spaces = job["spaces"].values()
        emit_event(db, project_id, "knowledge.confluence_synced", job.get("actor") or "system",
                   "docs_connection", row["id"],
                   {"job": job_id, "status": job["status"], "spaces": list(job["spaces"]),
                    "indexed": sum(v.get("indexed", 0) for v in spaces),
                    "updated": sum(v.get("updated", 0) for v in spaces),
                    "unchanged": sum(v.get("unchanged", 0) for v in spaces)})
    return job


def _one_page(engine: Any, store: PostgresKnowledgeStore, client: Any, project_id: str, site: str,
              space: str, summary: Any, progress: dict[str, Any]) -> None:
    key = node_key(site, summary.id)
    try:
        page = client.get_page(summary.id, labels=True)            # HTTP: no transaction open
        with engine.begin() as db:
            held = _held_version(db, project_id, key)
            if held is not None and held >= page.version:
                progress["unchanged"] += 1
                return
            capture = accept_connector_document(
                db, store, project_id=project_id, connector=SOURCE,
                document_id=f"{site}:{page.id}:v{page.version}", body=page.body_markdown)
            store.upsert_node(
                db, project_id=project_id, kind="spec", node_key=key, name=page.title,
                body=page.body_markdown[:NODE_BODY_CHARS], trust_tier="observed",
                actor_kind="system", source=SOURCE,
                attrs={"source": SOURCE, "site": site, "space": page.space_key or space,
                       "page_id": page.id, "version": page.version, "url": page.url,
                       "last_modified": page.last_modified, "labels": page.labels,
                       "truncated": page.truncated, "capture_id": capture["capture_id"]})
        progress["updated" if held is not None else "indexed"] += 1
    except TicketSystemError as error:
        progress["failed"].append({"page_id": summary.id, "error": str(error)[:200]})
