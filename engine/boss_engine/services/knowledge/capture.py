"""Lifecycle capture. BOSS records become nodes linked to the code they touched.

Each record is scrubbed, then stored once for a source id and body. Replays
converge. A symbol that is not in this project is not linked.

A pull request that carries the files it wrote links exactly the symbols those files define,
through ``kg_change_links`` (natural keys, durable). Its ``changed`` edges are derived from those
links, again after every re-index (``sync_change_links``), so a symbol that was closed, replaced
or forgotten gets its link back when it is in the graph again.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .code_index import index_files, symbol_keys_for
from .scrub import contains_secret_marker, scrub
from .store import PostgresKnowledgeStore

LIFECYCLE_KINDS = frozenset({
    "spec", "decision", "pull_request", "review_finding", "release",
    "runbook", "alert_rule", "run", "incident",
})

# Records whose files name exactly the symbols they changed (kg_change_links).
_CHANGE_KINDS = frozenset({"pull_request"})

# The edge a record draws toward a symbol it names.
_EDGE_FOR_KIND = {
    "pull_request": "changed",
    "review_finding": "found_in",
    "release": "shipped",
    "spec": "specifies",
    "decision": "about",
    "run": "implements",
    "incident": "about",
    "runbook": "about",
    "alert_rule": "fired_on",
}


def capture_lifecycle(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    kind = str(record.get("kind") or "").strip()
    if kind not in LIFECYCLE_KINDS:
        raise ValueError(f"unknown lifecycle kind {kind!r}")
    source_id = str(record.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("source_id is required")
    title = str(record.get("title") or source_id).strip() or source_id
    body, counts = scrub(str(record.get("body") or ""))
    if contains_secret_marker(body):
        raise RuntimeError("scrub left a secret shape in the capture")
    files = record.get("files") or []
    indexed = None
    changed_keys: list[str] = []
    if files:
        indexed = index_files(connection, store, project_id=project_id, files=list(files))
        if kind in _CHANGE_KINDS:
            changed_keys = symbol_keys_for(list(files))
    captured = store.capture(
        connection,
        project_id=project_id,
        source_kind=kind,
        source_id=source_id,
        body=body,
        scrub_counts=counts,
    )
    actor_ref = str(record.get("actor_ref") or "").strip()
    if kind == "decision" and actor_ref:
        trust_tier, actor_kind = "governed", "person"
    else:
        trust_tier, actor_kind = "observed", "system"
    node_id = store.upsert_node(
        connection,
        project_id=project_id,
        kind=kind,
        node_key=f"{kind}:{source_id}",
        name=title,
        body=body,
        trust_tier=trust_tier,
        actor_kind=actor_kind,
        source="lifecycle",
        attrs={"source_id": source_id, "capture_id": captured["id"]},
    )
    linked: list[str] = []
    edge_kind = _EDGE_FOR_KIND.get(kind)
    if changed_keys:
        store.record_change_links(
            connection, project_id=project_id, source_kind=kind, source_id=source_id,
            symbol_keys=changed_keys,
        )
        linked = sync_change_links(
            connection, store, project_id=project_id, source_kind=kind, source_id=source_id
        )
    elif edge_kind:
        for raw_name in record.get("symbol_names") or []:
            symbol_id = _symbol_in_project(connection, project_id, str(raw_name))
            if symbol_id is None:
                continue
            store.upsert_edge(
                connection,
                project_id=project_id,
                src_id=node_id,
                dst_id=symbol_id,
                kind=edge_kind,
                actor_kind=actor_kind,
            )
            linked.append(symbol_id)
    if kind == "decision" and actor_ref:
        person_id = store.upsert_node(
            connection,
            project_id=project_id,
            kind="person",
            node_key=f"person:{actor_ref}",
            name=actor_ref,
            body="",
            trust_tier="observed",
            actor_kind="system",
            source="lifecycle",
            attrs={"ref": actor_ref},
        )
        store.upsert_edge(
            connection,
            project_id=project_id,
            src_id=node_id,
            dst_id=person_id,
            kind="decided_by",
            actor_kind="person",
        )
    from .age import after_write

    after_write(connection, project_id)
    return {
        "id": node_id,
        "replayed": bool(captured["replayed"]),
        "linked": linked,
        "scrub_counts": counts,
        "indexed": indexed,
    }


def sync_change_links(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    source_kind: str | None = None,
    source_id: str | None = None,
) -> list[str]:
    """Derive ``changed`` edges from the durable links; returns the linked symbol ids.

    An edge the links imply is written, and reopened if a re-index had closed it.
    """
    linked: list[str] = []
    for row in store.change_links(
        connection, project_id=project_id, source_kind=source_kind, source_id=source_id
    ):
        edge_id = store.upsert_edge(
            connection,
            project_id=project_id,
            src_id=row["record_id"],
            dst_id=row["symbol_id"],
            kind=_EDGE_FOR_KIND.get(row["source_kind"], "changed"),
            actor_kind="system",
        )
        connection.execute(
            text("UPDATE kg_edges SET valid_to = NULL WHERE id = :id AND valid_to IS NOT NULL "
                 "AND trust_tier = 'observed'"),
            {"id": edge_id},
        )
        linked.append(row["symbol_id"])
    return linked


def _symbol_in_project(connection: Connection, project_id: str, name: str) -> str | None:
    cleaned = name.strip()
    if not cleaned:
        return None
    row = connection.execute(
        text(
            """
            SELECT id FROM kg_nodes
            WHERE project_id = :project_id AND kind = 'symbol' AND name = :name
              AND (valid_to IS NULL OR valid_to > now())
            ORDER BY node_key
            LIMIT 1
            """
        ),
        {"project_id": project_id, "name": cleaned},
    ).first()
    return None if row is None else str(row[0])
