"""Confluence as a LIVE knowledge source (BOSSAPP-1384).

The operator picks the spaces (``knowledge_spaces`` on the org's Confluence connection). A sync
indexes each page into the project's knowledge store with its provenance — space, page id,
version, URL and last-modified — and re-syncs only what changed: a page whose version is the one
already held is skipped, a newer one is captured again and its node updated in place.

Pages are scrubbed (services/knowledge/scrub) before they are stored, exactly like every other
connector document, and written as ``spec`` nodes with trust tier ``observed`` (they are
somebody's words, not governed facts). The first sync of a space reads everything (up to the page
cap); later syncs ask Confluence only for pages modified since the last one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.tickets.contract import TicketSystemError
from . import docs_connections
from .knowledge.sources import accept_connector_document
from .knowledge.store import PostgresKnowledgeStore

SOURCE = "confluence"
MAX_PAGES_PER_SPACE = 300
NODE_BODY_CHARS = 4000


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


def sync_space(db: Connection, store: PostgresKnowledgeStore, client: Any, *, project_id: str,
               space: str, since: Optional[str], limit: int = MAX_PAGES_PER_SPACE) -> dict[str, Any]:
    site = _site(client.base)
    summaries = client.changed_since(space, since, limit=limit)
    done = {"space": space, "seen": len(summaries), "indexed": 0, "updated": 0, "unchanged": 0,
            "failed": []}
    for summary in summaries:
        key = node_key(site, summary.id)
        try:
            page = client.get_page(summary.id, labels=True)
            held = _held_version(db, project_id, key)
            if held is not None and held >= page.version:
                done["unchanged"] += 1
                continue
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
            done["updated" if held is not None else "indexed"] += 1
        except TicketSystemError as error:
            done["failed"].append({"page_id": summary.id, "error": str(error)[:200]})
    return done


def sync(db: Connection, app_settings: Any, *, project_id: str,
         spaces: Optional[list[str]] = None, full: bool = False, actor: str = "system"
         ) -> dict[str, Any]:
    """Sync the chosen spaces (default: the connection's ``knowledge_spaces``) into the project's
    knowledge store. Returns counts per space; records the sync time per space for the next."""
    row, client = docs_connections.client_for(db, app_settings, project_id)
    chosen = docs_connections._spaces(spaces if spaces is not None  # noqa: SLF001
                                      else row["configuration"].get("knowledge_spaces"))
    if not chosen:
        raise docs_connections.DocsConnectionError(
            "invalid_request", "Pick the Confluence spaces to keep in knowledge first "
                              "(Setup › Connections › Confluence).")
    store = PostgresKnowledgeStore()
    state = dict(row["configuration"].get("sync_state") or {})
    results = []
    for space in chosen:
        started = datetime.now(timezone.utc)
        since = None if full else (state.get(space) or {}).get("synced_at")
        out = sync_space(db, store, client, project_id=project_id, space=space,
                         since=since)
        if not out["failed"]:
            state[space] = {"synced_at": started.strftime("%Y-%m-%d %H:%M"),
                            "pages": out["seen"] + (state.get(space) or {}).get("pages", 0)
                            if since else out["seen"]}
        results.append(out)
    db.execute(text("UPDATE docs_connections SET configuration = configuration || "
                    "jsonb_build_object('sync_state', CAST(:s AS JSONB)), updated_at = now() "
                    "WHERE id = :i"), {"s": json.dumps(state), "i": row["id"]})
    from .core import emit_event

    emit_event(db, project_id, "knowledge.confluence_synced", actor, "docs_connection", row["id"],
               {"spaces": chosen, "indexed": sum(r["indexed"] for r in results),
                "updated": sum(r["updated"] for r in results),
                "unchanged": sum(r["unchanged"] for r in results)})
    return {"ok": True, "spaces": results}
