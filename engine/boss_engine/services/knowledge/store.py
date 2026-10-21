"""Postgres knowledge store for the Phase 0 retrieval slice.

kg_nodes and kg_edges stay the system of record, including the SRE topology
that KnowledgeGraphService already writes. This store adds trust tiers,
temporal close, full text, and exact-cosine embeddings. Apache AGE is not
used: Phase 0 did not measure it, so traversal is a recursive CTE.

Vectors are double precision[] until the Debian image provides pgvector.
An agent cannot write or close a governed or approved fact; the tier is
chosen by the caller role, not accepted from an untrusted payload.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import make_id, now_utc

NODE_KINDS = frozenset({
    "service", "resource", "repo", "deployment", "datastore", "external",
    "incident", "root_cause",
    "module", "file", "symbol", "endpoint", "table", "queue", "config_key",
    "spec", "decision", "pull_request", "review_finding", "release", "runbook",
    "alert_rule", "run", "person", "agent", "team",
    "fact", "topic",
})
EDGE_KINDS = frozenset({
    "depends_on", "runs_on", "deployed_from", "owns", "emits", "caused_by",
    "correlates_with",
    "defines", "calls", "imports", "reads", "writes", "publishes", "subscribes",
    "exposes", "tests",
    "specifies", "implements", "changed", "reviewed", "found_in", "shipped",
    "fired_on", "fixed_by", "decided_by", "approved_by", "about", "supersedes",
    "evidenced_by",
})
TRUST_TIERS = frozenset({"observed", "governed", "inferred", "approved"})
# The existing kg_* CHECK only allows these two layers. Trust lives in trust_tier.
_LAYER_FOR_TIER = {
    "observed": "deterministic",
    "governed": "deterministic",
    "approved": "deterministic",
    "inferred": "inferred",
}
_ACTOR_TIERS = {
    "agent": frozenset({"inferred"}),
    "person": frozenset({"inferred", "governed", "approved"}),
    "system": frozenset({"observed", "inferred"}),
}
RRF_K = 60

_SCHEMA_SQL = """
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS org_id TEXT;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS scope TEXT;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS trust_tier TEXT NOT NULL DEFAULT 'observed';
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS weight DOUBLE PRECISION NOT NULL DEFAULT 1.0;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system';
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS body TEXT NOT NULL DEFAULT '';
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS search_tsv TSVECTOR;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS org_id TEXT;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS scope TEXT;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS trust_tier TEXT NOT NULL DEFAULT 'observed';
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS weight DOUBLE PRECISION NOT NULL DEFAULT 1.0;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system';
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS kg_embeddings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK (dimensions > 0),
  embedding DOUBLE PRECISION[] NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (node_id, model_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_search ON kg_nodes USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_kg_embeddings_project_model
  ON kg_embeddings(project_id, model_id);
CREATE TABLE IF NOT EXISTS kg_captures (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  body TEXT NOT NULL,
  scrub_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'captured',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, source_kind, source_id, content_hash)
);
CREATE TABLE IF NOT EXISTS kg_usage (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  step TEXT NOT NULL,
  fact_id TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kg_fact_stats (
  fact_id TEXT PRIMARY KEY REFERENCES kg_nodes(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  last_used_at TIMESTAMPTZ,
  archived BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS kg_audit (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_NODE_COLUMNS = """
id, project_id, kind, node_key, name, body, trust_tier, scope, content_hash,
recorded_at, valid_from, valid_to, org_id, created_by
"""


def _content_hash(kind: str, node_key: str, name: str, body: str, trust_tier: str) -> str:
    material = "\n".join([kind, node_key, name, body, trust_tier])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _assert_can_write(actor_kind: str, trust_tier: str) -> None:
    allowed = _ACTOR_TIERS.get(actor_kind)
    if allowed is None:
        raise ValueError(f"unknown actor_kind {actor_kind!r}")
    if trust_tier not in TRUST_TIERS:
        raise ValueError(f"unknown trust_tier {trust_tier!r}")
    if trust_tier not in allowed:
        raise PermissionError(f"{actor_kind} cannot write trust_tier {trust_tier}")


def _assert_can_close(actor_kind: str, trust_tier: str) -> None:
    if actor_kind == "person":
        return
    if actor_kind == "agent" and trust_tier == "inferred":
        return
    if actor_kind == "system" and trust_tier in {"observed", "inferred"}:
        return
    raise PermissionError(f"{actor_kind} cannot close trust_tier {trust_tier}")


def _vector_literal(values: list[float]) -> str:
    if not values:
        raise ValueError("embedding must be a non-empty list of numbers")
    parts: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("embedding must be a non-empty list of numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("embedding must be finite")
        parts.append(repr(number))
    return "{" + ",".join(parts) + "}"


class PostgresKnowledgeStore:
    def __init__(self) -> None:
        self._schema_ready = False

    def ensure_schema(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        for stmt in _SCHEMA_SQL.split(";"):
            sql = stmt.strip()
            if sql:
                connection.execute(text(sql))
        self._schema_ready = True

    def _org_id(self, connection: Connection, project_id: str, claimed: Optional[str]) -> str:
        if not project_id or not str(project_id).strip():
            raise ValueError("project_id is required")
        org_id = connection.execute(
            text(
                """
                SELECT w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).scalar()
        if not org_id:
            raise LookupError(f"project not found: {project_id}")
        if claimed is not None and claimed != org_id:
            raise PermissionError("org_id does not match the project")
        return org_id

    def _node_in_project(self, connection: Connection, project_id: str, node_id: str) -> dict:
        row = connection.execute(
            text(f"SELECT {_NODE_COLUMNS} FROM kg_nodes WHERE id = :id"),
            {"id": node_id},
        ).mappings().first()
        if row is None:
            raise LookupError(f"node not found: {node_id}")
        found = dict(row)
        if found["project_id"] != project_id:
            raise PermissionError(f"node {node_id} is not in project {project_id}")
        return found

    def upsert_node(
        self,
        connection: Connection,
        *,
        project_id: str,
        kind: str,
        node_key: str,
        name: str,
        body: str,
        trust_tier: str,
        actor_kind: str,
        org_id: Optional[str] = None,
        attrs: Optional[dict] = None,
        source: str = "knowledge_store",
    ) -> str:
        self.ensure_schema(connection)
        if kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {kind!r}")
        _assert_can_write(actor_kind, trust_tier)
        resolved_org = self._org_id(connection, project_id, org_id)
        attrs_json = json.dumps(attrs or {})
        digest = _content_hash(kind, node_key, name, body, trust_tier)
        existing = connection.execute(
            text(
                """
                SELECT id, trust_tier, content_hash
                FROM kg_nodes
                WHERE project_id = :project_id AND kind = :kind AND node_key = :node_key
                """
            ),
            {"project_id": project_id, "kind": kind, "node_key": node_key},
        ).mappings().first()
        now = now_utc()
        if existing is not None:
            if actor_kind == "agent" and existing["trust_tier"] != "inferred":
                raise PermissionError("agent cannot overwrite a non-inferred fact")
            if actor_kind == "system" and existing["trust_tier"] in {"governed", "approved"}:
                raise PermissionError("system cannot overwrite a governed fact")
            if existing["content_hash"] == digest:
                connection.execute(
                    text("UPDATE kg_nodes SET last_seen = :now WHERE id = :id"),
                    {"now": now, "id": existing["id"]},
                )
                return existing["id"]
            connection.execute(
                text(
                    """
                    UPDATE kg_nodes SET
                      name = :name,
                      body = :body,
                      attrs = CAST(:attrs AS JSONB),
                      source = :source,
                      trust_tier = :trust_tier,
                      layer = :layer,
                      content_hash = :content_hash,
                      recorded_at = :now,
                      last_seen = :now,
                      created_by = :actor,
                      search_tsv = to_tsvector('english', :name || ' ' || :body)
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing["id"],
                    "name": name,
                    "body": body,
                    "attrs": attrs_json,
                    "source": source,
                    "trust_tier": trust_tier,
                    "layer": _LAYER_FOR_TIER[trust_tier],
                    "content_hash": digest,
                    "now": now,
                    "actor": actor_kind,
                },
            )
            return existing["id"]

        node_id = make_id("kgn")
        connection.execute(
            text(
                """
                INSERT INTO kg_nodes (
                  id, project_id, kind, node_key, name, body, attrs, layer, source,
                  confidence, org_id, scope, trust_tier, weight, created_by,
                  content_hash, recorded_at, search_tsv, first_seen, last_seen
                ) VALUES (
                  :id, :project_id, :kind, :node_key, :name, :body,
                  CAST(:attrs AS JSONB), :layer, :source, 1.0,
                  :org_id, :scope, :trust_tier, 1.0, :actor, :content_hash, :now,
                  to_tsvector('english', :name || ' ' || :body), :now, :now
                )
                """
            ),
            {
                "id": node_id,
                "project_id": project_id,
                "kind": kind,
                "node_key": node_key,
                "name": name,
                "body": body,
                "attrs": attrs_json,
                "source": source,
                "layer": _LAYER_FOR_TIER[trust_tier],
                "org_id": resolved_org,
                "scope": project_id,
                "trust_tier": trust_tier,
                "actor": actor_kind,
                "content_hash": digest,
                "now": now,
            },
        )
        return node_id

    def get_node(
        self,
        connection: Connection,
        *,
        project_id: str,
        node_id: str,
        include_closed: bool = False,
    ) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        try:
            row = self._node_in_project(connection, project_id, node_id)
        except LookupError:
            return None
        closed = row["valid_to"] is not None and row["valid_to"] <= now_utc()
        if closed and not include_closed:
            return None
        return row

    def upsert_edge(
        self,
        connection: Connection,
        *,
        project_id: str,
        src_id: str,
        dst_id: str,
        kind: str,
        actor_kind: str,
    ) -> str:
        self.ensure_schema(connection)
        if kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind {kind!r}")
        trust = {"system": "observed", "person": "governed", "agent": "inferred"}.get(actor_kind)
        if trust is None:
            raise ValueError(f"unknown actor_kind {actor_kind!r}")
        _assert_can_write(actor_kind, trust)
        org_id = self._org_id(connection, project_id, None)
        self._node_in_project(connection, project_id, src_id)
        self._node_in_project(connection, project_id, dst_id)
        now = now_utc()
        edge_id = make_id("kge")
        return connection.execute(
            text(
                """
                INSERT INTO kg_edges (
                  id, project_id, src_id, dst_id, kind, attrs, layer, source,
                  confidence, org_id, scope, trust_tier, weight, created_by,
                  content_hash, recorded_at, first_seen, last_seen
                ) VALUES (
                  :id, :project_id, :src, :dst, :kind, CAST('{}' AS JSONB),
                  :layer, 'knowledge_store', 1.0, :org_id, :scope, :trust_tier,
                  1.0, :actor, :content_hash, :now, :now, :now
                )
                ON CONFLICT (project_id, src_id, dst_id, kind) DO UPDATE SET
                  last_seen = EXCLUDED.last_seen
                RETURNING id
                """
            ),
            {
                "id": edge_id,
                "project_id": project_id,
                "src": src_id,
                "dst": dst_id,
                "kind": kind,
                "layer": _LAYER_FOR_TIER[trust],
                "org_id": org_id,
                "scope": project_id,
                "trust_tier": trust,
                "actor": actor_kind,
                "content_hash": _content_hash(kind, src_id, dst_id, "", trust),
                "now": now,
            },
        ).scalar_one()

    def close_validity(
        self,
        connection: Connection,
        *,
        project_id: str,
        valid_to,
        actor_kind: str,
        node_id: Optional[str] = None,
        edge_id: Optional[str] = None,
        superseded_by: Optional[str] = None,
    ) -> None:
        self.ensure_schema(connection)
        if (node_id is None) == (edge_id is None):
            raise ValueError("close either a node or an edge")
        self._org_id(connection, project_id, None)
        if edge_id is not None:
            edge = connection.execute(
                text(
                    """
                    SELECT id, project_id, trust_tier FROM kg_edges WHERE id = :id
                    """
                ),
                {"id": edge_id},
            ).mappings().first()
            if edge is None:
                raise LookupError(f"edge not found: {edge_id}")
            if edge["project_id"] != project_id:
                raise PermissionError(f"edge {edge_id} is not in project {project_id}")
            _assert_can_close(actor_kind, edge["trust_tier"])
            connection.execute(
                text("UPDATE kg_edges SET valid_to = :valid_to WHERE id = :id"),
                {"valid_to": valid_to, "id": edge_id},
            )
            return
        node = self._node_in_project(connection, project_id, node_id)
        _assert_can_close(actor_kind, node["trust_tier"])
        connection.execute(
            text("UPDATE kg_nodes SET valid_to = :valid_to WHERE id = :id"),
            {"valid_to": valid_to, "id": node_id},
        )
        if superseded_by is not None:
            self._node_in_project(connection, project_id, superseded_by)
            self.upsert_edge(
                connection,
                project_id=project_id,
                src_id=superseded_by,
                dst_id=node_id,
                kind="supersedes",
                actor_kind=actor_kind,
            )

    def text_search(
        self,
        connection: Connection,
        *,
        project_id: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        if not query or not query.strip():
            raise ValueError("query is required")
        rows = connection.execute(
            text(
                """
                SELECT id AS node_id, node_key,
                       ts_rank(search_tsv, plainto_tsquery('english', :query)) AS rank
                FROM kg_nodes
                WHERE project_id = :project_id
                  AND search_tsv @@ plainto_tsquery('english', :query)
                  AND (valid_to IS NULL OR valid_to > now())
                ORDER BY rank DESC, node_key ASC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "query": query, "limit": max(1, min(int(limit), 50))},
        ).mappings().all()
        return [dict(row) for row in rows]

    def upsert_embedding(
        self,
        connection: Connection,
        *,
        project_id: str,
        node_id: str,
        model_id: str,
        embedding: list[float],
        content_hash: str,
    ) -> str:
        self.ensure_schema(connection)
        node = self._node_in_project(connection, project_id, node_id)
        if node["content_hash"] != content_hash:
            raise ValueError("content_hash does not match the node")
        literal = _vector_literal(embedding)
        embedding_id = make_id("emb")
        return connection.execute(
            text(
                """
                INSERT INTO kg_embeddings (
                  id, project_id, node_id, model_id, dimensions, embedding, content_hash
                ) VALUES (
                  :id, :project_id, :node_id, :model_id, :dimensions,
                  CAST(:embedding AS float8[]), :content_hash
                )
                ON CONFLICT (node_id, model_id, content_hash) DO UPDATE SET
                  embedding = EXCLUDED.embedding
                RETURNING id
                """
            ),
            {
                "id": embedding_id,
                "project_id": project_id,
                "node_id": node_id,
                "model_id": model_id,
                "dimensions": len(embedding),
                "embedding": literal,
                "content_hash": content_hash,
            },
        ).scalar_one()

    def vector_search(
        self,
        connection: Connection,
        *,
        project_id: str,
        model_id: str,
        embedding: list[float],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        literal = _vector_literal(embedding)
        span = connection.execute(
            text(
                """
                SELECT MIN(dimensions) AS low, MAX(dimensions) AS high, COUNT(*) AS n
                FROM kg_embeddings
                WHERE project_id = :project_id AND model_id = :model_id
                """
            ),
            {"project_id": project_id, "model_id": model_id},
        ).mappings().one()
        if span["n"] == 0:
            return []
        if span["low"] != len(embedding) or span["high"] != len(embedding):
            raise ValueError("embedding dimension mismatch")
        rows = connection.execute(
            text(
                """
                SELECT node_id, node_key, cosine FROM (
                  SELECT n.id AS node_id, n.node_key,
                    (
                      SELECT COALESCE(SUM(a * b), 0)
                      FROM unnest(e.embedding, CAST(:query AS float8[])) AS u(a, b)
                    ) / NULLIF(
                      sqrt((SELECT COALESCE(SUM(x * x), 0) FROM unnest(e.embedding) AS s(x)))
                      * sqrt((SELECT COALESCE(SUM(y * y), 0)
                              FROM unnest(CAST(:query AS float8[])) AS t(y))),
                      0
                    ) AS cosine
                  FROM kg_embeddings e
                  JOIN kg_nodes n
                    ON n.id = e.node_id AND n.content_hash = e.content_hash
                  WHERE e.project_id = :project_id
                    AND e.model_id = :model_id
                    AND n.project_id = :project_id
                    AND (n.valid_to IS NULL OR n.valid_to > now())
                ) scored
                WHERE cosine > 0
                ORDER BY cosine DESC, node_key ASC
                LIMIT :limit
                """
            ),
            {
                "project_id": project_id,
                "model_id": model_id,
                "query": literal,
                "limit": max(1, min(int(limit), 50)),
            },
        ).mappings().all()
        return [dict(row) for row in rows]

    def hybrid_search(
        self,
        connection: Connection,
        *,
        project_id: str,
        query: str,
        model_id: str,
        embedding: list[float],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Reciprocal-rank fusion. Overlap outranks a hit from only one list."""
        text_hits = self.text_search(connection, project_id=project_id, query=query, limit=50)
        vector_hits = self.vector_search(
            connection,
            project_id=project_id,
            model_id=model_id,
            embedding=embedding,
            limit=50,
        )
        fused: dict[str, dict[str, Any]] = {}
        for hits in (text_hits, vector_hits):
            for rank, hit in enumerate(hits, start=1):
                slot = fused.setdefault(
                    hit["node_id"],
                    {"node_id": hit["node_id"], "node_key": hit["node_key"], "score": 0.0},
                )
                slot["score"] += 1.0 / (RRF_K + rank)
        ordered = sorted(fused.values(), key=lambda item: (-item["score"], item["node_key"]))
        return ordered[: max(1, min(int(limit), 50))]

    def traverse(
        self,
        connection: Connection,
        *,
        project_id: str,
        start_id: str,
        max_depth: int,
        pattern: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if pattern is not None:
            raise ValueError(
                "cypher traversal is refused; projection is recursive_cte "
                "until an AGE benchmark selects it"
            )
        self.ensure_schema(connection)
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        self._node_in_project(connection, project_id, start_id)
        rows = connection.execute(
            text(
                """
                WITH RECURSIVE walk AS (
                  SELECT e.dst_id AS id, 1 AS depth, ARRAY[e.src_id, e.dst_id] AS path
                  FROM kg_edges e
                  JOIN kg_nodes n ON n.id = e.dst_id
                  WHERE e.src_id = :start
                    AND e.project_id = :project_id
                    AND n.project_id = :project_id
                    AND (e.valid_to IS NULL OR e.valid_to > now())
                    AND (n.valid_to IS NULL OR n.valid_to > now())
                  UNION ALL
                  SELECT e.dst_id, w.depth + 1, w.path || e.dst_id
                  FROM kg_edges e
                  JOIN walk w ON e.src_id = w.id
                  JOIN kg_nodes n ON n.id = e.dst_id
                  WHERE e.project_id = :project_id
                    AND n.project_id = :project_id
                    AND w.depth < :max_depth
                    AND NOT e.dst_id = ANY(w.path)
                    AND (e.valid_to IS NULL OR e.valid_to > now())
                    AND (n.valid_to IS NULL OR n.valid_to > now())
                )
                SELECT n.id AS node_id, n.node_key, MIN(w.depth) AS depth
                FROM walk w
                JOIN kg_nodes n ON n.id = w.id
                GROUP BY n.id, n.node_key
                ORDER BY MIN(w.depth), n.node_key
                """
            ),
            {"start": start_id, "project_id": project_id, "max_depth": int(max_depth)},
        ).mappings().all()
        return [dict(row) for row in rows]

    def neighbors(
        self,
        connection: Connection,
        *,
        project_id: str,
        node_id: str,
        kind: Optional[str] = None,
        direction: str = "out",
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self._node_in_project(connection, project_id, node_id)
        if direction not in {"out", "in"}:
            raise ValueError("direction must be out or in")
        if kind is not None and kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind {kind!r}")
        anchor = "e.src_id" if direction == "out" else "e.dst_id"
        other = "e.dst_id" if direction == "out" else "e.src_id"
        kind_sql = "AND e.kind = :kind" if kind is not None else ""
        rows = connection.execute(
            text(
                f"""
                SELECT n.id AS node_id, n.node_key, n.kind, n.name, n.trust_tier,
                       n.body, e.kind AS edge_kind
                FROM kg_edges e
                JOIN kg_nodes n ON n.id = {other}
                WHERE {anchor} = :node_id
                  AND e.project_id = :project_id
                  AND n.project_id = :project_id
                  {kind_sql}
                  AND (e.valid_to IS NULL OR e.valid_to > now())
                  AND (n.valid_to IS NULL OR n.valid_to > now())
                ORDER BY n.node_key
                """
            ),
            {"node_id": node_id, "project_id": project_id, "kind": kind},
        ).mappings().all()
        return [dict(row) for row in rows]

    def find_by_key(
        self,
        connection: Connection,
        *,
        project_id: str,
        kind: str,
        node_key: str,
    ) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        row = connection.execute(
            text(
                f"""
                SELECT {_NODE_COLUMNS}
                FROM kg_nodes
                WHERE project_id = :project_id AND kind = :kind AND node_key = :node_key
                  AND (valid_to IS NULL OR valid_to > now())
                """
            ),
            {"project_id": project_id, "kind": kind, "node_key": node_key},
        ).mappings().first()
        return dict(row) if row else None

    def capture(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_kind: str,
        source_id: str,
        body: str,
        scrub_counts: dict,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        existing = connection.execute(
            text(
                """
                SELECT id, content_hash FROM kg_captures
                WHERE project_id = :project_id AND source_kind = :source_kind
                  AND source_id = :source_id AND content_hash = :content_hash
                """
            ),
            {
                "project_id": project_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "content_hash": digest,
            },
        ).mappings().first()
        if existing is not None:
            return {"id": existing["id"], "content_hash": digest, "replayed": True}
        capture_id = make_id("cap")
        connection.execute(
            text(
                """
                INSERT INTO kg_captures (
                  id, project_id, source_kind, source_id, content_hash, body, scrub_counts
                ) VALUES (
                  :id, :project_id, :source_kind, :source_id, :content_hash, :body,
                  CAST(:scrub_counts AS JSONB)
                )
                """
            ),
            {
                "id": capture_id,
                "project_id": project_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "content_hash": digest,
                "body": body,
                "scrub_counts": json.dumps(scrub_counts),
            },
        )
        return {"id": capture_id, "content_hash": digest, "replayed": False}

    def audit(
        self,
        connection: Connection,
        *,
        project_id: str,
        action: str,
        entity_id: str,
        actor_kind: str,
        detail: Optional[dict] = None,
    ) -> str:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        audit_id = make_id("kga")
        connection.execute(
            text(
                """
                INSERT INTO kg_audit (id, project_id, action, entity_id, actor_kind, detail)
                VALUES (:id, :project_id, :action, :entity_id, :actor_kind, CAST(:detail AS JSONB))
                """
            ),
            {
                "id": audit_id,
                "project_id": project_id,
                "action": action,
                "entity_id": entity_id,
                "actor_kind": actor_kind,
                "detail": json.dumps(detail or {}),
            },
        )
        return audit_id

    def log_usage(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        step: str,
        fact_ids: list[str],
    ) -> int:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        written = 0
        for fact_id in fact_ids:
            self._node_in_project(connection, project_id, fact_id)
            connection.execute(
                text(
                    """
                    INSERT INTO kg_usage (id, project_id, run_id, step, fact_id)
                    VALUES (:id, :project_id, :run_id, :step, :fact_id)
                    """
                ),
                {
                    "id": make_id("use"),
                    "project_id": project_id,
                    "run_id": run_id,
                    "step": step,
                    "fact_id": fact_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO kg_fact_stats (fact_id, project_id, last_used_at)
                    VALUES (:fact_id, :project_id, now())
                    ON CONFLICT (fact_id) DO UPDATE SET last_used_at = now()
                    """
                ),
                {"fact_id": fact_id, "project_id": project_id},
            )
            written += 1
        return written

    def record_outcome(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        success: bool,
    ) -> list[str]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT fact_id FROM kg_usage
                WHERE project_id = :project_id AND run_id = :run_id
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).scalars().all()
        column = "success_count" if success else "failure_count"
        for fact_id in rows:
            connection.execute(
                text(
                    f"""
                    INSERT INTO kg_fact_stats (fact_id, project_id, {column}, last_used_at)
                    VALUES (:fact_id, :project_id, 1, now())
                    ON CONFLICT (fact_id) DO UPDATE SET
                      {column} = kg_fact_stats.{column} + 1,
                      last_used_at = now()
                    """
                ),
                {"fact_id": fact_id, "project_id": project_id},
            )
        return list(rows)

    def fact_stat(
        self, connection: Connection, *, project_id: str, fact_id: str
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        self._node_in_project(connection, project_id, fact_id)
        row = connection.execute(
            text(
                """
                SELECT fact_id, success_count, failure_count, last_used_at, archived
                FROM kg_fact_stats
                WHERE fact_id = :fact_id AND project_id = :project_id
                """
            ),
            {"fact_id": fact_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            return {
                "fact_id": fact_id,
                "success_count": 0,
                "failure_count": 0,
                "last_used_at": None,
                "archived": False,
            }
        return dict(row)

    def set_archived(
        self,
        connection: Connection,
        *,
        project_id: str,
        fact_id: str,
        archived: bool,
        weight: float,
    ) -> None:
        self.ensure_schema(connection)
        self._node_in_project(connection, project_id, fact_id)
        connection.execute(
            text(
                """
                INSERT INTO kg_fact_stats (fact_id, project_id, archived)
                VALUES (:fact_id, :project_id, :archived)
                ON CONFLICT (fact_id) DO UPDATE SET archived = :archived
                """
            ),
            {"fact_id": fact_id, "project_id": project_id, "archived": archived},
        )
        connection.execute(
            text("UPDATE kg_nodes SET weight = :weight WHERE id = :id AND project_id = :project_id"),
            {"weight": weight, "id": fact_id, "project_id": project_id},
        )

    def set_trust(
        self,
        connection: Connection,
        *,
        project_id: str,
        node_id: str,
        trust_tier: str,
        actor_kind: str,
    ) -> None:
        self.ensure_schema(connection)
        node = self._node_in_project(connection, project_id, node_id)
        _assert_can_write(actor_kind, trust_tier)
        if actor_kind != "person":
            raise PermissionError("only a person can change a trust tier")
        if node["trust_tier"] == "governed" and trust_tier != "governed":
            raise PermissionError("a governed fact is not demoted by promotion")
        connection.execute(
            text(
                """
                UPDATE kg_nodes
                SET trust_tier = :trust_tier, layer = :layer, recorded_at = :now
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "trust_tier": trust_tier,
                "layer": _LAYER_FOR_TIER[trust_tier],
                "now": now_utc(),
                "id": node_id,
                "project_id": project_id,
            },
        )

    def delete_node(
        self, connection: Connection, *, project_id: str, node_id: str, actor_kind: str
    ) -> None:
        self.ensure_schema(connection)
        node = self._node_in_project(connection, project_id, node_id)
        if actor_kind != "person" and node["trust_tier"] in {"governed", "approved"}:
            raise PermissionError("only a person can forget a governed fact")
        connection.execute(
            text("DELETE FROM kg_embeddings WHERE node_id = :id AND project_id = :project_id"),
            {"id": node_id, "project_id": project_id},
        )
        connection.execute(
            text("DELETE FROM kg_nodes WHERE id = :id AND project_id = :project_id"),
            {"id": node_id, "project_id": project_id},
        )

    def facts_about_source(
        self, connection: Connection, *, project_id: str, source_id: str
    ) -> list[str]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        rows = connection.execute(
            text(
                """
                SELECT id FROM kg_nodes
                WHERE project_id = :project_id
                  AND kind = 'fact'
                  AND (
                    attrs->>'capture_id' = :source_id
                    OR attrs->'evidence' ? :source_id
                  )
                """
            ),
            {"project_id": project_id, "source_id": source_id},
        ).scalars().all()
        return list(rows)

    def counts(self, connection: Connection, *, project_id: str) -> dict[str, int]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        nodes = connection.execute(
            text("SELECT COUNT(*) FROM kg_nodes WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
        edges = connection.execute(
            text("SELECT COUNT(*) FROM kg_edges WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
        facts = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM kg_nodes
                WHERE project_id = :project_id AND kind = 'fact'
                  AND (valid_to IS NULL OR valid_to > now())
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        return {"nodes": int(nodes), "edges": int(edges), "facts": int(facts)}
