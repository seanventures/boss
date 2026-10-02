"""Observability suite — unified operator view of connectors, OSS stack, and KG convenience.

BOSS already has:

* **Read connectors** (`telemetry_connectors`) for Prometheus, Loki, Grafana, Datadog, …
* **Deployable stack** (`observability_stack`) — OTel Collector → Prometheus → Grafana
* **Knowledge graph** (`knowledge_graph`) for topology + RCA learning

What was missing is one operator surface that answers:

1. What OSS components should we run, and why?
2. What is connected / deployed *for this project* right now?
3. Given a workload profile, what should we add next?
4. Can we reach the configured sources (dry-run, no silent fake data)?
5. Graph health + an RCA pack (subgraph + known causes) without stitching five calls.

No live GitHub/star scrapes and no live backend calls unless the operator asks for
``health-check`` (and even then the wire helper is stubbable for tests).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .core import now_utc
from .knowledge_graph import KnowledgeGraphService
from .reliability import ReliabilityService
from .telemetry_connectors import CONNECTOR_CATALOG, list_connector_catalog

# Market OSS components operators actually deploy alongside BOSS. Stars are static
# order-of-magnitude notes (not scraped) so the catalog stays offline-safe and stable.
OSS_STACK_CATALOG: list[dict[str, Any]] = [
    {
        "id": "prometheus",
        "name": "Prometheus",
        "purpose": "Metrics TSDB + PromQL evaluation; scrape or remote-write store.",
        "role": "metrics",
        "boss_integration": "native connector + BOSS-managed stack (boss-prometheus)",
        "deployed_by_boss_stack": True,
        "stars_note": "~55k GitHub stars (CNCF graduated) — de-facto metrics standard",
        "default_ports": [9090, 9091],
        "health_paths": ["/-/healthy", "/api/v1/status/buildinfo"],
        "homepage": "https://prometheus.io",
    },
    {
        "id": "grafana",
        "name": "Grafana",
        "purpose": "Dashboards and explore UI over metrics, logs, and traces.",
        "role": "visualization",
        "boss_integration": "native connector (datasource proxy) + BOSS-managed stack",
        "deployed_by_boss_stack": True,
        "stars_note": "~65k GitHub stars — default operator UI for the OSS stack",
        "default_ports": [3000, 3006],
        "health_paths": ["/api/health"],
        "homepage": "https://grafana.com/oss/grafana/",
    },
    {
        "id": "loki",
        "name": "Loki",
        "purpose": "Log aggregation with LogQL; label-indexed, Prometheus-aligned.",
        "role": "logs",
        "boss_integration": "native connector; optional next to BOSS metrics stack",
        "deployed_by_boss_stack": False,
        "stars_note": "~25k GitHub stars (Grafana Labs) — log half of the LGTM stack",
        "default_ports": [3100],
        "health_paths": ["/ready", "/loki/api/v1/status/buildinfo"],
        "homepage": "https://grafana.com/oss/loki/",
    },
    {
        "id": "tempo",
        "name": "Grafana Tempo",
        "purpose": "Distributed tracing backend (OTLP); pairs with Grafana TraceQL.",
        "role": "traces",
        "boss_integration": "recommended for microservices; wire via OTel Collector",
        "deployed_by_boss_stack": False,
        "stars_note": "~4k GitHub stars — lightest OSS trace store in the LGTM set",
        "default_ports": [3200, 4317, 4318],
        "health_paths": ["/ready", "/status"],
        "homepage": "https://grafana.com/oss/tempo/",
    },
    {
        "id": "jaeger",
        "name": "Jaeger",
        "purpose": "Distributed tracing UI + collector (CNCF); alternative to Tempo.",
        "role": "traces",
        "boss_integration": "recommended when teams already standardize on Jaeger",
        "deployed_by_boss_stack": False,
        "stars_note": "~20k GitHub stars (CNCF graduated) — mature tracing UI",
        "default_ports": [16686, 4317, 14268],
        "health_paths": ["/"],
        "homepage": "https://www.jaegertracing.io",
    },
    {
        "id": "otel-collector",
        "name": "OpenTelemetry Collector",
        "purpose": "Vendor-neutral receive/process/export for metrics, logs, traces.",
        "role": "collector",
        "boss_integration": "BOSS-managed stack deploys otel-collector-contrib as scrape hub",
        "deployed_by_boss_stack": True,
        "stars_note": "~5k GitHub stars (contrib) — standard pipeline between apps and stores",
        "default_ports": [4317, 4318, 8889],
        "health_paths": ["/"],
        "homepage": "https://opentelemetry.io/docs/collector/",
    },
    {
        "id": "alertmanager",
        "name": "Alertmanager",
        "purpose": "Alert grouping, inhibition, and routing for Prometheus rules.",
        "role": "alerting",
        "boss_integration": "recommended with Prometheus; BOSS incidents remain the SRE loop",
        "deployed_by_boss_stack": False,
        "stars_note": "shipped with Prometheus; core of OSS on-call notification routing",
        "default_ports": [9093],
        "health_paths": ["/-/healthy"],
        "homepage": "https://prometheus.io/docs/alerting/latest/alertmanager/",
    },
]

# workload_profile → ordered component ids (from OSS_STACK_CATALOG)
_PROFILE_RECOMMENDATIONS: dict[str, dict[str, Any]] = {
    "minimal": {
        "title": "Minimal metrics",
        "description": "Prometheus + Grafana only — enough to prove scrapes and one dashboard.",
        "components": ["prometheus", "grafana", "otel-collector"],
    },
    "web": {
        "title": "Web / API service",
        "description": "Metrics + logs + alerts for a request-serving app.",
        "components": ["otel-collector", "prometheus", "grafana", "loki", "alertmanager"],
    },
    "api": {
        "title": "API service",
        "description": "Same as web — RED metrics, structured logs, alert routing.",
        "components": ["otel-collector", "prometheus", "grafana", "loki", "alertmanager"],
    },
    "batch": {
        "title": "Batch / jobs",
        "description": "Metrics + logs; traces optional unless jobs are multi-step.",
        "components": ["otel-collector", "prometheus", "grafana", "loki"],
    },
    "microservices": {
        "title": "Microservices",
        "description": "Full LGTM-style stack: metrics, logs, traces, alerts.",
        "components": [
            "otel-collector",
            "prometheus",
            "grafana",
            "loki",
            "tempo",
            "alertmanager",
        ],
    },
    "k8s": {
        "title": "Kubernetes cluster",
        "description": "Full stack plus Jaeger as an alternate trace UI teams often know.",
        "components": [
            "otel-collector",
            "prometheus",
            "grafana",
            "loki",
            "tempo",
            "jaeger",
            "alertmanager",
        ],
    },
    "enterprise": {
        "title": "Enterprise default",
        "description": "Everything BOSS recommends for production multi-service fleets.",
        "components": [
            "otel-collector",
            "prometheus",
            "grafana",
            "loki",
            "tempo",
            "alertmanager",
        ],
    },
    "traces_focus": {
        "title": "Tracing-first",
        "description": "Collector + Tempo (or Jaeger) + Grafana for span-heavy debugging.",
        "components": ["otel-collector", "tempo", "jaeger", "grafana", "prometheus"],
    },
}

_CATALOG_BY_ID = {c["id"]: c for c in OSS_STACK_CATALOG}


def list_oss_catalog() -> list[dict[str, Any]]:
    """Static OSS component catalog (purpose + stars note). Offline-safe."""
    return [dict(item) for item in OSS_STACK_CATALOG]


def list_profiles() -> list[dict[str, Any]]:
    """Workload profiles the recommender understands."""
    return [
        {
            "id": key,
            "title": meta["title"],
            "description": meta["description"],
            "component_ids": list(meta["components"]),
        }
        for key, meta in sorted(_PROFILE_RECOMMENDATIONS.items())
    ]


def recommend_stack(workload_profile: str) -> dict[str, Any]:
    """Map a workload profile to ordered OSS components + BOSS wiring notes."""
    key = (workload_profile or "").strip().lower().replace("-", "_").replace(" ", "_")
    # common aliases
    aliases = {
        "default": "web",
        "production": "enterprise",
        "prod": "enterprise",
        "kubernetes": "k8s",
        "kube": "k8s",
        "service": "api",
        "services": "microservices",
        "microservice": "microservices",
        "trace": "traces_focus",
        "tracing": "traces_focus",
        "jobs": "batch",
        "worker": "batch",
    }
    key = aliases.get(key, key)
    meta = _PROFILE_RECOMMENDATIONS.get(key)
    if not meta:
        known = sorted(_PROFILE_RECOMMENDATIONS)
        raise ValueError(
            f"unknown workload_profile {workload_profile!r}; known: {', '.join(known)}"
        )
    components = []
    for cid in meta["components"]:
        base = _CATALOG_BY_ID.get(cid)
        if base:
            components.append(dict(base))
    already_in_boss_stack = [c["id"] for c in components if c.get("deployed_by_boss_stack")]
    external_or_optional = [
        c["id"] for c in components if not c.get("deployed_by_boss_stack")
    ]
    return {
        "workload_profile": key,
        "title": meta["title"],
        "description": meta["description"],
        "components": components,
        "component_ids": [c["id"] for c in components],
        "boss_stack_covers": already_in_boss_stack,
        "add_alongside_boss_stack": external_or_optional,
        "next_steps": [
            # Operator words: these render on the Observability screen, where an endpoint is
            # not something anyone can press (console walk, 2026-10-02).
            "Deploy the BOSS monitoring stack from Infra — Deploy monitoring stack, beside the "
            "deployment targets (covers otel-collector, prometheus, grafana).",
            "Connect the remaining sources as telemetry connectors once the stack is up.",
            "Press Health-check on the Observability screen after the endpoints are configured.",
        ],
    }


def _serialize_source(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe source summary (no secrets; config may hold base_url + env-var names)."""
    cfg = row.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    created = row.get("created_at")
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "name": row.get("name"),
        "enabled": bool(row.get("enabled", True)),
        "config": cfg,
        "base_url": (cfg or {}).get("base_url") if isinstance(cfg, dict) else None,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


