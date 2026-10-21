"""The knowledge store: one interface over BOSS's graph, vectors and text (KNOWLEDGE_GRAPH_SPEC.md §3.1, §5).

``KnowledgeStore`` is the protocol every caller codes against; ``PostgresKnowledgeStore`` is the
only implementation in v1 (one database: Postgres with AGE and pgvector). A different backend is
an adapter behind the same protocol, never a second code path in callers.

Phase 0 skeleton. What is real today:

* ``upsert_node`` / ``upsert_edge`` — the idempotent, provenance- and validity-carrying upserts
  over ``kg_nodes`` / ``kg_edges`` that ``KnowledgeGraphService`` has always used (now here).
* ``close_validity`` — supersession never deletes: it closes ``valid_to`` and records what
  replaced the element.
* ``traverse`` with a :class:`Pattern` — a scoped, bounded, time-aware recursive CTE over
  ``kg_edges`` (the Phase 0 benchmark's winner for 2–4 hops; see KNOWLEDGE_GRAPH_SPIKE.md).
* ``text_search`` — Postgres full text over node names and keys.
* ``hybrid_search`` — reciprocal-rank fusion of the candidate lists the store can produce.

``vector_search`` and Cypher traversal (Apache AGE) are declared and refused with
:class:`NotImplementedInPhase` until ``kg_embeddings`` (Phase 3) and the AGE projection land.

Scope is enforced HERE, not by callers: every read and write names a project, an unknown project
is refused, and an edge may only join nodes of its own project.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import make_id, now_utc

# Spec §4: the kinds the store accepts. The SRE facade narrows these to its own set.
NODE_KINDS = (
    "service", "resource", "repo", "deployment", "datastore", "external", "incident", "root_cause",
    "module", "file", "symbol", "endpoint", "table", "queue", "config_key",
    "spec", "decision", "pull_request", "review_finding", "release", "runbook", "alert_rule", "run",
    "person", "agent", "team",
    "fact", "topic",
)
EDGE_KINDS = (
    "depends_on", "runs_on", "deployed_from", "owns", "emits", "caused_by", "correlates_with",
    "defines", "calls", "imports", "reads", "writes", "publishes", "subscribes", "exposes", "tests",
    "specifies", "implements", "changed", "reviewed", "found_in", "shipped", "fired_on", "fixed_by",
    "decided_by", "approved_by", "about", "supersedes", "evidenced_by",
    # Phase 1: a non-call use of a symbol (type, base class, value) — blast radius needs it too.
    "references",
)
MAX_TRAVERSAL_DEPTH = 6
RRF_K = 60


class NotImplementedInPhase(NotImplementedError):
    """A KnowledgeStore capability that a later phase of the epic delivers."""


@dataclass(frozen=True)
class Scope:
    """Who may see what. Phase 0 enforces the project; org, team and Passport ceilings follow."""
    project_id: str
    org_id: str | None = None
    team_id: str | None = None


@dataclass(frozen=True)
class Pattern:
    """A bounded traversal: from seed node ids, along edge kinds, up to ``depth`` hops."""
    seeds: Sequence[str]
    depth: int = 2
    edge_kinds: Sequence[str] | None = None
    direction: Literal["out", "in", "both"] = "out"
    valid_at: Any = None          # None: now; validity is always applied to edges walked
    limit: int = 10_000


@dataclass
class Hit:
    id: str
    score: float
    sources: list[str] = field(default_factory=list)
    node: dict[str, Any] | None = None


@runtime_checkable
class KnowledgeStore(Protocol):
    def upsert_node(self, connection: Connection, scope: Scope, kind: str, node_key: str,
                    name: str | None = None, **fields: Any) -> str: ...

    def upsert_edge(self, connection: Connection, scope: Scope, src_id: str, dst_id: str, kind: str,
                    **fields: Any) -> str: ...

    def close_validity(self, connection: Connection, scope: Scope, element: Literal["node", "edge"],
                       element_id: str, *, valid_to: Any = None,
                       superseded_by: str | None = None) -> bool: ...

    def traverse(self, connection: Connection, scope: Scope, query: Pattern | str) -> dict[str, Any]: ...

    def vector_search(self, connection: Connection, scope: Scope, embedding: Sequence[float], *,
                      model_id: str, k: int = 20) -> list[Hit]: ...

    def text_search(self, connection: Connection, scope: Scope, query: str, *, k: int = 20) -> list[Hit]: ...

    def hybrid_search(self, connection: Connection, scope: Scope, query: str, *,
                      seeds: Sequence[str] = (), embedding: Sequence[float] | None = None,
                      model_id: str | None = None, k: int = 20) -> list[Hit]: ...


def parse_ts(value: Any) -> datetime | None:
    """Normalize optional timestamp inputs for SQL binds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=None)
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc


