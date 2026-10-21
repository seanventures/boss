"""Enterprise source ingest.

ADR folders are read from a local path. Confluence, Jira, and Slack connectors
accept already-fetched documents and refuse to open a network themselves.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Connection

from .scrub import scrub
from .store import PostgresKnowledgeStore

LIVE_CONNECTORS = frozenset({"confluence", "jira", "slack"})


def ingest_adr_dir(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    root: Path,
) -> dict:
    if not root.is_dir():
        raise FileNotFoundError(f"ADR directory not found: {root}")
    written = []
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        cleaned, counts = scrub(text)
        capture = store.capture(
            connection,
            project_id=project_id,
            source_kind="adr",
            source_id=str(path.relative_to(root)),
            body=cleaned,
            scrub_counts=counts,
        )
        node_id = store.upsert_node(
            connection,
            project_id=project_id,
            kind="spec",
            node_key=f"adr:{path.relative_to(root)}",
            name=path.stem,
            body=cleaned[:4000],
            trust_tier="observed",
            actor_kind="system",
            source="adr",
            attrs={"capture_id": capture["id"]},
        )
        written.append({"capture_id": capture["id"], "node_id": node_id, "replayed": capture["replayed"]})
    return {"ingested": len(written), "documents": written}


def accept_connector_document(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    connector: str,
    document_id: str,
    body: str,
) -> dict:
    if connector not in LIVE_CONNECTORS:
        raise ValueError(f"unknown connector {connector!r}")
    cleaned, counts = scrub(body)
    capture = store.capture(
        connection,
        project_id=project_id,
        source_kind=connector,
        source_id=document_id,
        body=cleaned,
        scrub_counts=counts,
    )
    return {"capture_id": capture["id"], "replayed": capture["replayed"], "scrub_counts": counts}


def refuse_live_fetch(connector: str) -> None:
    """Connectors do not open sockets. A caller must hand in the document."""
    raise RuntimeError(f"{connector} live fetch is not executed by the knowledge plane")
