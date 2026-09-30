"""RB-15 (remainder) — knowledge-graph walkers: the layers that keep the topology live.

* `walk_kubernetes` — the **deterministic** layer from a cluster: services/pods/nodes via the k8s
  HTTP API (read-only RBAC token via env-var name), normalized into `service` / `resource` nodes and
  `runs_on` edges. Idempotent — repeated walks converge.
* `walk_infra` — the deterministic layer from BOSS's own provisioned inventory (delegates to
  `KnowledgeGraphService.seed_from_infra_resources`).
* `infer_co_occurrence` — the **inferred** layer: services whose incidents repeatedly overlap in time
  gain a `correlates_with` edge (confidence < 1, grows with evidence). Cheap, model-free inference
  that gives the investigation engine cross-service suspects before any tracing exists.

HTTP goes through module-level `_http_json` (tests stub the wire, same pattern as the connectors).
"""
from __future__ import annotations

import json
import os
import urllib.request
from collections import defaultdict
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .knowledge_graph import KnowledgeGraphService

_TIMEOUT = 20


def _http_json(url: str, *, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 — operator-configured URL
        return json.loads(r.read().decode())


def _auth(bearer_env: str) -> dict[str, str]:
    token = os.environ.get(bearer_env, "") if bearer_env else ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def walk_kubernetes(connection: Connection, project_id: str, kg: KnowledgeGraphService,
                    base_url: str, *, bearer_env: str = "",
                    namespace: str = "") -> dict[str, int]:
    """Walk services + pods + nodes into the deterministic layer. An anomaly's service then
    resolves to a named graph node with the hosts it runs on."""
    base = base_url.rstrip("/")
    ns = f"/namespaces/{namespace}" if namespace else ""
    headers = _auth(bearer_env)
    services = _http_json(f"{base}/api/v1{ns}/services", headers=headers).get("items", [])
    pods = _http_json(f"{base}/api/v1{ns}/pods", headers=headers).get("items", [])
    nodes = _http_json(f"{base}/api/v1/nodes", headers=headers).get("items", [])

    counts = {"services": 0, "pods": 0, "nodes": 0, "edges": 0}
    node_ids: dict[str, str] = {}
    for n in nodes:
        name = (n.get("metadata") or {}).get("name", "")
        if not name:
            continue
        node_ids[name] = kg.upsert_node(connection, project_id, "resource", name,
                                        attrs={"provider": "kubernetes", "resource_type": "node"},
                                        layer="deterministic", source="k8s")
        counts["nodes"] += 1

    svc_ids: dict[str, str] = {}          # selector-app -> service node id
    for s in services:
        meta = s.get("metadata") or {}
        name = meta.get("name", "")
        if not name:
            continue
        sid = kg.upsert_node(connection, project_id, "service", name,
                             attrs={"namespace": meta.get("namespace", "")},
                             layer="deterministic", source="k8s")
        counts["services"] += 1
        app = ((s.get("spec") or {}).get("selector") or {}).get("app", name)
        svc_ids[app] = sid

    for p in pods:
        meta = p.get("metadata") or {}
        app = (meta.get("labels") or {}).get("app", "")
        host = (p.get("spec") or {}).get("nodeName", "")
        counts["pods"] += 1
        if app in svc_ids and host in node_ids:   # service --runs_on--> node
            kg.upsert_edge(connection, project_id, svc_ids[app], node_ids[host], "runs_on",
                           layer="deterministic", source="k8s")
            counts["edges"] += 1
    return counts


def walk_infra(connection: Connection, project_id: str, kg: KnowledgeGraphService) -> dict[str, int]:
    return kg.seed_from_infra_resources(connection, project_id)


def infer_co_occurrence(connection: Connection, project_id: str, kg: KnowledgeGraphService, *,
                        window_minutes: int = 30, min_overlap: int = 2) -> dict[str, int]:
    """Services whose incidents overlap in time (within `window_minutes`) repeatedly are likely
    coupled: add an inferred `correlates_with` edge whose confidence grows with the evidence."""
    rows = connection.execute(
        text("SELECT service, first_seen, last_seen FROM incidents "
             "WHERE project_id = :p AND service != '' ORDER BY first_seen"),
        {"p": project_id},
    ).mappings().all()
    overlap: dict[tuple[str, str], int] = defaultdict(int)
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["service"] == b["service"]:
                continue
            gap = (b["first_seen"] - a["last_seen"]).total_seconds() / 60.0
            if gap <= window_minutes:             # b started within the window after a
                pair = tuple(sorted((a["service"], b["service"])))
                overlap[pair] += 1
    edges = 0
    for (svc_a, svc_b), n in overlap.items():
        if n < min_overlap:
            continue
        ida = kg.upsert_node(connection, project_id, "service", svc_a, source="cooccurrence")
        idb = kg.upsert_node(connection, project_id, "service", svc_b, source="cooccurrence")
        confidence = min(0.8, 0.3 + 0.1 * n)
        kg.upsert_edge(connection, project_id, ida, idb, "correlates_with",
                       layer="inferred", source="cooccurrence", confidence=confidence,
                       attrs={"overlapping_incidents": n})
        edges += 1
    return {"pairs_considered": len(overlap), "edges": edges}


WALKERS = {"kubernetes": walk_kubernetes, "infra": walk_infra, "cooccurrence": infer_co_occurrence}