def _confidence(value: Any) -> float:
    value = 1.0 if value is None else float(value)
    if value < 0.0 or value > 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return value


def _window(valid_from: Any, valid_to: Any) -> tuple[datetime | None, datetime | None]:
    vf, vt = parse_ts(valid_from), parse_ts(valid_to)
    if vf is not None and vt is not None and vt < vf:
        raise ValueError("valid_to must be >= valid_from")
    return vf, vt


def reciprocal_rank_fusion(ranked: dict[str, Sequence[str]], *, k: int = RRF_K) -> list[Hit]:
    """Model-free fusion of ranked id lists (spec §3.5): score = Σ 1 / (k + rank)."""
    scores: dict[str, Hit] = {}
    for source, ids in ranked.items():
        for rank, node_id in enumerate(ids, start=1):
            hit = scores.setdefault(node_id, Hit(id=node_id, score=0.0))
            hit.score += 1.0 / (k + rank)
            hit.sources.append(source)
    return sorted(scores.values(), key=lambda h: (-h.score, h.id))


class PostgresKnowledgeStore:
    """KnowledgeStore over kg_nodes / kg_edges (system of record) in the BOSS Postgres."""

    def __init__(self, *, node_kinds: Iterable[str] = NODE_KINDS, edge_kinds: Iterable[str] = EDGE_KINDS):
        self.node_kinds = tuple(node_kinds)
        self.edge_kinds = tuple(edge_kinds)

    # --- scope ----------------------------------------------------------------
    def require_scope(self, connection: Connection, scope: Scope) -> None:
        """Tenant scope: reject unknown project_ids (hard fail, no cross-tenant leak)."""
        project_id = scope.project_id
        if not project_id or not str(project_id).strip():
            raise ValueError("project_id is required")
        exists = connection.execute(text("SELECT 1 FROM projects WHERE id = :p"), {"p": project_id}).scalar()
        if not exists:
            raise LookupError(f"project not found: {project_id}")

    def assert_nodes_in_scope(self, connection: Connection, scope: Scope, *node_ids: str) -> None:
        """Ensure every referenced node id belongs to the scope's project (tenant isolation)."""
        ids = [nid for nid in node_ids if nid]
        if not ids:
            return
        rows = connection.execute(
            text("SELECT id, project_id FROM kg_nodes WHERE id = ANY(:ids)"), {"ids": list(ids)}
        ).mappings().all()
        found = {r["id"]: r["project_id"] for r in rows}
        for nid in ids:
            if nid not in found:
                raise LookupError(f"node not found: {nid}")
            if found[nid] != scope.project_id:
                raise PermissionError(f"node {nid} is not in project {scope.project_id}")

    # --- writes -----------------------------------------------------------------
    def upsert_node(self, connection: Connection, scope: Scope, kind: str, node_key: str,
                    name: str | None = None, *, attrs: dict | None = None,
                    layer: str = "deterministic", source: str = "manual", confidence: float = 1.0,
                    valid_from: Any = None, valid_to: Any = None) -> str:
        """Create-or-merge a node keyed by (project, kind, node_key). Returns its id.

        On conflict: refresh name/last_seen, shallow-merge attrs, keep the stronger layer, take
        GREATEST confidence, and replace provenance source + temporal validity when provided.
        """
        self.require_scope(connection, scope)
        if kind not in self.node_kinds:
            raise ValueError(f"unknown node kind {kind!r}")
        confidence = _confidence(confidence)
        vf, vt = _window(valid_from, valid_to)
        return connection.execute(
            text(
                """
                INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, layer,
                                      source, confidence, valid_from, valid_to,
                                      first_seen, last_seen)
                VALUES (:id, :project_id, :kind, :node_key, :name, CAST(:attrs AS JSONB),
                        :layer, :source, :confidence, :valid_from, :valid_to, :now, :now)
                ON CONFLICT (project_id, kind, node_key) DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, kg_nodes.name),
                    attrs = kg_nodes.attrs || EXCLUDED.attrs,
                    -- a deterministic sighting outranks a prior inferred guess
                    layer = CASE WHEN EXCLUDED.layer = 'deterministic' THEN 'deterministic'
                                 ELSE kg_nodes.layer END,
                    confidence = GREATEST(kg_nodes.confidence, EXCLUDED.confidence),
                    source = EXCLUDED.source,
                    valid_from = COALESCE(EXCLUDED.valid_from, kg_nodes.valid_from),
                    valid_to = COALESCE(EXCLUDED.valid_to, kg_nodes.valid_to),
                    last_seen = EXCLUDED.last_seen
                RETURNING id
                """
            ),
            {"id": make_id("kgn"), "project_id": scope.project_id, "kind": kind, "node_key": node_key,
             "name": name or node_key, "attrs": json.dumps(attrs or {}), "layer": layer,
             "source": source, "confidence": confidence, "valid_from": vf, "valid_to": vt,
             "now": now_utc()},
        ).scalar_one()

    def upsert_edge(self, connection: Connection, scope: Scope, src_id: str, dst_id: str, kind: str, *,
                    attrs: dict | None = None, layer: str = "deterministic", source: str = "manual",
                    confidence: float = 1.0, valid_from: Any = None, valid_to: Any = None) -> str:
        """Create-or-merge a directed edge keyed by (project, src, dst, kind). Returns its id."""
        self.require_scope(connection, scope)
        if kind not in self.edge_kinds:
            raise ValueError(f"unknown edge kind {kind!r}")
        confidence = _confidence(confidence)
        self.assert_nodes_in_scope(connection, scope, src_id, dst_id)
        vf, vt = _window(valid_from, valid_to)
        return connection.execute(
            text(
                """
                INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer,
                                      source, confidence, valid_from, valid_to,
                                      first_seen, last_seen)
                VALUES (:id, :project_id, :src, :dst, :kind, CAST(:attrs AS JSONB),
                        :layer, :source, :confidence, :valid_from, :valid_to, :now, :now)
                ON CONFLICT (project_id, src_id, dst_id, kind) DO UPDATE SET
                    attrs = kg_edges.attrs || EXCLUDED.attrs,
                    confidence = GREATEST(kg_edges.confidence, EXCLUDED.confidence),
                    source = EXCLUDED.source,
                    valid_from = COALESCE(EXCLUDED.valid_from, kg_edges.valid_from),
                    valid_to = COALESCE(EXCLUDED.valid_to, kg_edges.valid_to),
                    last_seen = EXCLUDED.last_seen
                RETURNING id
                """
            ),
            {"id": make_id("kge"), "project_id": scope.project_id, "src": src_id, "dst": dst_id,
             "kind": kind, "attrs": json.dumps(attrs or {}), "layer": layer, "source": source,
             "confidence": confidence, "valid_from": vf, "valid_to": vt, "now": now_utc()},
        ).scalar_one()

    def close_validity(self, connection: Connection, scope: Scope, element: Literal["node", "edge"],
                       element_id: str, *, valid_to: Any = None,
                       superseded_by: str | None = None) -> bool:
        """End an element's validity (a contradiction supersedes; nothing is deleted).

        Returns False when it was already closed. ``superseded_by`` is recorded in attrs.
        """
        self.require_scope(connection, scope)
        table = {"node": "kg_nodes", "edge": "kg_edges"}[element]
        at = parse_ts(valid_to) or now_utc()
        extra = json.dumps({"superseded_by": superseded_by} if superseded_by else {})
        row = connection.execute(
            text(
                f"UPDATE {table} SET valid_to = :at, attrs = attrs || CAST(:extra AS JSONB) "
                "WHERE id = :id AND project_id = :p AND (valid_to IS NULL OR valid_to > :at) "
                "AND (valid_from IS NULL OR valid_from <= :at) RETURNING id"
            ),
            {"at": at, "extra": extra, "id": element_id, "p": scope.project_id},
        ).first()
        if row is None:
            exists = connection.execute(
                text(f"SELECT project_id FROM {table} WHERE id = :id"), {"id": element_id}
            ).scalar()
            if exists is None:
                raise LookupError(f"{element} not found: {element_id}")
            if exists != scope.project_id:
                raise PermissionError(f"{element} {element_id} is not in project {scope.project_id}")
            return False
        return True

    # --- bulk writes (code index) ----------------------------------------------------
    def upsert_nodes_bulk(self, connection: Connection, scope: Scope, rows: Sequence[dict], *, seen_at: datetime,
                          source: str, layer: str = "deterministic", chunk: int = 5000) -> dict[tuple[str, str], str]:
        """Upsert many nodes in a few statements; returns {(kind, node_key): id}.

        Each row: kind, key, name, attrs, repo, path. A node seen again is refreshed and, if its
        validity had been closed, reopened from ``seen_at`` — the code says it exists again.
        """
        self.require_scope(connection, scope)
        unknown = {r["kind"] for r in rows} - set(self.node_kinds)
        if unknown:
            raise ValueError(f"unknown node kinds {sorted(unknown)}")
        ids: dict[tuple[str, str], str] = {}
        for start in range(0, len(rows), chunk):
            part = rows[start:start + chunk]
            result = connection.execute(text(
                """
                INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, layer, source, confidence,
                                      valid_from, first_seen, last_seen, repo, path)
                SELECT u.id, :p, u.kind, u.key, u.name, CAST(u.attrs AS JSONB), :layer, :source, 1.0,
                       :seen, :seen, :seen, u.repo, u.path
                FROM unnest(CAST(:ids AS TEXT[]), CAST(:kinds AS TEXT[]), CAST(:keys AS TEXT[]),
                            CAST(:names AS TEXT[]), CAST(:attrs AS TEXT[]), CAST(:repos AS TEXT[]),
                            CAST(:paths AS TEXT[])) AS u(id, kind, key, name, attrs, repo, path)
                ON CONFLICT (project_id, kind, node_key) DO UPDATE SET
                    name = EXCLUDED.name, attrs = EXCLUDED.attrs, repo = EXCLUDED.repo, path = EXCLUDED.path,
                    source = EXCLUDED.source, layer = EXCLUDED.layer, last_seen = EXCLUDED.last_seen,
                    valid_from = CASE WHEN kg_nodes.valid_to IS NOT NULL THEN EXCLUDED.last_seen
                                      ELSE COALESCE(kg_nodes.valid_from, EXCLUDED.valid_from) END,
                    valid_to = NULL
                RETURNING id, kind, node_key
                """),
                {"p": scope.project_id, "layer": layer, "source": source, "seen": seen_at,
                 "ids": [make_id("kgn") for _ in part], "kinds": [r["kind"] for r in part],
                 "keys": [r["key"] for r in part], "names": [r.get("name") or r["key"] for r in part],
                 "attrs": [json.dumps(r.get("attrs") or {}) for r in part], "repos": [r.get("repo") for r in part],
                 "paths": [r.get("path") for r in part]})
            ids.update({(row.kind, row.node_key): row.id for row in result})
        return ids

    def upsert_edges_bulk(self, connection: Connection, scope: Scope, rows: Sequence[dict], *, seen_at: datetime,
                          source: str, layer: str = "deterministic", chunk: int = 5000) -> int:
        """Upsert many edges (src, dst, kind, attrs, confidence) between nodes of this scope.

        Callers pass node ids they got from this store for this scope; the statement re-checks
        that both ends belong to the project, so a foreign id writes nothing.
        """
        self.require_scope(connection, scope)
        unknown = {r["kind"] for r in rows} - set(self.edge_kinds)
        if unknown:
            raise ValueError(f"unknown edge kinds {sorted(unknown)}")
        written = 0
        for start in range(0, len(rows), chunk):
            part = rows[start:start + chunk]
            written += connection.execute(text(
                """
                INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer, source, confidence,
                                      valid_from, first_seen, last_seen)
                SELECT u.id, :p, u.src, u.dst, u.kind, CAST(u.attrs AS JSONB), :layer, :source, u.conf,
                       :seen, :seen, :seen
                FROM unnest(CAST(:ids AS TEXT[]), CAST(:srcs AS TEXT[]), CAST(:dsts AS TEXT[]),
                            CAST(:kinds AS TEXT[]), CAST(:attrs AS TEXT[]), CAST(:confs AS FLOAT8[]))
                     AS u(id, src, dst, kind, attrs, conf)
                JOIN kg_nodes s ON s.id = u.src AND s.project_id = :p
                JOIN kg_nodes d ON d.id = u.dst AND d.project_id = :p
                ON CONFLICT (project_id, src_id, dst_id, kind) DO UPDATE SET
                    attrs = EXCLUDED.attrs, confidence = EXCLUDED.confidence, source = EXCLUDED.source,
                    last_seen = EXCLUDED.last_seen,
                    valid_from = CASE WHEN kg_edges.valid_to IS NOT NULL THEN EXCLUDED.last_seen
                                      ELSE COALESCE(kg_edges.valid_from, EXCLUDED.valid_from) END,
                    valid_to = NULL
                """),
                {"p": scope.project_id, "layer": layer, "source": source, "seen": seen_at,
                 "ids": [make_id("kge") for _ in part], "srcs": [r["src"] for r in part],
                 "dsts": [r["dst"] for r in part], "kinds": [r["kind"] for r in part],
                 "attrs": [json.dumps(r.get("attrs") or {}) for r in part],
                 "confs": [float(r.get("confidence", 1.0)) for r in part]}).rowcount
        return written

    def close_unseen(self, connection: Connection, scope: Scope, *, repo: str, source: str, before: datetime,
                     paths: Sequence[str] | None = None) -> dict[str, int]:
        """Close (never delete) what an index run of ``repo`` did not see again.

        Nodes from the given files (all of the repo's files when ``paths`` is None) and the edges
        leaving them that were last seen before ``before`` get ``valid_to = before``.
        """
        self.require_scope(connection, scope)
        path_clause = "" if paths is None else " AND n.path = ANY(:paths)"
        params = {"p": scope.project_id, "repo": repo, "source": source, "before": before}
        if paths is not None:
            params["paths"] = list(paths)
        edges = connection.execute(text(
            f"""UPDATE kg_edges e SET valid_to = :before
                FROM kg_nodes n
                WHERE e.src_id = n.id AND n.project_id = :p AND n.repo = :repo{path_clause}
                  AND e.project_id = :p AND e.source = :source AND e.valid_to IS NULL AND e.last_seen < :before"""),
            params).rowcount
        nodes = connection.execute(text(
            f"""UPDATE kg_nodes n SET valid_to = :before
                WHERE n.project_id = :p AND n.repo = :repo{path_clause}
                  AND n.source = :source AND n.valid_to IS NULL AND n.last_seen < :before"""),
            params).rowcount
        # Edges INTO a node that just closed are no longer true either.
        into = connection.execute(text(
            """UPDATE kg_edges e SET valid_to = :before
               FROM kg_nodes n
               WHERE e.dst_id = n.id AND n.project_id = :p AND n.repo = :repo AND n.valid_to = :before
                 AND e.project_id = :p AND e.valid_to IS NULL"""), params).rowcount
        return {"nodes": nodes, "edges": edges + into}

    # --- reads --------------------------------------------------------------------
    def traverse(self, connection: Connection, scope: Scope, query: Pattern | str) -> dict[str, Any]:
        """Nodes reachable from the seeds within depth, with the hop count that first reached them.

        A recursive CTE over kg_edges: scoped to the project on every hop, edges valid at
        ``valid_at`` only, cycle-safe (UNION de-duplicates (node, depth); the outer MIN keeps the
        shortest), bounded by depth and limit.
        """
        if isinstance(query, str):
            raise NotImplementedInPhase("Cypher traversal arrives with the AGE projection (Phase 1)")
        self.require_scope(connection, scope)
        depth = max(0, min(int(query.depth), MAX_TRAVERSAL_DEPTH))
        seeds = [s for s in query.seeds if s]
        self.assert_nodes_in_scope(connection, scope, *seeds)
        if not seeds:
            return {"nodes": [], "edges_walked": 0}
        kinds = list(query.edge_kinds) if query.edge_kinds else None
        if kinds and set(kinds) - set(self.edge_kinds):
            raise ValueError(f"unknown edge kinds {sorted(set(kinds) - set(self.edge_kinds))}")
        steps = {
            "out": "SELECT e.dst_id AS id FROM kg_edges e WHERE e.src_id = w.id",
            "in": "SELECT e.src_id AS id FROM kg_edges e WHERE e.dst_id = w.id",
            "both": ("SELECT CASE WHEN e.src_id = w.id THEN e.dst_id ELSE e.src_id END AS id "
                     "FROM kg_edges e WHERE (e.src_id = w.id OR e.dst_id = w.id)"),
        }[query.direction]
        edge_filter = (" AND e.project_id = :p"
                       " AND (e.valid_from IS NULL OR e.valid_from <= :at)"
                       " AND (e.valid_to IS NULL OR e.valid_to >= :at)"
                       + (" AND e.kind = ANY(:kinds)" if kinds else ""))
        sql = f"""
            WITH RECURSIVE walk(id, depth) AS (
                SELECT unnest(CAST(:seeds AS TEXT[])), 0
                UNION
                SELECT nxt.id, w.depth + 1
                FROM walk w
                CROSS JOIN LATERAL ({steps}{edge_filter}) nxt
                WHERE w.depth < :depth
            ),
            reached AS (SELECT id, MIN(depth) AS depth FROM walk GROUP BY id)
            SELECT n.id, n.kind, n.node_key, n.name, n.layer, n.confidence, n.repo, n.path, r.depth
            FROM reached r JOIN kg_nodes n ON n.id = r.id AND n.project_id = :p
            ORDER BY r.depth, n.id
            LIMIT :limit
        """
        params: dict[str, Any] = {"seeds": seeds, "depth": depth, "p": scope.project_id,
                                  "at": parse_ts(query.valid_at) or now_utc(),
                                  "limit": max(1, int(query.limit))}
        if kinds:
            params["kinds"] = kinds
        rows = connection.execute(text(sql), params).mappings().all()
        return {"nodes": [dict(r) for r in rows], "depth": depth}

    def vector_search(self, connection: Connection, scope: Scope, embedding: Sequence[float], *,
                      model_id: str, k: int = 20) -> list[Hit]:
        raise NotImplementedInPhase("vector search needs kg_embeddings (pgvector), Phase 3")

    def text_search(self, connection: Connection, scope: Scope, query: str, *, k: int = 20) -> list[Hit]:
        """Full text over node names and keys, current nodes only. The stored tsvector column
        with its GIN index (spec §3.1) replaces the on-the-fly vector in Phase 3."""
        self.require_scope(connection, scope)
        if not query or not query.strip():
            return []
        rows = connection.execute(
            text(
                """
                SELECT id, kind, node_key, name,
                       ts_rank(to_tsvector('simple', name || ' ' || node_key), q) AS score
                FROM kg_nodes, websearch_to_tsquery('simple', :q) q
                WHERE project_id = :p AND (valid_to IS NULL OR valid_to >= now())
                  AND to_tsvector('simple', name || ' ' || node_key) @@ q
                ORDER BY score DESC, id LIMIT :k
                """
            ),
            {"q": query, "p": scope.project_id, "k": max(1, int(k))},
        ).mappings().all()
        return [Hit(id=r["id"], score=float(r["score"]), sources=["text"], node=dict(r)) for r in rows]

    def hybrid_search(self, connection: Connection, scope: Scope, query: str, *,
                      seeds: Sequence[str] = (), embedding: Sequence[float] | None = None,
                      model_id: str | None = None, k: int = 20) -> list[Hit]:
        """Fuse what the store can produce today: full text, and the graph neighbourhood of the
        task's seeds. Vector candidates join when ``vector_search`` exists (Phase 3)."""
        ranked: dict[str, Sequence[str]] = {"text": [h.id for h in self.text_search(connection, scope, query, k=k)]}
        if seeds:
            near = self.traverse(connection, scope, Pattern(seeds=seeds, depth=2, direction="both", limit=k * 5))
            ranked["graph"] = [n["id"] for n in near["nodes"] if n["depth"] > 0]
        if embedding is not None and model_id:
            ranked["vector"] = [h.id for h in self.vector_search(connection, scope, embedding, model_id=model_id, k=k)]
        return reciprocal_rank_fusion(ranked)[:k]
