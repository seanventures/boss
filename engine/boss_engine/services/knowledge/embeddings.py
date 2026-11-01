"""Embeddings for meaning-based search (BOSSAPP-622; operator decisions 2026-10-22).

The database image always has pgvector, so a stored embedding is searched through the HNSW
index (``PostgresKnowledgeStore.sync_vector_index``). The embedding model is its own slot,
Setup › Repo and models › Embedding model, one per org (embedding_slot.py):
``configured_embedder`` answers with that slot's
governed embedder when its check passed, or None and the reason in words, and retrieval then runs
on full text and the graph and says so in status.

Only distilled facts and node summaries are embedded (``EMBEDDED_KINDS``), never raw logs.
``embed_due`` is the background half: on the worker's knowledge tick it drops a project's vectors
of any other space (another model or dimension) and embeds what the current one lacks.
"""
from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .embedding_slot import NO_MODEL, UNCHECKED  # noqa: F401 — the reasons, in words

#: Rules (BOSSAPP-768) are embedded with the org's embedding model like any fact: a proposed rule
#: so review can find its near-duplicates by meaning, an approved one so a task finds it.
EMBEDDED_KINDS = ("fact", "decision", "incident", "runbook", "spec", "review_finding", "rule",
                  # what agents learn while they work (BOSSAPP-768 §4.3), under the same governed pass
                  "investigation", "test_failure", "fix", "workaround", "root_cause", "precedent",
                  # what a rulebook or regulation put in the graph (rulebook source G1): its
                  # documents and clauses, a bank's data-quality rules, their thresholds and the
                  # critical data elements they protect — so a question in other words finds them.
                  "document", "clause", "data_quality_rule", "dq_threshold", "cde")
#: Summaries embedded per project per tick: a few governed calls, never a burst.
BATCH = 64


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def configured_embedder(connection: Connection, settings: Any, project_id: str) -> tuple[Embedder | None, str]:
    """The embedder this project's slot gives, or None and the reason in words."""
    from .embedding_slot import active_embedder

    return active_embedder(connection, settings, project_id)


def drop_other_spaces(connection: Connection, *, project_id: str, model_id: str) -> int:
    """Remove this project's vectors of every space but ``model_id``, in kg_embeddings and in
    the pgvector side table of each dimension: spaces never mix."""
    from .store import _vec_table

    dims = connection.execute(text(
        "SELECT DISTINCT dimensions FROM kg_embeddings WHERE project_id = :p AND model_id <> :m"),
        {"p": project_id, "m": model_id}).scalars().all()
    removed = connection.execute(text(
        "DELETE FROM kg_embeddings WHERE project_id = :p AND model_id <> :m"),
        {"p": project_id, "m": model_id}).rowcount or 0
    for dim in (int(d) for d in dims if 1 <= int(d) <= 2000):
        table = _vec_table(dim)
        if connection.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar():
            connection.execute(text(f"DELETE FROM {table} WHERE project_id = :p AND model_id <> :m"),
                               {"p": project_id, "m": model_id})
    return int(removed)


def embed_due(engine: Any, *, project_id: str | None = None, limit: int = BATCH) -> dict[str, int]:
    """Re-embed in the background, per project whose slot (its org's, or an older install's own
    row) passed its check: a new org slot is a new space for EVERY project of the org. One project's
    refusal (budget, a provider down) never holds the others back; nothing raises."""
    from ...config import get_settings
    from .embedding_slot import active_embedder, projects_with_a_slot
    from .store import PostgresKnowledgeStore

    from .. import projects_archive    # an archived project is not re-embedded

    out = {"projects": 0, "embedded": 0, "dropped": 0, "failed": 0}
    with engine.begin() as connection:
        projects = projects_archive.live_ids(
            connection, [project_id] if project_id else projects_with_a_slot(connection))
    settings = get_settings()
    for pid in projects:
        try:
            with engine.begin() as connection:
                embedder, _reason = active_embedder(connection, settings, pid)
                if embedder is None:
                    continue
                out["projects"] += 1
                out["dropped"] += drop_other_spaces(connection, project_id=pid, model_id=embedder.model_id)
                out["embedded"] += embed_pending(connection, PostgresKnowledgeStore(), project_id=pid,
                                                 embedder=embedder, limit=limit)
        except Exception:  # noqa: BLE001 — retried on the next tick
            out["failed"] += 1
    return out


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