def suite_status(
    connection: Connection,
    project_id: str,
    *,
    host_target_id: Optional[str] = None,
    engine: Optional[Engine] = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Unified status: registered connectors + stack deploy signal + catalog coverage."""
    reliability = ReliabilityService(settings)
    sources = reliability.list_sources(connection, project_id, enabled_only=False)
    source_summaries = [_serialize_source(s) for s in sources]
    kinds_present = sorted({s["kind"] for s in source_summaries if s.get("kind")})

    # BOSS stack signal: telemetry_sources row named boss-prometheus (same as /v1/observability/stack)
    boss_prom = next(
        (
            s
            for s in source_summaries
            if s.get("kind") == "prometheus" and s.get("name") == "boss-prometheus"
        ),
        None,
    )
    stack_deployed = boss_prom is not None

    apps: list[dict[str, Any]] = []
    try:
        from .observability_stack import scrape_targets

        apps = scrape_targets(connection, project_id)
    except Exception:  # noqa: BLE001 — status must not fail if deploy tables partial
        apps = []

    host_status: dict[str, Any] = {"running": None, "probed": False}
    if host_target_id and engine is not None and settings is not None:
        try:
            from .observability_stack import stack_status as probe_stack

            host_status = {
                **probe_stack(engine, settings, host_target_id=host_target_id),
                "probed": True,
            }
        except Exception as exc:  # noqa: BLE001
            host_status = {
                "running": False,
                "probed": True,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }

    oss = list_oss_catalog()
    boss_stack_ids = {c["id"] for c in oss if c.get("deployed_by_boss_stack")}
    connector_kinds = {c["kind"] for c in list_connector_catalog()}

    return {
        "project_id": project_id,
        "generated_at": now_utc().isoformat(),
        "connectors": {
            "registered": source_summaries,
            "count": len(source_summaries),
            "enabled_count": sum(1 for s in source_summaries if s.get("enabled")),
            "kinds_present": kinds_present,
            "catalog_kinds": sorted(connector_kinds),
        },
        "stack": {
            "deployed": stack_deployed,
            "boss_prometheus": boss_prom,
            "scrape_app_count": len(apps),
            "apps": [
                {
                    "app": a.get("app"),
                    "environment": a.get("environment"),
                    "port": a.get("port"),
                    "target_id": a.get("target_id"),
                }
                for a in apps
            ],
            "host_status": host_status,
            "components_covered_when_deployed": sorted(boss_stack_ids),
        },
        "recommended_oss": {
            "items": oss,
            "count": len(oss),
            "profiles": [p["id"] for p in list_profiles()],
        },
        "gaps": _compute_gaps(kinds_present, stack_deployed),
    }


def _compute_gaps(kinds_present: list[str], stack_deployed: bool) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    if not stack_deployed and "prometheus" not in kinds_present:
        gaps.append(
            {
                "id": "no_metrics_backend",
                "severity": "warning",
                "message": "No Prometheus source registered and BOSS stack not deployed.",
            }
        )
    if "loki" not in kinds_present and "grafana" not in kinds_present:
        gaps.append(
            {
                "id": "no_logs_backend",
                "severity": "info",
                "message": "No Loki/Grafana log connector registered — log RCA will be limited.",
            }
        )
    if not kinds_present and not stack_deployed:
        gaps.append(
            {
                "id": "empty_observability",
                "severity": "warning",
                "message": "No telemetry sources and no stack — start with Deploy monitoring stack "
                "in Infra, or connect a telemetry source.",
            }
        )
    return gaps


def _probe_url(base_url: str, path: str) -> dict[str, Any]:
    """Cheap GET via the shared wire helper (tests monkeypatch telemetry_connectors._http_json)."""
    from . import telemetry_connectors as tc

    url = base_url.rstrip("/") + path
    try:
        data = tc._http_json(url)
        return {"ok": True, "url": url, "sample_keys": list(data.keys())[:8] if isinstance(data, dict) else []}
    except Exception as exc:  # noqa: BLE001 — connectivity probe must report, not raise
        return {
            "ok": False,
            "url": url,
            "error": f"{type(exc).__name__}: {exc}"[:240],
        }


def health_check_sources(
    connection: Connection,
    project_id: str,
    *,
    source_ids: Optional[list[str]] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Dry-run connectivity for configured telemetry sources.

    * ``dry_run=True`` (default): only validates config shape / readiness — no wire calls.
    * ``dry_run=False``: probes a health path per kind when ``base_url`` is present.

    Never returns fake healthy data on failure; each source is explicit ok/fail.
    """
    reliability = ReliabilityService()
    sources = reliability.list_sources(connection, project_id, enabled_only=False)
    if source_ids:
        want = set(source_ids)
        sources = [s for s in sources if s.get("id") in want]

    results: list[dict[str, Any]] = []
    for raw in sources:
        summary = _serialize_source(raw)
        kind = summary.get("kind") or ""
        cfg = summary.get("config") or {}
        base_url = str(cfg.get("base_url") or "").strip()
        entry: dict[str, Any] = {
            "id": summary.get("id"),
            "name": summary.get("name"),
            "kind": kind,
            "enabled": summary.get("enabled"),
            "base_url": base_url or None,
            "dry_run": dry_run,
        }

        # Config readiness (same fail-closed spirit as ensure_connector_ready)
        try:
            from .telemetry_connectors import ensure_connector_ready

            ensure_connector_ready({"kind": kind, **cfg})
            entry["config_ready"] = True
        except Exception as exc:  # noqa: BLE001
            entry["config_ready"] = False
            entry["config_error"] = str(exc)[:240]
            entry["reachable"] = False
            entry["status"] = "misconfigured"
            results.append(entry)
            continue

        if dry_run:
            entry["reachable"] = None
            entry["status"] = "config_ok"
            entry["note"] = "dry-run only — no live probe performed"
            results.append(entry)
            continue

        if not base_url:
            entry["reachable"] = False
            entry["status"] = "no_base_url"
            entry["note"] = "live probe skipped — connector has no base_url"
            results.append(entry)
            continue

        paths = {
            "prometheus": ["/-/healthy", "/api/v1/status/buildinfo"],
            "loki": ["/ready"],
            "grafana": ["/api/health"],
            "splunk": ["/services/server/info"],
            "datadog": None,  # cloud API — skip generic GET
            "pagerduty": None,
            "cloudwatch": None,
            "memory": None,
            "log_export": None,
        }.get(kind, ["/"])

        if paths is None:
            entry["reachable"] = None
            entry["status"] = "probe_not_applicable"
            entry["note"] = f"kind {kind!r} has no HTTP health path probe"
            results.append(entry)
            continue

        probe_results = []
        any_ok = False
        for path in paths:
            # Some health endpoints return non-JSON; treat any non-exception as soft ok
            # by trying JSON first, then falling back to raw open via _http_json only.
            pr = _probe_url(base_url, path)
            probe_results.append(pr)
            if pr.get("ok"):
                any_ok = True
                break
        entry["probes"] = probe_results
        entry["reachable"] = any_ok
        entry["status"] = "reachable" if any_ok else "unreachable"
        results.append(entry)

    ok_count = sum(1 for r in results if r.get("status") in ("config_ok", "reachable", "probe_not_applicable"))
    return {
        "project_id": project_id,
        "dry_run": dry_run,
        "checked_at": now_utc().isoformat(),
        "count": len(results),
        "ok_count": ok_count,
        "items": results,
    }


def graph_health_summary(connection: Connection, project_id: str) -> dict[str, Any]:
    """Node/edge counts + kind breakdown for the knowledge graph."""
    node_rows = connection.execute(
        text(
            """
            SELECT kind, layer, COUNT(*)::int AS n
            FROM kg_nodes
            WHERE project_id = :p
            GROUP BY kind, layer
            ORDER BY kind, layer
            """
        ),
        {"p": project_id},
    ).mappings().all()
    edge_rows = connection.execute(
        text(
            """
            SELECT kind, COUNT(*)::int AS n
            FROM kg_edges
            WHERE project_id = :p
            GROUP BY kind
            ORDER BY kind
            """
        ),
        {"p": project_id},
    ).mappings().all()
    totals = connection.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*)::int FROM kg_nodes WHERE project_id = :p) AS nodes,
              (SELECT COUNT(*)::int FROM kg_edges WHERE project_id = :p) AS edges,
              (SELECT MAX(last_seen) FROM kg_nodes WHERE project_id = :p) AS last_node_seen,
              (SELECT MAX(last_seen) FROM kg_edges WHERE project_id = :p) AS last_edge_seen
            """
        ),
        {"p": project_id},
    ).mappings().one()

    by_kind: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    for r in node_rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + int(r["n"])
        by_layer[r["layer"]] = by_layer.get(r["layer"], 0) + int(r["n"])

    node_count = int(totals["nodes"] or 0)
    edge_count = int(totals["edges"] or 0)
    healthy = node_count > 0
    return {
        "project_id": project_id,
        "healthy": healthy,
        "status": "ready" if healthy else "empty",
        "node_count": node_count,
        "edge_count": edge_count,
        "nodes_by_kind": by_kind,
        "nodes_by_layer": by_layer,
        "edges_by_kind": {r["kind"]: int(r["n"]) for r in edge_rows},
        "last_node_seen": (
            totals["last_node_seen"].isoformat()
            if totals["last_node_seen"] is not None
            and hasattr(totals["last_node_seen"], "isoformat")
            else totals["last_node_seen"]
        ),
        "last_edge_seen": (
            totals["last_edge_seen"].isoformat()
            if totals["last_edge_seen"] is not None
            and hasattr(totals["last_edge_seen"], "isoformat")
            else totals["last_edge_seen"]
        ),
        "hints": (
            []
            if healthy
            else [
                "Graph is empty — it fills from deployed infrastructure once an app is onboarded "
                "in Infra.",
            ]
        ),
    }


def rca_pack(
    connection: Connection,
    project_id: str,
    *,
    kind: str = "service",
    node_key: str,
    depth: int = 2,
    known_causes_limit: int = 10,
) -> dict[str, Any]:
    """Combine subgraph + known causes into one RCA investigation pack."""
    kg = KnowledgeGraphService()
    sub = kg.subgraph(connection, project_id, kind, node_key, depth=depth)
    causes: list[dict[str, Any]] = []
    if kind == "service":
        causes = kg.known_causes(
            connection, project_id, node_key, limit=known_causes_limit
        )
    health = graph_health_summary(connection, project_id)
    root = sub.get("root")
    return {
        "project_id": project_id,
        "query": {"kind": kind, "node_key": node_key, "depth": depth},
        "found": root is not None,
        "root": root,
        "subgraph": {
            "nodes": sub.get("nodes") or [],
            "edges": sub.get("edges") or [],
            "node_count": len(sub.get("nodes") or []),
            "edge_count": len(sub.get("edges") or []),
        },
        "known_causes": causes,
        "known_causes_count": len(causes),
        "graph_health": {
            "status": health["status"],
            "node_count": health["node_count"],
            "edge_count": health["edge_count"],
        },
    }


def seed_and_summarize(
    connection: Connection,
    project_id: str,
    *,
    actor: str = "operator",
) -> dict[str, Any]:
    """Seed resource nodes from infra_resources, then return graph health."""
    kg = KnowledgeGraphService()
    counts = kg.seed_from_infra_resources(connection, project_id)
    health = graph_health_summary(connection, project_id)
    return {
        "project_id": project_id,
        "actor": actor,
        "seeded": counts,
        "health": health,
    }


class ObservabilitySuiteService:
    """Thin OO façade for the suite helpers (mirrors ReliabilityService style)."""

    def __init__(self, settings: Any = None):
        self.settings = settings

    def catalog(self) -> dict[str, Any]:
        items = list_oss_catalog()
        return {
            "items": items,
            "count": len(items),
            "profiles": list_profiles(),
            "connector_catalog": list_connector_catalog(),
        }

    def status(
        self,
        connection: Connection,
        project_id: str,
        *,
        host_target_id: Optional[str] = None,
        engine: Optional[Engine] = None,
    ) -> dict[str, Any]:
        return suite_status(
            connection,
            project_id,
            host_target_id=host_target_id,
            engine=engine,
            settings=self.settings,
        )

    def recommend(self, workload_profile: str) -> dict[str, Any]:
        return recommend_stack(workload_profile)

    def health_check(
        self,
        connection: Connection,
        project_id: str,
        *,
        source_ids: Optional[list[str]] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return health_check_sources(
            connection, project_id, source_ids=source_ids, dry_run=dry_run
        )

    def graph_summary(
        self,
        connection: Connection,
        project_id: str,
        *,
        kind: Optional[str] = None,
        node_key: Optional[str] = None,
        depth: int = 2,
    ) -> dict[str, Any]:
        health = graph_health_summary(connection, project_id)
        out: dict[str, Any] = {"project_id": project_id, "health": health}
        if kind and node_key:
            out["rca"] = rca_pack(
                connection, project_id, kind=kind, node_key=node_key, depth=depth
            )
        return out
