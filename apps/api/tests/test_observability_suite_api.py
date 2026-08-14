"""HTTP surface for observability suite + KG convenience.

Covers:
  GET  /v1/observability-suite/catalog
  GET  /v1/observability-suite/status?project_id=
  POST /v1/observability-suite/recommend
  POST /v1/observability-suite/health-check
  GET  /v1/observability-suite/graph-summary
  POST /v1/observability-suite/graph-seed
  GET  /v1/knowledge/graph/health

No live external HTTP — wire helpers are stubbed; dry-run is the default.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services import telemetry_connectors as tc
from apps.api.app.services.core import make_id, now_utc
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def test_catalog_lists_oss_components_and_profiles(client):
    r = client.get("/v1/observability-suite/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 6
    ids = {item["id"] for item in body["items"]}
    assert {
        "prometheus",
        "grafana",
        "loki",
        "tempo",
        "jaeger",
        "otel-collector",
        "alertmanager",
    } <= ids
    prom = next(i for i in body["items"] if i["id"] == "prometheus")
    assert "purpose" in prom and "stars_note" in prom
    assert prom["deployed_by_boss_stack"] is True
    profile_ids = {p["id"] for p in body["profiles"]}
    assert "web" in profile_ids and "microservices" in profile_ids
    connector_kinds = {c["kind"] for c in body["connector_catalog"]}
    assert "prometheus" in connector_kinds


def test_recommend_web_profile(client):
    r = client.post(
        "/v1/observability-suite/recommend",
        json={"workload_profile": "web"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workload_profile"] == "web"
    assert "prometheus" in body["component_ids"]
    assert "loki" in body["component_ids"]
    assert "otel-collector" in body["boss_stack_covers"]
    assert "loki" in body["add_alongside_boss_stack"]
    assert body["next_steps"]


def test_recommend_alias_and_unknown(client):
    r = client.post(
        "/v1/observability-suite/recommend",
        json={"workload_profile": "kubernetes"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["workload_profile"] == "k8s"

    bad = client.post(
        "/v1/observability-suite/recommend",
        json={"workload_profile": "not-a-real-profile"},
    )
    assert bad.status_code == 422


def test_status_empty_project_reports_gaps(client):
    r = client.get(
        "/v1/observability-suite/status",
        params={"project_id": PID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PID
    assert body["connectors"]["count"] == 0
    assert body["stack"]["deployed"] is False
    assert body["recommended_oss"]["count"] >= 6
    gap_ids = {g["id"] for g in body["gaps"]}
    assert "empty_observability" in gap_ids or "no_metrics_backend" in gap_ids


def test_status_with_registered_sources(client, engine):
    with begin(engine) as c:
        RS.register_source(
            c,
            PID,
            "prometheus",
            "boss-prometheus",
            config={"base_url": "http://prom.local:9090"},
        )
        RS.register_source(
            c,
            PID,
            "loki",
            "team-loki",
            config={"base_url": "http://loki.local:3100"},
        )

    r = client.get(
        "/v1/observability-suite/status",
        params={"project_id": PID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connectors"]["count"] == 2
    assert body["stack"]["deployed"] is True
    assert "prometheus" in body["connectors"]["kinds_present"]
    assert "loki" in body["connectors"]["kinds_present"]
    assert body["stack"]["boss_prometheus"]["name"] == "boss-prometheus"


def test_health_check_dry_run_default(client, engine):
    with begin(engine) as c:
        RS.register_source(
            c,
            PID,
            "prometheus",
            "prom-a",
            config={"base_url": "http://prom.local:9090"},
        )
        RS.register_source(
            c,
            PID,
            "datadog",
            "dd-missing-keys",
            config={},
        )

    r = client.post(
        "/v1/observability-suite/health-check",
        json={"project_id": PID, "actor": ACTOR},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["count"] == 2
    by_name = {i["name"]: i for i in body["items"]}
    assert by_name["prom-a"]["status"] == "config_ok"
    assert by_name["prom-a"]["reachable"] is None
    assert by_name["dd-missing-keys"]["status"] == "misconfigured"
    assert by_name["dd-missing-keys"]["config_ready"] is False


def test_health_check_live_probe_stubbed(client, engine, monkeypatch):
    with begin(engine) as c:
        RS.register_source(
            c,
            PID,
            "prometheus",
            "prom-live",
            config={"base_url": "http://prom.local:9090"},
        )

    def fake_http(url, *, headers=None):
        assert "prom.local" in url
        if "/-/healthy" in url:
            return {"status": "ok"}
        return {}

    monkeypatch.setattr(tc, "_http_json", fake_http)

    r = client.post(
        "/v1/observability-suite/health-check",
        json={"project_id": PID, "actor": ACTOR, "dry_run": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    item = body["items"][0]
    assert item["status"] == "reachable"
    assert item["reachable"] is True
    assert item["probes"]


def test_health_check_live_unreachable_stubbed(client, engine, monkeypatch):
    with begin(engine) as c:
        RS.register_source(
            c,
            PID,
            "prometheus",
            "prom-down",
            config={"base_url": "http://prom.down:9090"},
        )

    def boom(url, *, headers=None):
        raise OSError("connection refused")

    monkeypatch.setattr(tc, "_http_json", boom)

    r = client.post(
        "/v1/observability-suite/health-check",
        json={"project_id": PID, "actor": ACTOR, "dry_run": False},
    )
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["status"] == "unreachable"
    assert item["reachable"] is False


def test_knowledge_graph_health_empty(client):
    r = client.get(
        "/v1/knowledge/graph/health",
        params={"project_id": PID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PID
    assert body["status"] == "empty"
    assert body["healthy"] is False
    assert body["node_count"] == 0
    assert body["hints"]


def test_graph_summary_and_rca_pack(client, engine):
    # Build a tiny service → datastore graph + a known cause
    svc = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "kind": "service",
            "node_key": "checkout",
            "name": "Checkout",
        },
    )
    assert svc.status_code == 200, svc.text
    svc_id = svc.json()["id"]

    db = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "kind": "datastore",
            "node_key": "pg-checkout",
            "name": "Checkout PG",
        },
    )
    assert db.status_code == 200, db.text
    db_id = db.json()["id"]

    edge = client.post(
        "/v1/knowledge/graph/edges",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "src_id": svc_id,
            "dst_id": db_id,
            "kind": "depends_on",
        },
    )
    assert edge.status_code == 200, edge.text

    rc = client.post(
        "/v1/knowledge/graph/root-causes",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "service": "checkout",
            "cause_kind": "root_cause",
            "cause_key": "pool-exhaustion",
            "cause_name": "connection pool exhaustion",
            "confidence": 0.85,
        },
    )
    assert rc.status_code == 200, rc.text

    health = client.get(
        "/v1/knowledge/graph/health",
        params={"project_id": PID},
    )
    assert health.status_code == 200, health.text
    h = health.json()
    assert h["healthy"] is True
    assert h["node_count"] >= 2
    assert h["nodes_by_kind"].get("service", 0) >= 1

    summary = client.get(
        "/v1/observability-suite/graph-summary",
        params={
            "project_id": PID,
            "kind": "service",
            "node_key": "checkout",
            "depth": 1,
        },
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["health"]["healthy"] is True
    rca = body["rca"]
    assert rca["found"] is True
    assert rca["root"]["node_key"] == "checkout"
    keys = {n["node_key"] for n in rca["subgraph"]["nodes"]}
    assert "checkout" in keys and "pg-checkout" in keys
    assert rca["known_causes_count"] >= 1
    assert rca["known_causes"][0]["node_key"] == "pool-exhaustion"


def test_graph_seed_from_infra(client, engine):
    rid = make_id("infra")
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO infra_resources
                  (id, project_id, provider, resource_type, resource_name,
                   external_resource_id, region, size, image, ttl_minutes,
                   expires_at, status)
                VALUES (:id, :p, 'digitalocean', 'droplet', 'web-obs',
                        'do-obs-seed', 'nyc3', 's-1vcpu', 'ubuntu-22', 60,
                        :exp, 'active')
                """
            ),
            {"id": rid, "p": PID, "exp": now_utc()},
        )

    r = client.post(
        "/v1/observability-suite/graph-seed",
        json={"project_id": PID, "actor": ACTOR},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seeded"]["resources"] >= 1
    assert body["health"]["node_count"] >= 1
    assert body["health"]["healthy"] is True
