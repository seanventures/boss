"""Postgres knowledge store for the Phase 0 retrieval slice.

kg_nodes and kg_edges stay the system of record, including the SRE topology
that KnowledgeGraphService already writes. This store adds trust tiers,
temporal close, full text, and embeddings. Traversal stays a recursive CTE.
An AGE projection is a separate comparison and is not this read path.

Vectors are double precision[]. When the vector extension is installed,
upsert copies rows into kg_vec_<dimensions> and builds an HNSW index.
Search uses that index when iterative scans are available, and exact cosine
otherwise.
An agent cannot write or close a governed or approved fact; the tier is
chosen by the caller role, not accepted from an untrusted payload.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import make_id, now_utc
from .scopes import StagePolicy, layer_sql_case

NODE_KINDS = frozenset({
    "service", "resource", "repo", "deployment", "datastore", "external",
    "incident", "root_cause",
    "module", "file", "symbol", "endpoint", "table", "queue", "config_key",
    "spec", "decision", "pull_request", "review_finding", "release", "runbook",
    "alert_rule", "run", "person", "agent", "team",
    "fact", "topic",
    # The Rules layer and what agents learn while they work (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.2).
    "rule", "fix", "precedent", "investigation", "test_failure", "workaround",
})
EDGE_KINDS = frozenset({
    "depends_on", "runs_on", "deployed_from", "owns", "emits", "caused_by",
    "correlates_with",
    "defines", "calls", "dispatches_to", "overrides", "references", "imports", "reads", "writes",
    "publishes", "subscribes", "exposes", "tests", "extends",
    "specifies", "implements", "changed", "reviewed", "found_in", "shipped",
    "fired_on", "fixed_by", "decided_by", "approved_by", "about", "supersedes",
    "evidenced_by",
    # Learning and rules provenance (§2.2): a run consulted or reused an item, who proposed a rule
    # or fact, what a run produced, what an investigation ruled out, a rule correcting another,
    # and where an approved rule applies.
    "consulted", "reused", "proposed_by", "produced", "ruled_out", "corrects", "governs",
})
TRUST_TIERS = frozenset({"observed", "governed", "inferred", "approved"})
#: kg_usage.use_kind (spec §2.5; the CHECK in db/schema.sql block "BOSSAPP-KGR rules + usage").
USE_KINDS = frozenset({"shown", "consulted", "cited", "reused"})
_IDENT_SPLIT = re.compile(r"[^A-Za-z0-9_]+")
_IDENT_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def identifier_terms(name: str) -> str:
    """The words inside a code name, for full-text search: each dotted/slashed part whole, then
    split at camelCase and snake_case boundaries. Postgres reads ``Processor.handleInAdvance``
    as one token, so a phrase ("payment in advance") never met it; with these terms it does,
    and the part itself ("handleInAdvance") still matches a query naming the symbol."""
    words: list[str] = []
    for part in _IDENT_SPLIT.split(name or ""):
        if not part.strip("_"):
            continue
        words.append(part)
        for segment in part.split("_"):
            split = _IDENT_WORD.findall(segment)
            words.extend([segment] if len(split) <= 1 else [segment, *split])
    return " ".join(w for w in dict.fromkeys(words) if w)
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
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS path TEXT;
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
CREATE INDEX IF NOT EXISTS idx_kg_nodes_repo_path ON kg_nodes(project_id, repo, path) WHERE repo IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_key ON kg_nodes(project_id, node_key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_name ON kg_nodes(project_id, name) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kg_edges_src_valid ON kg_edges(src_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kg_embeddings_project_model
  ON kg_embeddings(project_id, model_id);
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS knowledge_layer TEXT
  GENERATED ALWAYS AS (""" + layer_sql_case("kind") + """) STORED;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_layer
  ON kg_nodes(project_id, knowledge_layer, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_search_noncode
  ON kg_nodes USING GIN (search_tsv) WHERE knowledge_layer <> 'code';
CREATE TABLE IF NOT EXISTS kg_change_links (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  symbol_key TEXT NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, source_kind, source_id, symbol_key)
);
CREATE INDEX IF NOT EXISTS idx_kg_change_links_symbol ON kg_change_links(project_id, symbol_key);
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  use_kind TEXT NOT NULL DEFAULT 'shown',
  workflow_run_id TEXT,
  code_task_run_id TEXT,
  agent_role TEXT
);
ALTER TABLE kg_usage ADD COLUMN IF NOT EXISTS agent_thread_id TEXT;
ALTER TABLE kg_usage ADD COLUMN IF NOT EXISTS act_id TEXT;
ALTER TABLE kg_usage ADD COLUMN IF NOT EXISTS message_id TEXT;
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_ref TEXT
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


def _created_by(actor_kind: str, actor_ref: Optional[str]) -> str:
    """kg_*.created_by: who wrote the row when the caller knows (a person id, an agent role key),
    else the kind of actor."""
    ref = str(actor_ref or "").strip()
    return ref or actor_kind


def _assert_can_close(actor_kind: str, trust_tier: str) -> None:
    if actor_kind == "person":
        return
    if actor_kind == "agent" and trust_tier == "inferred":
        return
    if actor_kind == "system" and trust_tier in {"observed", "inferred"}:
        return
    raise PermissionError(f"{actor_kind} cannot close trust_tier {trust_tier}")


def _vector_numbers(values: list[float]) -> list[str]:
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
    return parts


def _vector_literal(values: list[float]) -> str:
    return "{" + ",".join(_vector_numbers(values)) + "}"


def _pgvector_literal(values: list[float]) -> str:
    return "[" + ",".join(_vector_numbers(values)) + "]"


def _vec_table(dimensions: int) -> str:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError("hnsw dimension must be an integer from 1 to 2000")
    if dimensions < 1 or dimensions > 2000:
        raise ValueError("hnsw dimension must be an integer from 1 to 2000")
    return f"kg_vec_{dimensions}"


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
        actor_ref: Optional[str] = None,
    ) -> str:
        """``actor_ref`` is who wrote it — a person id or an agent role key — and becomes
        ``created_by``; without it ``created_by`` is the actor kind, as before."""
        self.ensure_schema(connection)
        if kind not in NODE_KINDS:
            from .ontology_packs import effective_node_kinds

            if kind not in effective_node_kinds(connection, project_id=project_id):
                raise ValueError(
                    f"unknown node kind {kind!r}: no active ontology pack for project "
                    f"{project_id!r} grants it (core kinds, or an assigned pack's kinds, only)"
                )
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
                    "actor": _created_by(actor_kind, actor_ref),
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
                "actor": _created_by(actor_kind, actor_ref),
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
        attrs: Optional[dict] = None,
        actor_ref: Optional[str] = None,
    ) -> str:
        """One edge, unique per (project, src, dst, kind). ``attrs`` (evidence lines, a run's step)
        are written on insert and merged on a repeat; ``actor_ref`` becomes ``created_by``."""
        self.ensure_schema(connection)
        if kind not in EDGE_KINDS:
            from .ontology_packs import effective_edge_kinds

            if kind not in effective_edge_kinds(connection, project_id=project_id):
                raise ValueError(
                    f"unknown edge kind {kind!r}: no active ontology pack for project "
                    f"{project_id!r} grants it (core kinds, or an assigned pack's kinds, only)"
                )
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
                  :id, :project_id, :src, :dst, :kind, CAST(:attrs AS JSONB),
                  :layer, 'knowledge_store', 1.0, :org_id, :scope, :trust_tier,
                  1.0, :actor, :content_hash, :now, :now, :now
                )
                ON CONFLICT (project_id, src_id, dst_id, kind) DO UPDATE SET
                  last_seen = EXCLUDED.last_seen,
                  attrs = kg_edges.attrs || EXCLUDED.attrs
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
                "actor": _created_by(actor_kind, actor_ref),
                "attrs": json.dumps(attrs or {}),
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

    # --- the code graph, in bulk ---------------------------------------------------------------
    # The code indexer writes thousands of nodes per repository; one statement per chunk, not
    # three round trips per row. Code nodes are observed and written by the system; a row that a
    # person governed or approved is never overwritten (ON CONFLICT ... WHERE).

    def upsert_code_nodes(
        self,
        connection: Connection,
        *,
        project_id: str,
        rows: list[dict],
        seen_at,
        chunk: int = 2000,
    ) -> dict[tuple[str, str], str]:
        """Upsert code nodes; returns {(kind, node_key): id}. A node seen again is reopened."""
        self.ensure_schema(connection)
        org_id = self._org_id(connection, project_id, None)
        unknown = {r["kind"] for r in rows} - NODE_KINDS
        if unknown:
            raise ValueError(f"unknown node kinds {sorted(unknown)}")
        ids: dict[tuple[str, str], str] = {}
        for start in range(0, len(rows), chunk):
            part = rows[start:start + chunk]
            result = connection.execute(
                text(
                    """
                    INSERT INTO kg_nodes (
                      id, project_id, kind, node_key, name, body, attrs, layer, source, confidence,
                      org_id, scope, trust_tier, weight, created_by, content_hash, recorded_at,
                      search_tsv, valid_from, first_seen, last_seen, repo, path
                    )
                    SELECT u.id, :project_id, u.kind, u.key, u.name, u.body, CAST(u.attrs AS JSONB),
                           'deterministic', :source, 1.0, :org_id, :project_id, 'observed', 1.0,
                           'system', u.hash, :seen,
                           to_tsvector('english', u.name || ' ' || u.terms || ' ' || u.body),
                           :seen, :seen, :seen, u.repo, u.path
                    FROM unnest(
                      CAST(:ids AS TEXT[]), CAST(:kinds AS TEXT[]), CAST(:keys AS TEXT[]),
                      CAST(:names AS TEXT[]), CAST(:bodies AS TEXT[]), CAST(:attrs AS TEXT[]),
                      CAST(:hashes AS TEXT[]), CAST(:repos AS TEXT[]), CAST(:paths AS TEXT[]),
                      CAST(:terms AS TEXT[])
                    ) AS u(id, kind, key, name, body, attrs, hash, repo, path, terms)
                    ON CONFLICT (project_id, kind, node_key) DO UPDATE SET
                      name = EXCLUDED.name,
                      body = EXCLUDED.body,
                      attrs = EXCLUDED.attrs,
                      source = EXCLUDED.source,
                      content_hash = EXCLUDED.content_hash,
                      search_tsv = EXCLUDED.search_tsv,
                      repo = EXCLUDED.repo,
                      path = EXCLUDED.path,
                      last_seen = EXCLUDED.last_seen,
                      recorded_at = CASE
                        WHEN kg_nodes.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                        THEN EXCLUDED.recorded_at ELSE kg_nodes.recorded_at END,
                      valid_from = CASE
                        WHEN kg_nodes.valid_to IS NOT NULL THEN EXCLUDED.last_seen
                        ELSE kg_nodes.valid_from END,
                      valid_to = NULL
                    WHERE kg_nodes.trust_tier IN ('observed', 'inferred')
                    RETURNING id, kind, node_key
                    """
                ),
                {
                    "project_id": project_id,
                    "org_id": org_id,
                    "source": "code_index",
                    "seen": seen_at,
                    "ids": [make_id("kgn") for _ in part],
                    "kinds": [r["kind"] for r in part],
                    "keys": [r["key"] for r in part],
                    "names": [r.get("name") or r["key"] for r in part],
                    "bodies": [r.get("body") or "" for r in part],
                    "attrs": [json.dumps(r.get("attrs") or {}, default=str) for r in part],
                    "hashes": [
                        _content_hash(r["kind"], r["key"], r.get("name") or r["key"],
                                      r.get("body") or "", "observed")
                        for r in part
                    ],
                    "repos": [r.get("repo") for r in part],
                    "paths": [r.get("path") for r in part],
                    "terms": [identifier_terms(r.get("name") or r["key"]) for r in part],
                },
            )
            ids.update({(row.kind, row.node_key): row.id for row in result})
        return ids

    def upsert_code_edges(
        self,
        connection: Connection,
        *,
        project_id: str,
        rows: list[dict],
        seen_at,
        chunk: int = 5000,
    ) -> int:
        """Upsert code edges between nodes of this project; a foreign id writes nothing."""
        self.ensure_schema(connection)
        org_id = self._org_id(connection, project_id, None)
        unknown = {r["kind"] for r in rows} - EDGE_KINDS
        if unknown:
            raise ValueError(f"unknown edge kinds {sorted(unknown)}")
        written = 0
        for start in range(0, len(rows), chunk):
            part = rows[start:start + chunk]
            written += connection.execute(
                text(
                    """
                    INSERT INTO kg_edges (
                      id, project_id, src_id, dst_id, kind, attrs, layer, source, confidence,
                      org_id, scope, trust_tier, weight, created_by, recorded_at, valid_from,
                      first_seen, last_seen
                    )
                    SELECT u.id, :project_id, u.src, u.dst, u.kind, CAST(u.attrs AS JSONB),
                           'deterministic', :source, u.conf, :org_id, :project_id, 'observed', 1.0,
                           'system', :seen, :seen, :seen, :seen
                    FROM unnest(
                      CAST(:ids AS TEXT[]), CAST(:srcs AS TEXT[]), CAST(:dsts AS TEXT[]),
                      CAST(:kinds AS TEXT[]), CAST(:attrs AS TEXT[]), CAST(:confs AS FLOAT8[])
                    ) AS u(id, src, dst, kind, attrs, conf)
                    JOIN kg_nodes s ON s.id = u.src AND s.project_id = :project_id
                    JOIN kg_nodes d ON d.id = u.dst AND d.project_id = :project_id
                    ON CONFLICT (project_id, src_id, dst_id, kind) DO UPDATE SET
                      attrs = EXCLUDED.attrs,
                      confidence = EXCLUDED.confidence,
                      source = EXCLUDED.source,
                      last_seen = EXCLUDED.last_seen,
                      valid_from = CASE
                        WHEN kg_edges.valid_to IS NOT NULL THEN EXCLUDED.last_seen
                        ELSE kg_edges.valid_from END,
                      valid_to = NULL
                    WHERE kg_edges.trust_tier IN ('observed', 'inferred')
                    """
                ),
                {
                    "project_id": project_id,
                    "org_id": org_id,
                    "source": "code_index",
                    "seen": seen_at,
                    "ids": [make_id("kge") for _ in part],
                    "srcs": [r["src"] for r in part],
                    "dsts": [r["dst"] for r in part],
                    "kinds": [r["kind"] for r in part],
                    "attrs": [json.dumps(r.get("attrs") or {}) for r in part],
                    "confs": [float(r.get("confidence", 1.0)) for r in part],
                },
            ).rowcount
        return written

    def code_node_ids(self, connection: Connection, *, project_id: str, keys: list[str]) -> dict[str, str]:
        """{node_key: id} for open nodes of this project."""
        self.ensure_schema(connection)
        found: dict[str, str] = {}
        for start in range(0, len(keys), 5000):
            rows = connection.execute(
                text(
                    """
                    SELECT node_key, id FROM kg_nodes
                    WHERE project_id = :project_id AND node_key = ANY(:keys) AND valid_to IS NULL
                    """
                ),
                {"project_id": project_id, "keys": list(keys[start:start + 5000])},
            )
            found.update({row.node_key: row.id for row in rows})
        return found

    def code_file_facts(
        self,
        connection: Connection,
        *,
        project_id: str,
        repo: str,
        paths: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """The cached per-file facts of a repository's open file nodes."""
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        path_sql = "" if paths is None else "AND path = ANY(:paths)"
        rows = connection.execute(
            text(
                f"""
                SELECT path, attrs FROM kg_nodes
                WHERE project_id = :project_id AND kind = 'file' AND source = 'code_index'
                  AND repo = :repo AND valid_to IS NULL {path_sql}
                """
            ),
            {"project_id": project_id, "repo": repo, "paths": list(paths or [])},
        ).mappings().all()
        return [dict(row) for row in rows]

    def code_repo_attrs(self, connection: Connection, *, project_id: str, node_key: str) -> dict:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                """
                SELECT attrs FROM kg_nodes
                WHERE project_id = :project_id AND kind = 'repo' AND node_key = :node_key
                """
            ),
            {"project_id": project_id, "node_key": node_key},
        ).scalar()
        if isinstance(row, str):
            return json.loads(row)
        return dict(row or {})

    def close_unseen_code(
        self,
        connection: Connection,
        *,
        project_id: str,
        repo: str,
        before,
        paths: Optional[list[str]] = None,
    ) -> dict[str, int]:
        """Close (never delete) the code an index run of ``repo`` did not see again.

        Nodes from the given files (every file of the repository when ``paths`` is None) that were
        last seen before ``before``, the code edges leaving them, and every observed edge into a
        node that just closed get ``valid_to = now()``: the transaction's time, so a read later in
        the same transaction (``valid_to > now()``) already sees them closed.
        """
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        if paths is None:
            # A whole-tree index just wrote tens of thousands of rows in this transaction; planned
            # on the statistics of the empty tables, the joins below went quadratic (Fineract:
            # 156k edges x 51k nodes, still running after 10 minutes). ANALYZE samples this
            # transaction's own rows.
            connection.execute(text("ANALYZE kg_nodes"))
            connection.execute(text("ANALYZE kg_edges"))
        path_sql = "" if paths is None else "AND n.path = ANY(:paths)"
        params = {"project_id": project_id, "repo": repo, "before": before, "paths": list(paths or [])}
        edges = connection.execute(
            text(
                f"""
                UPDATE kg_edges e SET valid_to = now()
                WHERE e.project_id = :project_id AND e.source = 'code_index'
                  AND e.valid_to IS NULL AND e.last_seen < :before
                  AND e.src_id IN (SELECT n.id FROM kg_nodes n
                                   WHERE n.project_id = :project_id AND n.repo = :repo {path_sql})
                """
            ),
            params,
        ).rowcount
        nodes = connection.execute(
            text(
                f"""
                UPDATE kg_nodes n SET valid_to = now()
                WHERE n.project_id = :project_id AND n.repo = :repo {path_sql}
                  AND n.source = 'code_index' AND n.valid_to IS NULL AND n.last_seen < :before
                  AND n.trust_tier IN ('observed', 'inferred')
                """
            ),
            params,
        ).rowcount
        into = connection.execute(
            text(
                """
                UPDATE kg_edges e SET valid_to = now()
                WHERE e.project_id = :project_id AND e.valid_to IS NULL
                  AND e.trust_tier IN ('observed', 'inferred')
                  AND e.dst_id IN (SELECT n.id FROM kg_nodes n
                                   WHERE n.project_id = :project_id AND n.valid_to = now())
                """
            ),
            params,
        ).rowcount
        return {"nodes": int(nodes), "edges": int(edges + into)}

    # --- durable record -> symbol links -----------------------------------------------------
    def record_change_links(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_kind: str,
        source_id: str,
        symbol_keys: list[str],
    ) -> int:
        """Remember which symbols a record changed, by natural key. Idempotent."""
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        if not symbol_keys:
            return 0
        return connection.execute(
            text(
                """
                INSERT INTO kg_change_links (project_id, source_kind, source_id, symbol_key)
                SELECT :project_id, :source_kind, :source_id, k FROM unnest(CAST(:keys AS TEXT[])) AS k
                ON CONFLICT DO NOTHING
                """
            ),
            {"project_id": project_id, "source_kind": source_kind, "source_id": source_id,
             "keys": list(dict.fromkeys(symbol_keys))},
        ).rowcount

    def change_links(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_kind: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Links whose record node and symbol node are both in the graph now, with their ids."""
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT l.source_kind, l.source_id, l.symbol_key, r.id AS record_id, s.id AS symbol_id
                FROM kg_change_links l
                JOIN kg_nodes r ON r.project_id = l.project_id AND r.kind = l.source_kind
                               AND r.node_key = l.source_kind || ':' || l.source_id
                JOIN kg_nodes s ON s.project_id = l.project_id AND s.kind = 'symbol'
                               AND s.node_key = l.symbol_key AND s.valid_to IS NULL
                WHERE l.project_id = :project_id
                  AND (CAST(:source_kind AS TEXT) IS NULL OR l.source_kind = :source_kind)
                  AND (CAST(:source_id AS TEXT) IS NULL OR l.source_id = :source_id)
                ORDER BY l.symbol_key
                """
            ),
            {"project_id": project_id, "source_kind": source_kind, "source_id": source_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    # --- stage knowledge scopes ----------------------------------------------------------------
    def code_reach(
        self, connection: Connection, *, project_id: str, policy: StagePolicy
    ) -> list[str]:
        """The code nodes a seeded stage may read: its seeds, then at most ``max_hops`` hops
        between code nodes in either direction, ``max_nodes`` in all. No seeds, no code.

        A hop is a dependency (calls, references, imports, exposes, reads, writes, publishes,
        subscribes), never ``defines``: a file is not a step from one of its symbols to the rest."""
        if not policy.reads_code or not policy.seeds:
            return []
        reach = [row[0] for row in connection.execute(
            text(
                """
                SELECT id FROM kg_nodes
                WHERE project_id = :project_id AND id = ANY(:seeds)
                  AND knowledge_layer = 'code' AND (valid_to IS NULL OR valid_to > now())
                ORDER BY id
                """
            ),
            {"project_id": project_id, "seeds": list(policy.seeds)},
        )][: policy.max_nodes]
        frontier = list(reach)
        for _hop in range(policy.max_hops):
            if not frontier or len(reach) >= policy.max_nodes:
                break
            rows = connection.execute(
                text(
                    """
                    SELECT DISTINCT n.id, n.node_key FROM kg_edges e
                    JOIN kg_nodes n ON n.id = CASE WHEN e.src_id = ANY(:ids) THEN e.dst_id ELSE e.src_id END
                    WHERE e.project_id = :project_id
                      AND (e.src_id = ANY(:ids) OR e.dst_id = ANY(:ids))
                      AND e.kind <> 'defines'
                      AND (e.valid_to IS NULL OR e.valid_to > now())
                      AND n.project_id = :project_id AND n.knowledge_layer = 'code'
                      AND (n.valid_to IS NULL OR n.valid_to > now())
                    ORDER BY n.node_key
                    """
                ),
                {"project_id": project_id, "ids": frontier},
            ).all()
            seen = set(reach)
            frontier = []
            for node_id, _key in rows:
                if node_id in seen:
                    continue
                if len(reach) >= policy.max_nodes:
                    break
                seen.add(node_id)
                reach.append(node_id)
                frontier.append(node_id)
        return reach

    def _scope_sql(
        self, connection: Connection, project_id: str, policy: Optional[StagePolicy], alias: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """The layer filter a stage's reads carry. Empty without a policy (a person, BOSS)."""
        if policy is None:
            return "", {}
        col = f"{alias}." if alias else ""
        params: dict[str, Any] = {"scope_layers": sorted(policy.layers - {"code"})}
        # A proposed (unapproved) rule is never trusted input: a stage reads approved rules only.
        # The review queue and kg_rules(status='proposed') read them explicitly, labelled.
        rules = f" AND ({col}knowledge_layer <> 'rules' OR {col}trust_tier = 'approved')"
        if policy.reads_code:
            params["scope_code_ids"] = self.code_reach(connection, project_id=project_id, policy=policy)
            return (f" AND ({col}knowledge_layer = ANY(:scope_layers) OR ({col}knowledge_layer = 'code'"
                    f" AND {col}id = ANY(:scope_code_ids))){rules}", params)
        # The literal lets the planner use the partial non-code index.
        return (f" AND {col}knowledge_layer <> 'code' AND {col}knowledge_layer = ANY(:scope_layers)"
                f"{rules}", params)

    def assert_readable(
        self, connection: Connection, *, project_id: str, node_id: str, policy: Optional[StagePolicy]
    ) -> None:
        """Refuse, in words, a node outside the stage's scope."""
        if policy is None:
            return
        layer = connection.execute(
            text("SELECT knowledge_layer FROM kg_nodes WHERE id = :id AND project_id = :project_id"),
            {"id": node_id, "project_id": project_id},
        ).scalar()
        if layer is None:
            return
        if layer == "code":
            if not policy.reads_code or node_id not in self.code_reach(
                    connection, project_id=project_id, policy=policy):
                raise PermissionError(policy.refusal())
        elif layer not in policy.layers:
            raise PermissionError(f"The {layer} layer isn't available in {policy.stage}.")

    def text_search(
        self,
        connection: Connection,
        *,
        project_id: str,
        query: str,
        limit: int = 20,
        policy: Optional[StagePolicy] = None,
        exclude_layers: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        if not query or not query.strip():
            raise ValueError("query is required")
        scope_sql, scope_params = self._scope_sql(connection, project_id, policy)
        if exclude_layers:
            scope_sql += " AND NOT (knowledge_layer = ANY(:exclude_layers))"
            scope_params = {**scope_params, "exclude_layers": list(exclude_layers)}
        limit = max(1, min(int(limit), 50))
        params = {"project_id": project_id, "query": query, "limit": limit, **scope_params}
        # Every word first; when that leaves room, any word, ranked by how many it matches: a
        # phrase ("repayment allocation in advance") names the parts of several symbols, rarely
        # all of them in one.
        hits: list[dict[str, Any]] = []
        for tsquery in ("plainto_tsquery('english', :query)",
                        "CAST(replace(CAST(plainto_tsquery('english', :query) AS TEXT), '&', '|') AS tsquery)"):
            if len(hits) >= limit:
                break
            rows = connection.execute(
                text(
                    f"""
                    SELECT id AS node_id, node_key, ts_rank(search_tsv, q) AS rank
                    FROM kg_nodes, {tsquery} AS q
                    WHERE project_id = :project_id
                      AND search_tsv @@ q
                      AND (valid_to IS NULL OR valid_to > now()){scope_sql}
                    ORDER BY rank DESC, node_key ASC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
            seen = {hit["node_id"] for hit in hits}
            hits += [dict(row) for row in rows if row["node_id"] not in seen]
        return hits[:limit]

    def _vector_extension_available(self, connection: Connection) -> bool:
        return (
            connection.execute(
                text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
            ).first()
            is not None
        )

    def vector_extension_version(self, connection: Connection) -> Optional[str]:
        return connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar()

    def vector_index_kind(self, connection: Connection) -> str:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_index i ON i.indrelid = c.oid
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_am am ON am.oid = ic.relam
                WHERE n.nspname = 'public'
                  AND c.relname ~ '^kg_vec_[0-9]+$'
                  AND am.amname = 'hnsw'
                LIMIT 1
                """
            )
        ).first()
        return "hnsw" if row else "not_created"

    def _hnsw_ready(self, connection: Connection, dimensions: int) -> bool:
        table = _vec_table(dimensions)
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM pg_class c
                JOIN pg_index i ON i.indrelid = c.oid
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_am am ON am.oid = ic.relam
                WHERE c.relname = :table AND am.amname = 'hnsw'
                LIMIT 1
                """
            ),
            {"table": table},
        ).first()
        return row is not None

    def _iterative_hnsw(self, connection: Connection) -> bool:
        nested = connection.begin_nested()
        try:
            connection.execute(
                text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
            )
            nested.commit()
            return True
        except Exception:
            nested.rollback()
            return False

    def sync_vector_index(self, connection: Connection, *, dimensions: int) -> str:
        """Build kg_vec_<dim> and its HNSW index when pgvector is installed."""
        self.ensure_schema(connection)
        if not self._vector_extension_available(connection):
            return "not_created"
        table = _vec_table(dimensions)
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                  embedding_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  node_id TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  embedding vector({dimensions}) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE INDEX IF NOT EXISTS {table}_hnsw
                ON {table} USING hnsw (embedding vector_cosine_ops)
                """
            )
        )
        connection.execute(
            text(
                f"""
                INSERT INTO {table} (
                  embedding_id, project_id, node_id, model_id, content_hash, embedding
                )
                SELECT e.id, e.project_id, e.node_id, e.model_id, e.content_hash,
                       CAST(translate(e.embedding::text, '{{}}', '[]') AS vector)
                FROM kg_embeddings e
                WHERE e.dimensions = :dim
                ON CONFLICT (embedding_id) DO UPDATE SET
                  project_id = EXCLUDED.project_id,
                  node_id = EXCLUDED.node_id,
                  model_id = EXCLUDED.model_id,
                  content_hash = EXCLUDED.content_hash,
                  embedding = EXCLUDED.embedding
                """
            ),
            {"dim": dimensions},
        )
        return "hnsw"

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
        embedding_id = connection.execute(
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
                "id": make_id("emb"),
                "project_id": project_id,
                "node_id": node_id,
                "model_id": model_id,
                "dimensions": len(embedding),
                "embedding": literal,
                "content_hash": content_hash,
            },
        ).scalar_one()
        if self._vector_extension_available(connection) and 1 <= len(embedding) <= 2000:
            self.sync_vector_index(connection, dimensions=len(embedding))
        return embedding_id

    def vector_search(
        self,
        connection: Connection,
        *,
        project_id: str,
        model_id: str,
        embedding: list[float],
        limit: int = 20,
        policy: Optional[StagePolicy] = None,
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        literal = _vector_literal(embedding)
        scope_sql, scope_params = self._scope_sql(connection, project_id, policy, alias="n")
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
        bound = max(1, min(int(limit), 50))
        if self._hnsw_ready(connection, len(embedding)) and self._iterative_hnsw(connection):
            table = _vec_table(len(embedding))
            rows = connection.execute(
                text(
                    f"""
                    SELECT node_id, node_key, cosine FROM (
                      SELECT n.id AS node_id, n.node_key,
                             1 - ranked.distance AS cosine
                      FROM (
                        SELECT v.embedding_id,
                               v.embedding <=> CAST(:query AS vector) AS distance
                        FROM {table} v
                        WHERE v.project_id = :project_id
                          AND v.model_id = :model_id
                        ORDER BY v.embedding <=> CAST(:query AS vector)
                        LIMIT :limit
                      ) ranked
                      JOIN kg_embeddings e ON e.id = ranked.embedding_id
                      JOIN kg_nodes n
                        ON n.id = e.node_id AND n.content_hash = e.content_hash
                      WHERE n.project_id = :project_id
                        AND (n.valid_to IS NULL OR n.valid_to > now()){scope_sql}
                    ) scored
                    WHERE cosine > 0
                    ORDER BY cosine DESC, node_key ASC
                    """
                ),
                {
                    "project_id": project_id,
                    "model_id": model_id,
                    "query": _pgvector_literal(embedding),
                    "limit": bound,
                    **scope_params,
                },
            ).mappings().all()
            return [dict(row) for row in rows]
        rows = connection.execute(
            text(
                f"""
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
                    AND (n.valid_to IS NULL OR n.valid_to > now()){scope_sql}
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
                "limit": bound,
                **scope_params,
            },
        ).mappings().all()
        return [dict(row) for row in rows]

    def graph_candidates(
        self,
        connection: Connection,
        *,
        project_id: str,
        seed_ids: list[str],
        policy: Optional[StagePolicy] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Nodes one hop from the seeds (either direction, not ``defines``), most-linked first:
        the graph's list for hybrid retrieval (facts about a symbol, the incident a fix closed)."""
        if not seed_ids:
            return []
        scope_sql, scope_params = self._scope_sql(connection, project_id, policy, alias="n")
        rows = connection.execute(
            text(
                f"""
                SELECT n.id AS node_id, n.node_key, COUNT(*) AS links
                FROM kg_edges e
                JOIN kg_nodes n ON n.id = CASE WHEN e.src_id = ANY(:seeds) THEN e.dst_id ELSE e.src_id END
                WHERE e.project_id = :project_id
                  AND (e.src_id = ANY(:seeds) OR e.dst_id = ANY(:seeds))
                  AND e.kind <> 'defines'
                  AND (e.valid_to IS NULL OR e.valid_to > now())
                  AND n.project_id = :project_id AND NOT n.id = ANY(:seeds)
                  AND (n.valid_to IS NULL OR n.valid_to > now()){scope_sql}
                GROUP BY n.id, n.node_key
                ORDER BY COUNT(*) DESC, n.node_key
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "seeds": list(seed_ids), "limit": max(1, min(int(limit), 100)),
             **scope_params},
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
        policy: Optional[StagePolicy] = None,
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self._node_in_project(connection, project_id, node_id)
        self.assert_readable(connection, project_id=project_id, node_id=node_id, policy=policy)
        scope_sql, scope_params = self._scope_sql(connection, project_id, policy, alias="n")
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
                  AND (n.valid_to IS NULL OR n.valid_to > now()){scope_sql}
                ORDER BY n.node_key
                """
            ),
            {"node_id": node_id, "project_id": project_id, "kind": kind, **scope_params},
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
        actor_ref: Optional[str] = None,
    ) -> str:
        """``actor_ref``: who acted (a person id or an agent role key), beside what kind of actor."""
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        audit_id = make_id("kga")
        connection.execute(
            text(
                """
                INSERT INTO kg_audit (id, project_id, action, entity_id, actor_kind, actor_ref, detail)
                VALUES (:id, :project_id, :action, :entity_id, :actor_kind, :actor_ref,
                        CAST(:detail AS JSONB))
                """
            ),
            {
                "id": audit_id,
                "project_id": project_id,
                "action": action,
                "entity_id": entity_id,
                "actor_kind": actor_kind,
                "actor_ref": actor_ref,
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
        use_kind: str = "shown",
        workflow_run_id: Optional[str] = None,
        code_task_run_id: Optional[str] = None,
        agent_role: Optional[str] = None,
        agent_thread_id: Optional[str] = None,
        act_id: Optional[str] = None,
        message_id: Optional[str] = None,
        at: Optional[Any] = None,
    ) -> int:
        """Record that a run used these items (spec §2.5): ``shown`` (in the injected pack, no
        edge), ``consulted`` (a tool returned it), ``cited`` (the output named it), ``reused``.
        A conversation's use names its thread, turn (act) and answering message instead of a
        run; ``run_id`` is then the thread id."""
        self.ensure_schema(connection)
        self._org_id(connection, project_id, None)
        if use_kind not in USE_KINDS:
            raise ValueError(f"unknown use_kind {use_kind!r}")
        written = 0
        for fact_id in fact_ids:
            self._node_in_project(connection, project_id, fact_id)
            connection.execute(
                text(
                    """
                    INSERT INTO kg_usage (id, project_id, run_id, step, fact_id, use_kind,
                                          workflow_run_id, code_task_run_id, agent_role,
                                          agent_thread_id, act_id, message_id, created_at)
                    VALUES (:id, :project_id, :run_id, :step, :fact_id, :use_kind,
                            :workflow_run_id, :code_task_run_id, :agent_role,
                            :agent_thread_id, :act_id, :message_id,
                            coalesce(CAST(:at AS TIMESTAMPTZ), now()))
                    """
                ),
                {
                    "id": make_id("use"),
                    "project_id": project_id,
                    "run_id": run_id,
                    "step": step,
                    "fact_id": fact_id,
                    "use_kind": use_kind,
                    "workflow_run_id": workflow_run_id,
                    "code_task_run_id": code_task_run_id,
                    "agent_role": agent_role,
                    "agent_thread_id": agent_thread_id,
                    "act_id": act_id,
                    "message_id": message_id,
                    "at": at,
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
        from .age import forget_vertex

        forget_vertex(connection, project_id, node_id)     # a forget must not linger in Cypher

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
