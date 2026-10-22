"""Embeddings for meaning-based search (BOSSAPP-622; operator decision 2026-10-22).

The database image always has pgvector, so a stored embedding is searched through the HNSW
index (``PostgresKnowledgeStore.sync_vector_index``). What it needs is an embedding model. An
:class:`Embedder` is injected; ``configured_embedder`` says which one this BOSS has, or why it
has none, and retrieval then runs on full text and the graph and states that reason in status.

Only distilled facts and node summaries are embedded (``EMBEDDED_KINDS``), never raw logs.
"""
from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

EMBEDDED_KINDS = ("fact", "decision", "incident", "runbook", "spec", "review_finding")
NO_MODEL = "no embedding model is configured (BOSS_KNOWLEDGE_EMBEDDING_MODEL)"
NOT_WIRED = "an embedding model is named, but this build has no governed embedding call for it"


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def configured_embedder(settings: Any) -> tuple[Embedder | None, str]:
    """The embedder this BOSS is configured with, or None and the reason in words."""
    model = str(getattr(settings, "knowledge_embedding_model", "") or "").strip()
    if not model:
        return None, NO_MODEL
    return None, NOT_WIRED


def embed_pending(
    connection: Connection, store: Any, *, project_id: str, embedder: Embedder, limit: int = 200
) -> int:
    """Embed the open facts and summaries that have no embedding for this model and content."""
    rows = connection.execute(
        text(
            """
            SELECT n.id, n.name, n.body, n.content_hash FROM kg_nodes n
            WHERE n.project_id = :project_id AND n.kind = ANY(:kinds)
              AND (n.valid_to IS NULL OR n.valid_to > now()) AND n.content_hash IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM kg_embeddings e WHERE e.node_id = n.id
                                AND e.model_id = :model AND e.content_hash = n.content_hash)
            ORDER BY n.id LIMIT :n
            """
        ),
        {"project_id": project_id, "kinds": list(EMBEDDED_KINDS), "model": embedder.model_id, "n": limit},
    ).mappings().all()
    if not rows:
        return 0
    vectors = embedder.embed([f"{r['name']}\n{r['body']}"[:4000] for r in rows])
    for row, vector in zip(rows, vectors):
        store.upsert_embedding(connection, project_id=project_id, node_id=row["id"],
                               model_id=embedder.model_id, embedding=list(vector),
                               content_hash=row["content_hash"])
    return len(rows)
