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
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id, now_utc

NODE_KINDS = ("service", "resource", "repo", "deployment", "datastore", "external",
              "incident", "root_cause")
EDGE_KINDS = ("depends_on", "runs_on", "deployed_from", "owns", "emits",
              "caused_by", "correlates_with")


class KnowledgeGraphService:
    def __init__(self, settings=None):
        self.settings = settings

    # --- upserts (idempotent) ------------------------------------------------
    def upsert_node(self, connection: Connection, project_id: str, kind: str, node_key: str,
                    name: Optional[str] = None, *, attrs: Optional[dict] = None,
                    layer: str = "deterministic", source: str = "manual",
                    confidence: float = 1.0) -> str:
        """Create-or-merge a node keyed by (project, kind, node_key). Returns its id.
        On conflict: refresh name/last_seen, shallow-merge attrs, keep the stronger layer."""
        if kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {kind!r}")
        node_id = make_id("kgn")
        row = connection.execute(
            text(
                """
                INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, layer,
                                      source, confidence, first_seen, last_seen)
                VALUES (:id, :project_id, :kind, :node_key, :name, CAST(:attrs AS JSONB),
                        :layer, :source, :confidence, :now, :now)
                ON CONFLICT (project_id, kind, node_key) DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, kg_nodes.name),
                    attrs = kg_nodes.attrs || EXCLUDED.attrs,
                    -- a deterministic sighting outranks a prior inferred guess
                    layer = CASE WHEN EXCLUDED.layer = 'deterministic' THEN 'deterministic'
                                 ELSE kg_nodes.layer END,
                    confidence = GREATEST(kg_nodes.confidence, EXCLUDED.confidence),
                    source = EXCLUDED.source,
                    last_seen = EXCLUDED.last_seen
                RETURNING id
                """
            ),
            {"id": node_id, "project_id": project_id, "kind": kind, "node_key": node_key,
             "name": name or node_key, "attrs": json.dumps(attrs or {}), "layer": layer,
             "source": source, "confidence": confidence, "now": now_utc()},
        ).scalar_one()
        return row

    def upsert_edge(self, connection: Connection, project_id: str, src_id: str, dst_id: str,
                    kind: str, *, attrs: Optional[dict] = None, layer: str = "deterministic",
                    source: str = "manual", confidence: float = 1.0) -> str:
        """Create-or-merge a directed edge keyed by (project, src, dst, kind). Returns its id."""
        if kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind {kind!r}")
        edge_id = make_id("kge")
        return connection.execute(
            text(
                """
                INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer,
                                      source, confidence, first_seen, last_seen)
                VALUES (:id, :project_id, :src, :dst, :kind, CAST(:attrs AS JSONB),
                        :layer, :source, :confidence, :now, :now)
                ON CONFLICT (project_id, src_id, dst_id, kind) DO UPDATE SET
                    attrs = kg_edges.attrs || EXCLUDED.attrs,
                    confidence = GREATEST(kg_edges.confidence, EXCLUDED.confidence),
                    source = EXCLUDED.source,
                    last_seen = EXCLUDED.last_seen
                RETURNING id
                """
            ),
            {"id": edge_id, "project_id": project_id, "src": src_id, "dst": dst_id,
             "kind": kind, "attrs": json.dumps(attrs or {}), "layer": layer,
             "source": source, "confidence": confidence, "now": now_utc()},
        ).scalar_one()

    # --- reads ---------------------------------------------------------------
    def get_node(self, connection: Connection, project_id: str, kind: str,
                 node_key: str) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text("SELECT * FROM kg_nodes WHERE project_id = :p AND kind = :k AND node_key = :nk"),
            {"p": project_id, "k": kind, "nk": node_key},
        ).mappings().first()
        return dict(row) if row else None

    def neighbors(self, connection: Connection, node_id: str, *, direction: str = "both",
                  edge_kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        """Adjacent nodes with the edge that reached them. direction: out|in|both."""
        kinds = list(edge_kinds) if edge_kinds else None
        clause = "" if not kinds else " AND e.kind = ANY(:kinds)"
        sql = {
            "out": f"SELECT e.kind AS edge_kind, e.confidence AS edge_confidence, 'out' AS dir, n.* "
                   f"FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id "
                   f"WHERE e.src_id = :nid{clause}",
            "in": f"SELECT e.kind AS edge_kind, e.confidence AS edge_confidence, 'in' AS dir, n.* "
                  f"FROM kg_edges e JOIN kg_nodes n ON n.id = e.src_id "
                  f"WHERE e.dst_id = :nid{clause}",
        }
        if direction == "both":
            query = f"{sql['out']} UNION ALL {sql['in']}"
        else:
            query = sql[direction]
        params: dict[str, Any] = {"nid": node_id}
        if kinds:
            params["kinds"] = kinds
        rows = connection.execute(text(query), params).mappings().all()
        return [dict(r) for r in rows]

    def subgraph(self, connection: Connection, project_id: str, root_kind: str, root_key: str,
                 *, depth: int = 2, edge_kinds: Optional[Iterable[str]] = None) -> dict[str, Any]:
        """The RCA context bundle: BFS out from a root node to `depth` hops, returning the
        reached nodes + connecting edges. This is what an investigation reads to understand
        'what does this service touch, and what runs it.'"""
        root = self.get_node(connection, project_id, root_kind, root_key)
        if not root:
            return {"root": None, "nodes": [], "edges": []}
        seen_nodes: dict[str, dict] = {root["id"]: dict(root)}
        seen_edges: dict[tuple, dict] = {}
        frontier = [root["id"]]
        for _ in range(max(0, depth)):
            nxt: list[str] = []
            for nid in frontier:
                for hop in self.neighbors(connection, nid, direction="both", edge_kinds=edge_kinds):
                    other_id = hop["id"]
                    ek = (nid, other_id, hop["edge_kind"], hop["dir"])
                    seen_edges.setdefault(ek, {"from": nid, "to": other_id,
                                               "kind": hop["edge_kind"], "dir": hop["dir"],
                                               "confidence": hop["edge_confidence"]})
                    if other_id not in seen_nodes:
                        node = {k: v for k, v in hop.items()
                                if k not in ("edge_kind", "edge_confidence", "dir")}
                        seen_nodes[other_id] = node
                        nxt.append(other_id)
            frontier = nxt
            if not frontier:
                break
        return {"root": dict(root), "nodes": list(seen_nodes.values()),
                "edges": list(seen_edges.values())}

    # --- learning write-back -------------------------------------------------
    def record_root_cause(self, connection: Connection, project_id: str, service_key: str,
                          cause_kind: str, cause_key: str, cause_name: str, *,
                          confidence: float, evidence: Optional[dict] = None) -> dict[str, str]:
        """Persist a CONFIRMED root cause as a `caused_by` edge from the service to a cause node.
        This is the learning write-back — the graph remembers what actually broke things here."""
        svc = self.upsert_node(connection, project_id, "service", service_key)
        cause = self.upsert_node(connection, project_id, cause_kind, cause_key, cause_name,
                                 layer="inferred", source="investigation", confidence=confidence,
                                 attrs={"evidence": evidence or {}})
        edge = self.upsert_edge(connection, project_id, svc, cause, "caused_by",
                                layer="inferred", source="investigation", confidence=confidence,
                                attrs={"evidence": evidence or {}})
        return {"service_id": svc, "cause_id": cause, "edge_id": edge}

    def known_causes(self, connection: Connection, project_id: str, service_key: str, *,
                     limit: int = 10) -> list[dict[str, Any]]:
        """Prior confirmed causes for a service (ranked by confidence) — replayed into the
        next investigation so recurring failures are recognized fast."""
        rows = connection.execute(
            text(
                """
                SELECT c.kind, c.node_key, c.name, e.confidence, e.attrs, e.last_seen
                FROM kg_nodes s
                JOIN kg_edges e ON e.src_id = s.id AND e.kind = 'caused_by'
                JOIN kg_nodes c ON c.id = e.dst_id
                WHERE s.project_id = :p AND s.kind = 'service' AND s.node_key = :sk
                ORDER BY e.confidence DESC, e.last_seen DESC
                LIMIT :limit
                """
            ),
            {"p": project_id, "sk": service_key, "limit": limit},
        ).mappings().all()
        return [dict(r) for r in rows]

    # --- deterministic seeding ----------------------------------------------
    def seed_from_infra_resources(self, connection: Connection, project_id: str) -> dict[str, int]:
        """Walk the provisioned-inventory table into the deterministic layer: each active
        infra_resource becomes a `resource` node. A cheap, always-true first layer of the map
        (the k8s/cloud walkers land on the same `resource`/`runs_on` shapes later)."""
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
                connection, project_id, "resource", r["external_resource_id"] or r["id"],
                r["resource_name"],
                attrs={"provider": r["provider"], "resource_type": r["resource_type"],
                       "region": r["region"]},
                layer="deterministic", source="infra_resources",
            )
            n += 1
        return {"resources": n}
