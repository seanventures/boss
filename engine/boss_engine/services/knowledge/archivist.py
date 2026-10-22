"""Archivist pipeline: distil, resolve, trust, retain, forget.

Extraction is injected. A scripted extractor spends nothing. A spender is
refused unless a budget gate allows the `knowledge` category. The tier is
chosen here, never from the payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from sqlalchemy.engine import Connection

from .scrub import contains_secret_marker, scrub
from .store import PostgresKnowledgeStore

FACT_KINDS = frozenset({"invariant", "decision", "lesson", "pattern", "coupling", "ownership", "risk"})
BUDGET_CATEGORY = "knowledge"
PROMOTION_SUCCESSES = 3


class Extractor(Protocol):
    def extract(self, text: str) -> list[dict]: ...


@dataclass(frozen=True)
class ScriptedExtractor:
    facts: tuple[dict, ...]
    spends: bool = False

    def extract(self, text: str) -> list[dict]:
        return [dict(item) for item in self.facts]


@dataclass(frozen=True)
class SpendingExtractor:
    """A model extractor. Calling it without a budget grant is a bug."""

    spends: bool = True

    def extract(self, text: str) -> list[dict]:
        raise RuntimeError("model extraction requires an approved knowledge budget")


def _fact_key(subject: str, predicate: str, obj: str) -> str:
    return f"fact:{subject}|{predicate}|{obj}"


def distil(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    capture_id: str,
    text: str,
    extractor: Extractor,
    budget_allows: bool = True,
) -> dict[str, Any]:
    cleaned, counts = scrub(text)
    if contains_secret_marker(cleaned):
        raise RuntimeError("scrub left a secret shape in the capture")
    spends = bool(getattr(extractor, "spends", False))
    if spends and not budget_allows:
        store.audit(
            connection,
            project_id=project_id,
            action="knowledge.structure_only",
            entity_id=capture_id,
            actor_kind="system",
            detail={"budget_category": BUDGET_CATEGORY, "scrub_counts": counts},
        )
        return {"mode": "structure_only", "fact_ids": [], "scrub_counts": counts}
    raw_facts = extractor.extract(cleaned)
    fact_ids: list[str] = []
    for raw in raw_facts:
        kind = str(raw.get("kind") or "")
        if kind not in FACT_KINDS:
            raise ValueError(f"unknown fact kind {kind!r}")
        subject = str(raw.get("subject") or "").strip()
        predicate = str(raw.get("predicate") or "").strip()
        obj = str(raw.get("object") or "").strip()
        statement = str(raw.get("statement") or "").strip()
        if not subject or not predicate or not obj or not statement:
            raise ValueError("a fact needs subject, predicate, object, and statement")
        statement, more = scrub(statement)
        for label, count in more.items():
            counts[label] = counts.get(label, 0) + count
        node_key = _fact_key(subject, predicate, obj)
        previous = _open_same_predicate(connection, store, project_id, subject, predicate, obj)
        previous_node = (
            store.get_node(connection, project_id=project_id, node_id=previous, include_closed=True)
            if previous
            else None
        )
        fact_id = store.upsert_node(
            connection,
            project_id=project_id,
            kind="fact",
            node_key=node_key,
            name=f"{subject} {predicate} {obj}",
            body=statement,
            trust_tier="inferred",
            actor_kind="agent",
            source="archivist",
            attrs={
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "kind": kind,
                "evidence": [capture_id],
                "capture_id": capture_id,
                "confidence": float(raw.get("confidence") or 0.5),
            },
        )
        protected = previous_node and previous_node["trust_tier"] in {"governed", "approved"}
        if previous and previous != fact_id and not protected:
            store.close_validity(
                connection,
                project_id=project_id,
                node_id=previous,
                valid_to=datetime.now(timezone.utc),
                actor_kind="agent",
                superseded_by=fact_id,
            )
        _link_symbol(connection, store, project_id, fact_id, subject)
        _link_symbol(connection, store, project_id, fact_id, obj)
        fact_ids.append(fact_id)
    store.audit(
        connection,
        project_id=project_id,
        action="knowledge.distil",
        entity_id=capture_id,
        actor_kind="agent",
        detail={"fact_ids": fact_ids, "scrub_counts": counts, "budget_category": BUDGET_CATEGORY},
    )
    return {"mode": "distilled", "fact_ids": fact_ids, "scrub_counts": counts}


def _open_same_predicate(connection, store, project_id, subject, predicate, obj) -> Optional[str]:
    from sqlalchemy import text

    store._org_id(connection, project_id, None)
    rows = connection.execute(
        text(
            """
            SELECT id, node_key FROM kg_nodes
            WHERE project_id = :project_id AND kind = 'fact'
              AND node_key LIKE :prefix
              AND (valid_to IS NULL OR valid_to > now())
            """
        ),
        {"project_id": project_id, "prefix": f"fact:{subject}|{predicate}|%"},
    ).mappings().all()
    for row in rows:
        if row["node_key"] != _fact_key(subject, predicate, obj):
            return row["id"]
    return None


def _link_symbol(connection, store, project_id, fact_id, name) -> None:
    from sqlalchemy import text

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
        {"project_id": project_id, "name": name},
    ).scalar()
    if row:
        store.upsert_edge(
            connection,
            project_id=project_id,
            src_id=fact_id,
            dst_id=row,
            kind="about",
            actor_kind="agent",
        )


def forget_source(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    source_id: str,
    actor_kind: str,
) -> dict[str, Any]:
    fact_ids = store.facts_about_source(
        connection, project_id=project_id, source_id=source_id
    )
    for fact_id in fact_ids:
        store.delete_node(
            connection, project_id=project_id, node_id=fact_id, actor_kind=actor_kind
        )
    store.audit(
        connection,
        project_id=project_id,
        action="knowledge.forget",
        entity_id=source_id,
        actor_kind=actor_kind,
        detail={"fact_ids": fact_ids},
    )
    return {"forgotten": fact_ids}


def retention_deadline(now: datetime, *, years: int = 1) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - timedelta(days=365 * years)
