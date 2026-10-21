"""RB-15 — the AI-SRE knowledge graph (spec 12 §1.5 / Phase 1).

A layered service/infra topology that is the substrate for the two things the SRE
agent can't do without it:

* **Root-cause context** — `subgraph()` returns the neighborhood around a failing
  service (its dependencies, the resources it runs on, the repo that owns it), so
  the investigation engine reasons over *this system*, not a blank page.
* **Feedback & learning** — when an investigation confirms a cause, `record_root_cause()`
  writes it back as a `caused_by` edge; `known_causes()` replays those on the next
  incident for the same service, so the graph gets smarter every loop.

Two layers: ``deterministic`` (walked from k8s/cloud/`infra_resources`, confidence 1.0)
and ``inferred`` (derived from traces/heuristics, confidence < 1). All persistence is
plain SQL against ``kg_nodes`` / ``kg_edges``; upserts are idempotent so repeated
scans/walks converge instead of duplicating.

BOSS-355 adds first-class provenance source, temporal validity (valid_from/valid_to),
confidence filters, and strict project/tenant scope on every read path.

BOSS-357 adds ``compute_retrieval_eval`` coverage metrics + a simple regression alarm
when coverage drops below a configured threshold relative to the prior snapshot.

Knowledge Graph Phase 0 (KNOWLEDGE_GRAPH_SPEC.md §5): writes and tenant scope now live in the
``KnowledgeStore`` (``services/knowledge/store.py``); this service is the SRE facade over it and
keeps its public API and its narrower node/edge kinds unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id, now_utc
from .knowledge.store import PostgresKnowledgeStore, Scope, parse_ts as _parse_ts

NODE_KINDS = ("service", "resource", "repo", "deployment", "datastore", "external",
              "incident", "root_cause")
EDGE_KINDS = ("depends_on", "runs_on", "deployed_from", "owns", "emits",
              "caused_by", "correlates_with")

# Default coverage floor used by compute_retrieval_eval when callers omit threshold.
DEFAULT_COVERAGE_THRESHOLD = 0.35

_SCHEMA_EXTRAS_SQL = """
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS kg_eval_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  coverage_score DOUBLE PRECISION NOT NULL,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  alarm BOOLEAN NOT NULL DEFAULT FALSE,
  threshold DOUBLE PRECISION NOT NULL DEFAULT 0.35,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kg_eval_snapshots_project
  ON kg_eval_snapshots(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_source
  ON kg_nodes(project_id, source);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_confidence
  ON kg_nodes(project_id, confidence);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_valid
  ON kg_nodes(project_id, valid_from, valid_to);
"""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _serialize_node(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("first_seen", "last_seen", "valid_from", "valid_to"):
        if key in out:
            out[key] = _iso(out[key])
    # attrs may arrive as str from some drivers
    attrs = out.get("attrs")
    if isinstance(attrs, str):
        try:
            out["attrs"] = json.loads(attrs)
        except json.JSONDecodeError:
            out["attrs"] = {}
    return out


class KnowledgeGraphService:
    def __init__(self, settings=None):
        self.settings = settings
        self._schema_ready = False
        self.store = PostgresKnowledgeStore(node_kinds=NODE_KINDS, edge_kinds=EDGE_KINDS)

    def ensure_schema(self, connection: Connection) -> None:
        """Idempotent column/table extras for provenance + eval (BOSS-355/357)."""
        if self._schema_ready:
            return
        for stmt in _SCHEMA_EXTRAS_SQL.split(";"):
            sql = stmt.strip()
            if sql:
                connection.execute(text(sql))
        self._schema_ready = True

    def _require_project(self, connection: Connection, project_id: str) -> None:
        """Tenant scope: reject unknown project_ids (hard fail, no cross-tenant leak)."""
        self.store.require_scope(connection, Scope(project_id))

    def _assert_nodes_in_project(
        self, connection: Connection, project_id: str, *node_ids: str
    ) -> None:
        """Ensure every referenced node id belongs to project_id (tenant isolation)."""
        self.store.assert_nodes_in_scope(connection, Scope(project_id), *node_ids)

    # --- upserts (idempotent) ------------------------------------------------
    def upsert_node(
        self,
        connection: Connection,
        project_id: str,
        kind: str,
        node_key: str,
        name: Optional[str] = None,
        *,
        attrs: Optional[dict] = None,
        layer: str = "deterministic",
        source: str = "manual",
        confidence: float = 1.0,
        valid_from: Any = None,
        valid_to: Any = None,
    ) -> str:
        """Create-or-merge a node keyed by (project, kind, node_key). Returns its id.

        On conflict: refresh name/last_seen, shallow-merge attrs, keep the stronger
        layer, take GREATEST confidence, and replace provenance source + temporal
        validity window when provided.
        """
        self.ensure_schema(connection)
        return self.store.upsert_node(
            connection, Scope(project_id), kind, node_key, name, attrs=attrs, layer=layer,
            source=source, confidence=confidence, valid_from=valid_from, valid_to=valid_to,
        )

    def upsert_edge(
        self,
        connection: Connection,
        project_id: str,
        src_id: str,
        dst_id: str,
        kind: str,
        *,
        attrs: Optional[dict] = None,
        layer: str = "deterministic",
        source: str = "manual",
        confidence: float = 1.0,
        valid_from: Any = None,
        valid_to: Any = None,
    ) -> str:
        """Create-or-merge a directed edge keyed by (project, src, dst, kind). Returns its id."""
        self.ensure_schema(connection)
        return self.store.upsert_edge(
            connection, Scope(project_id), src_id, dst_id, kind, attrs=attrs, layer=layer,
            source=source, confidence=confidence, valid_from=valid_from, valid_to=valid_to,
        )

    # --- reads ---------------------------------------------------------------
    def get_node(
        self, connection: Connection, project_id: str, kind: str, node_key: str
    ) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                "SELECT * FROM kg_nodes WHERE project_id = :p AND kind = :k AND node_key = :nk"
            ),
            {"p": project_id, "k": kind, "nk": node_key},
        ).mappings().first()
        return _serialize_node(dict(row)) if row else None

    def list_nodes(
        self,
        connection: Connection,
        project_id: str,
        *,
        kind: Optional[str] = None,
        node_key: Optional[str] = None,
        source: Optional[str] = None,
        layer: Optional[str] = None,
        min_confidence: Optional[float] = None,
        max_confidence: Optional[float] = None,
        valid_at: Any = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List nodes for a project with optional provenance / confidence / temporal filters.

        Temporal filter ``valid_at`` (default: now when not include_expired) keeps
        rows where valid_from IS NULL OR valid_from <= t, AND valid_to IS NULL OR valid_to >= t.
        """
        self.ensure_schema(connection)
        self._require_project(connection, project_id)
        clauses = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id, "limit": max(1, min(int(limit), 500))}
        if kind:
            clauses.append("kind = :k")
            params["k"] = kind
        if node_key:
            clauses.append("node_key = :nk")
            params["nk"] = node_key
        if source:
            clauses.append("source = :source")
            params["source"] = source
        if layer:
            clauses.append("layer = :layer")
            params["layer"] = layer
        if min_confidence is not None:
            clauses.append("confidence >= :min_c")
            params["min_c"] = float(min_confidence)
        if max_confidence is not None:
            clauses.append("confidence <= :max_c")
            params["max_c"] = float(max_confidence)
        if not include_expired:
            at = _parse_ts(valid_at) or now_utc()
            clauses.append(
                "(valid_from IS NULL OR valid_from <= :valid_at) "
                "AND (valid_to IS NULL OR valid_to >= :valid_at)"
            )
            params["valid_at"] = at
        rows = connection.execute(
            text(
                f"SELECT * FROM kg_nodes WHERE {' AND '.join(clauses)} "
                "ORDER BY last_seen DESC LIMIT :limit"
            ),
            params,
        ).mappings().all()
        return [_serialize_node(dict(r)) for r in rows]

    def neighbors(
        self,
        connection: Connection,
        node_id: str,
        *,
        project_id: Optional[str] = None,
        direction: str = "both",
        edge_kinds: Optional[Iterable[str]] = None,
    ) -> list[dict[str, Any]]:
        """Adjacent nodes with the edge that reached them. direction: out|in|both.

        When ``project_id`` is provided, both the seed node and neighbors are
        constrained to that tenant (BOSS-355).
        """
        self.ensure_schema(connection)
        if project_id:
            self._assert_nodes_in_project(connection, project_id, node_id)
        kinds = list(edge_kinds) if edge_kinds else None
        kind_clause = "" if not kinds else " AND e.kind = ANY(:kinds)"
        project_edge = " AND e.project_id = :p" if project_id else ""
        project_node = " AND n.project_id = :p" if project_id else ""
        sql = {
            "out": (
                f"SELECT e.kind AS edge_kind, e.confidence AS edge_confidence, "
                f"e.source AS edge_source, e.valid_from AS edge_valid_from, "
                f"e.valid_to AS edge_valid_to, 'out' AS dir, n.* "
                f"FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id "
                f"WHERE e.src_id = :nid{kind_clause}{project_edge}{project_node}"
            ),
            "in": (
                f"SELECT e.kind AS edge_kind, e.confidence AS edge_confidence, "
                f"e.source AS edge_source, e.valid_from AS edge_valid_from, "
                f"e.valid_to AS edge_valid_to, 'in' AS dir, n.* "
                f"FROM kg_edges e JOIN kg_nodes n ON n.id = e.src_id "
                f"WHERE e.dst_id = :nid{kind_clause}{project_edge}{project_node}"
            ),
        }
        if direction == "both":
            query = f"{sql['out']} UNION ALL {sql['in']}"
        else:
            query = sql[direction]
        params: dict[str, Any] = {"nid": node_id}
        if kinds:
            params["kinds"] = kinds
        if project_id:
            params["p"] = project_id
        rows = connection.execute(text(query), params).mappings().all()
        out = []
        for r in rows:
            item = _serialize_node(dict(r))
            if "edge_valid_from" in item:
                item["edge_valid_from"] = _iso(item["edge_valid_from"])
            if "edge_valid_to" in item:
                item["edge_valid_to"] = _iso(item["edge_valid_to"])
            out.append(item)
        return out

    def subgraph(
        self,
        connection: Connection,
        project_id: str,
        root_kind: str,
        root_key: str,
        *,
        depth: int = 2,
        edge_kinds: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        """The RCA context bundle: BFS out from a root node to `depth` hops."""
        self.ensure_schema(connection)
        root = self.get_node(connection, project_id, root_kind, root_key)
        if not root:
            return {"root": None, "nodes": [], "edges": []}
        seen_nodes: dict[str, dict] = {root["id"]: dict(root)}
        seen_edges: dict[tuple, dict] = {}
        frontier = [root["id"]]
        for _ in range(max(0, depth)):
            nxt: list[str] = []
            for nid in frontier:
                for hop in self.neighbors(
                    connection,
                    nid,
                    project_id=project_id,
                    direction="both",
                    edge_kinds=edge_kinds,
                ):
                    other_id = hop["id"]
                    ek = (nid, other_id, hop["edge_kind"], hop["dir"])
                    seen_edges.setdefault(
                        ek,
                        {
                            "from": nid,
                            "to": other_id,
                            "kind": hop["edge_kind"],
                            "dir": hop["dir"],
                            "confidence": hop["edge_confidence"],
                            "source": hop.get("edge_source"),
                        },
                    )
                    if other_id not in seen_nodes:
                        node = {
                            k: v
                            for k, v in hop.items()
                            if k
                            not in (
                                "edge_kind",
                                "edge_confidence",
                                "edge_source",
                                "edge_valid_from",
                                "edge_valid_to",
                                "dir",
                            )
                        }
                        seen_nodes[other_id] = node
                        nxt.append(other_id)
            frontier = nxt
            if not frontier:
                break
        return {
            "root": dict(root),
            "nodes": list(seen_nodes.values()),
            "edges": list(seen_edges.values()),
        }

    # --- learning write-back -------------------------------------------------
    def record_root_cause(
        self,
        connection: Connection,
        project_id: str,
        service_key: str,
        cause_kind: str,
        cause_key: str,
        cause_name: str,
        *,
        confidence: float,
        evidence: Optional[dict] = None,
    ) -> dict[str, str]:
        """Persist a CONFIRMED root cause as a `caused_by` edge from the service to a cause node."""
        self.ensure_schema(connection)
        self._require_project(connection, project_id)
        svc = self.upsert_node(connection, project_id, "service", service_key)
        cause = self.upsert_node(
            connection,
            project_id,
            cause_kind,
            cause_key,
            cause_name,
            layer="inferred",
            source="investigation",
            confidence=confidence,
            attrs={"evidence": evidence or {}},
        )
        edge = self.upsert_edge(
            connection,
            project_id,
            svc,
            cause,
            "caused_by",
            layer="inferred",
            source="investigation",
            confidence=confidence,
            attrs={"evidence": evidence or {}},
        )
        return {"service_id": svc, "cause_id": cause, "edge_id": edge}

    def known_causes(
        self,
        connection: Connection,
        project_id: str,
        service_key: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Prior confirmed causes for a service (ranked by confidence)."""
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT c.kind, c.node_key, c.name, e.confidence, e.attrs, e.last_seen,
                       e.source, e.valid_from, e.valid_to
                FROM kg_nodes s
                JOIN kg_edges e ON e.src_id = s.id AND e.kind = 'caused_by'
                                 AND e.project_id = s.project_id
                JOIN kg_nodes c ON c.id = e.dst_id AND c.project_id = s.project_id
                WHERE s.project_id = :p AND s.kind = 'service' AND s.node_key = :sk
                ORDER BY e.confidence DESC, e.last_seen DESC
                LIMIT :limit
                """
            ),
            {"p": project_id, "sk": service_key, "limit": limit},
        ).mappings().all()
        out = []
        for r in rows:
            item = dict(r)
            item["last_seen"] = _iso(item.get("last_seen"))
            item["valid_from"] = _iso(item.get("valid_from"))
            item["valid_to"] = _iso(item.get("valid_to"))
            attrs = item.get("attrs")
            if isinstance(attrs, str):
                try:
                    item["attrs"] = json.loads(attrs)
                except json.JSONDecodeError:
                    item["attrs"] = {}
            out.append(item)
        return out

    # --- deterministic seeding ----------------------------------------------
    def seed_from_infra_resources(
        self, connection: Connection, project_id: str
    ) -> dict[str, int]:
        """Walk the provisioned-inventory table into the deterministic layer."""
        self.ensure_schema(connection)
        self._require_project(connection, project_id)
        rows = connection.execute(
            text(
                """
                SELECT id, provider, resource_type, resource_name, region, external_resource_id
                FROM infra_resources
                WHERE project_id = :p AND status = 'active'
                """
            ),
            {"p": project_id},
        ).mappings().all()
        n = 0
        for r in rows:
            self.upsert_node(
                connection,
                project_id,
                "resource",
                r["external_resource_id"] or r["id"],
                r["resource_name"],
                attrs={
                    "provider": r["provider"],
                    "resource_type": r["resource_type"],
                    "region": r["region"],
                },
                layer="deterministic",
                source="infra_resources",
            )
            n += 1
        return {"resources": n}

    # --- retrieval evaluation (BOSS-357) ------------------------------------
    def compute_retrieval_eval(
        self,
        connection: Connection,
        project_id: str,
        *,
        coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Compute coverage metrics over the knowledge graph for a project.

        Metrics (all tenant-scoped to project_id):
        * node/edge totals and density
        * kind + layer + source distributions
        * services with at least one dependency / resource / known cause
        * orphan node ratio (no incident edges)
        * mean confidence
        * coverage_score in [0,1] combining the above

        Stores a snapshot and sets ``alarm=True`` when coverage_score falls below
        ``coverage_threshold`` *or* regresses more than 15% vs the previous snapshot.
        """
        self.ensure_schema(connection)
        self._require_project(connection, project_id)
        threshold = float(coverage_threshold)
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("coverage_threshold must be between 0 and 1")

        totals = connection.execute(
            text(
                """
                SELECT
                  (SELECT COUNT(*)::int FROM kg_nodes WHERE project_id = :p) AS nodes,
                  (SELECT COUNT(*)::int FROM kg_edges WHERE project_id = :p) AS edges,
                  (SELECT COALESCE(AVG(confidence), 0) FROM kg_nodes WHERE project_id = :p)
                    AS avg_node_confidence,
                  (SELECT COALESCE(AVG(confidence), 0) FROM kg_edges WHERE project_id = :p)
                    AS avg_edge_confidence,
                  (SELECT COUNT(*)::int FROM kg_nodes
                     WHERE project_id = :p AND kind = 'service') AS services,
                  (SELECT COUNT(*)::int FROM kg_nodes
                     WHERE project_id = :p AND kind = 'resource') AS resources,
                  (SELECT COUNT(*)::int FROM kg_nodes
                     WHERE project_id = :p AND layer = 'deterministic') AS deterministic_nodes,
                  (SELECT COUNT(*)::int FROM kg_nodes
                     WHERE project_id = :p AND layer = 'inferred') AS inferred_nodes
                """
            ),
            {"p": project_id},
        ).mappings().one()

        node_count = int(totals["nodes"] or 0)
        edge_count = int(totals["edges"] or 0)
        services = int(totals["services"] or 0)
        resources = int(totals["resources"] or 0)

        by_kind_rows = connection.execute(
            text(
                """
                SELECT kind, COUNT(*)::int AS n FROM kg_nodes
                WHERE project_id = :p GROUP BY kind ORDER BY kind
                """
            ),
            {"p": project_id},
        ).mappings().all()
        by_source_rows = connection.execute(
            text(
                """
                SELECT source, COUNT(*)::int AS n FROM kg_nodes
                WHERE project_id = :p GROUP BY source ORDER BY source
                """
            ),
            {"p": project_id},
        ).mappings().all()

        services_with_dep = 0
        services_with_resource = 0
        services_with_cause = 0
        if services:
            services_with_dep = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT s.id)::int
                        FROM kg_nodes s
                        JOIN kg_edges e ON e.src_id = s.id AND e.project_id = s.project_id
                        WHERE s.project_id = :p AND s.kind = 'service'
                          AND e.kind IN ('depends_on', 'runs_on', 'deployed_from')
                        """
                    ),
                    {"p": project_id},
                ).scalar()
                or 0
            )
            services_with_resource = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT s.id)::int
                        FROM kg_nodes s
                        JOIN kg_edges e ON e.src_id = s.id AND e.project_id = s.project_id
                        JOIN kg_nodes r ON r.id = e.dst_id AND r.kind = 'resource'
                        WHERE s.project_id = :p AND s.kind = 'service'
                        """
                    ),
                    {"p": project_id},
                ).scalar()
                or 0
            )
            services_with_cause = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT s.id)::int
                        FROM kg_nodes s
                        JOIN kg_edges e ON e.src_id = s.id AND e.kind = 'caused_by'
                                         AND e.project_id = s.project_id
                        WHERE s.project_id = :p AND s.kind = 'service'
                        """
                    ),
                    {"p": project_id},
                ).scalar()
                or 0
            )

        orphans = 0
        if node_count:
            orphans = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*)::int FROM kg_nodes n
                        WHERE n.project_id = :p
                          AND NOT EXISTS (
                            SELECT 1 FROM kg_edges e
                            WHERE e.project_id = :p
                              AND (e.src_id = n.id OR e.dst_id = n.id)
                          )
                        """
                    ),
                    {"p": project_id},
                ).scalar()
                or 0
            )

        # Composite coverage in [0, 1]
        has_graph = 1.0 if node_count > 0 else 0.0
        density = 0.0
        if node_count > 1:
            # undirected max edges = n*(n-1)/2; we have directed so use n*(n-1)
            max_edges = node_count * (node_count - 1)
            density = min(1.0, edge_count / max_edges) if max_edges else 0.0
        service_link_ratio = (
            (services_with_dep / services) if services else (0.5 if node_count else 0.0)
        )
        resource_coverage = (
            min(1.0, resources / max(services, 1)) if services else (1.0 if resources else 0.0)
        )
        cause_coverage = (services_with_cause / services) if services else 0.0
        non_orphan_ratio = (
            1.0 - (orphans / node_count) if node_count else 0.0
        )
        avg_conf = float(totals["avg_node_confidence"] or 0.0)
        det_ratio = (
            float(totals["deterministic_nodes"] or 0) / node_count if node_count else 0.0
        )

        coverage_score = round(
            0.20 * has_graph
            + 0.15 * density
            + 0.20 * service_link_ratio
            + 0.15 * resource_coverage
            + 0.10 * cause_coverage
            + 0.10 * non_orphan_ratio
            + 0.05 * avg_conf
            + 0.05 * det_ratio,
            4,
        )

        metrics = {
            "node_count": node_count,
            "edge_count": edge_count,
            "services": services,
            "resources": resources,
            "deterministic_nodes": int(totals["deterministic_nodes"] or 0),
            "inferred_nodes": int(totals["inferred_nodes"] or 0),
            "services_with_dependency": services_with_dep,
            "services_with_resource": services_with_resource,
            "services_with_known_cause": services_with_cause,
            "orphan_nodes": orphans,
            "edge_density": round(density, 4),
            "service_link_ratio": round(service_link_ratio, 4),
            "resource_coverage": round(resource_coverage, 4),
            "cause_coverage": round(cause_coverage, 4),
            "non_orphan_ratio": round(non_orphan_ratio, 4),
            "avg_node_confidence": round(avg_conf, 4),
            "avg_edge_confidence": round(float(totals["avg_edge_confidence"] or 0.0), 4),
            "deterministic_ratio": round(det_ratio, 4),
            "by_kind": {r["kind"]: int(r["n"]) for r in by_kind_rows},
            "by_source": {r["source"]: int(r["n"]) for r in by_source_rows},
            "coverage_score": coverage_score,
        }

        # Prior snapshot for regression detection
        prior = connection.execute(
            text(
                """
                SELECT coverage_score, created_at FROM kg_eval_snapshots
                WHERE project_id = :p
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"p": project_id},
        ).mappings().first()
        prior_score = float(prior["coverage_score"]) if prior else None
        regression = False
        if prior_score is not None and prior_score > 0:
            # Alarm if drop exceeds 15% relative to prior
            drop = (prior_score - coverage_score) / prior_score
            regression = drop > 0.15
        below_threshold = coverage_score < threshold
        alarm = below_threshold or regression

        alarm_reasons: list[str] = []
        if below_threshold:
            alarm_reasons.append(
                f"coverage_score {coverage_score} below threshold {threshold}"
            )
        if regression:
            alarm_reasons.append(
                f"coverage_score regressed from {prior_score} to {coverage_score}"
            )

        snapshot_id = None
        if persist:
            snapshot_id = make_id("kgeval")
            connection.execute(
                text(
                    """
                    INSERT INTO kg_eval_snapshots
                      (id, project_id, coverage_score, metrics, alarm, threshold)
                    VALUES
                      (:id, :p, :score, CAST(:metrics AS JSONB), :alarm, :threshold)
                    """
                ),
                {
                    "id": snapshot_id,
                    "p": project_id,
                    "score": coverage_score,
                    "metrics": json.dumps(metrics),
                    "alarm": alarm,
                    "threshold": threshold,
                },
            )

        return {
            "project_id": project_id,
            "coverage_score": coverage_score,
            "threshold": threshold,
            "alarm": alarm,
            "alarm_reasons": alarm_reasons,
            "prior_coverage_score": prior_score,
            "regression": regression,
            "below_threshold": below_threshold,
            "metrics": metrics,
            "snapshot_id": snapshot_id,
            "evaluated_at": _iso(now_utc()),
        }
