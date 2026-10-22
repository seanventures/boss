"""Retrieval timing on a synthetic sample.

The scale target is 10M nodes, 50M edges, and 5M facts, with p95 under
300 ms. This function records the sample it actually loaded. It does not
report that target as met unless those counts were loaded and the measured
hybrid p95 is under the budget.
"""
from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore

SCALE_NODES = 10_000_000
SCALE_EDGES = 50_000_000
SCALE_FACTS = 5_000_000
BUDGET_MS = 300.0
_MAX_NODES = 500_000


def percentile_ms(samples: list[float], pct: float = 95.0) -> float:
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = math.ceil((pct / 100.0) * len(ordered)) - 1
    return ordered[max(0, min(rank, len(ordered) - 1))]


def _timed(calls: int, fn) -> float:
    samples = []
    for _ in range(calls):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return percentile_ms(samples)


def measure_retrieval(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    nodes: int,
    edges: int,
    queries: int,
) -> dict[str, Any]:
    count = int(nodes)
    edge_count = int(edges)
    calls = int(queries)
    if count < 2 or count > _MAX_NODES:
        raise ValueError(f"nodes must be from 2 to {_MAX_NODES}")
    if edge_count < 1 or edge_count > count:
        raise ValueError("edges must be from 1 to the node count")
    if calls < 1 or calls > 200:
        raise ValueError("queries must be from 1 to 200")
    store.ensure_schema(connection)
    org_id = store._org_id(connection, project_id, None)
    prefix = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:10]
    if connection.execute(text("SELECT to_regclass('public.kg_vec_8')")).scalar():
        connection.execute(
            text(
                """
                DELETE FROM kg_vec_8
                WHERE project_id = :project_id AND model_id = 'bench-model'
                """
            ),
            {"project_id": project_id},
        )
    connection.execute(
        text(
            """
            DELETE FROM kg_edges WHERE project_id = :project_id AND source = 'bench'
            """
        ),
        {"project_id": project_id},
    )
    connection.execute(
        text(
            """
            DELETE FROM kg_embeddings
            WHERE project_id = :project_id AND model_id = 'bench-model'
            """
        ),
        {"project_id": project_id},
    )
    connection.execute(
        text(
            """
            DELETE FROM kg_nodes WHERE project_id = :project_id AND source = 'bench'
            """
        ),
        {"project_id": project_id},
    )
    connection.execute(
        text(
            f"""
            INSERT INTO kg_nodes (
              id, project_id, kind, node_key, name, body, attrs, layer, source,
              confidence, org_id, scope, trust_tier, weight, created_by,
              content_hash, recorded_at, search_tsv, first_seen, last_seen
            )
            SELECT
              'kgn_' || '{prefix}' || '_' || i,
              :project_id,
              'fact',
              'fact:bench-' || i,
              'bench ' || i,
              CASE WHEN i % 50 = 0 THEN 'zephyrwidget bench fact ' || i
                   ELSE 'plain bench fact ' || i END,
              '{{}}'::jsonb,
              'deterministic',
              'bench',
              1.0,
              :org_id,
              :project_id,
              'observed',
              1.0,
              'system',
              md5('bench-' || i),
              now(),
              to_tsvector(
                'english',
                'bench ' || i || ' ' ||
                CASE WHEN i % 50 = 0 THEN 'zephyrwidget' ELSE 'plain' END
              ),
              now(),
              now()
            FROM generate_series(1, {count}) AS i
            """
        ),
        {"project_id": project_id, "org_id": org_id},
    )
    connection.execute(
        text(
            f"""
            INSERT INTO kg_embeddings (
              id, project_id, node_id, model_id, dimensions, embedding, content_hash
            )
            SELECT
              'emb_' || '{prefix}' || '_' || i,
              :project_id,
              'kgn_' || '{prefix}' || '_' || i,
              'bench-model',
              8,
              ARRAY[
                ((i % 7) + 1)::float8,
                ((i % 5) + 1)::float8,
                0, 0, 0, 0, 0, 1
              ]::float8[],
              md5('bench-' || i)
            FROM generate_series(1, {count}) AS i
            """
        ),
        {"project_id": project_id},
    )
    connection.execute(
        text(
            f"""
            INSERT INTO kg_edges (
              id, project_id, src_id, dst_id, kind, attrs, layer, source,
              confidence, org_id, scope, trust_tier, weight, created_by,
              content_hash, recorded_at, first_seen, last_seen
            )
            SELECT
              'kge_' || '{prefix}' || '_' || i,
              :project_id,
              'kgn_' || '{prefix}' || '_' || i,
              'kgn_' || '{prefix}' || '_' || (((i * 3) % {count}) + 1),
              'calls',
              '{{}}'::jsonb,
              'deterministic',
              'bench',
              1.0,
              :org_id,
              :project_id,
              'observed',
              1.0,
              'system',
              md5('edge-' || i),
              now(),
              now(),
              now()
            FROM generate_series(1, {edge_count}) AS i
            WHERE i <> (((i * 3) % {count}) + 1)
            """
        ),
        {"project_id": project_id, "org_id": org_id},
    )
    stored_edges = connection.execute(
        text(
            """
            SELECT COUNT(*) FROM kg_edges
            WHERE project_id = :project_id AND source = 'bench'
            """
        ),
        {"project_id": project_id},
    ).scalar_one()
    for statement in ("ANALYZE kg_nodes", "ANALYZE kg_edges", "ANALYZE kg_embeddings"):
        connection.execute(text(statement))
    text_p95 = _timed(
        calls,
        lambda: store.text_search(
            connection, project_id=project_id, query="zephyrwidget", limit=10
        ),
    )
    vector_p95 = _timed(
        calls,
        lambda: store.vector_search(
            connection,
            project_id=project_id,
            model_id="bench-model",
            embedding=[1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            limit=10,
        ),
    )
    hybrid_p95 = _timed(
        calls,
        lambda: store.hybrid_search(
            connection,
            project_id=project_id,
            query="zephyrwidget",
            model_id="bench-model",
            embedding=[1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            limit=10,
        ),
    )
    traverse_p95 = _timed(
        calls,
        lambda: store.traverse(
            connection,
            project_id=project_id,
            start_id=f"kgn_{prefix}_1",
            max_depth=3,
        ),
    )
    hnsw_p95 = None
    if store._vector_extension_available(connection):
        store.sync_vector_index(connection, dimensions=8)
        if store._hnsw_ready(connection, 8) and store._iterative_hnsw(connection):
            hnsw_p95 = _timed(
                calls,
                lambda: store.vector_search(
                    connection,
                    project_id=project_id,
                    model_id="bench-model",
                    embedding=[1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
                    limit=10,
                ),
            )
    facts = count
    met = (
        count >= SCALE_NODES
        and int(stored_edges) >= SCALE_EDGES
        and facts >= SCALE_FACTS
        and hybrid_p95 < BUDGET_MS
    )
    return {
        "nodes": count,
        "edges": int(stored_edges),
        "facts": facts,
        "queries": calls,
        "text_p95_ms": text_p95,
        "vector_p95_ms": vector_p95,
        "vector_p95_path": "exact",
        "hnsw_p95_ms": hnsw_p95,
        "hybrid_p95_ms": hybrid_p95,
        "traverse_p95_ms": traverse_p95,
        "vector_index": store.vector_index_kind(connection),
        "scale_nodes": SCALE_NODES,
        "scale_edges": SCALE_EDGES,
        "scale_facts": SCALE_FACTS,
        "budget_ms": BUDGET_MS,
        "scale_target_met": met,
    }
