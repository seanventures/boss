"""HTTP E2E for KnowledgeGraphService — /v1/knowledge/graph/* (RB-15).

Full path: upsert nodes/edges → subgraph → neighbors → root-cause write-back →
known-causes replay → seed-from-infra. Mutations require providerops_manage.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.core import make_id, now_utc

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def test_knowledge_graph_e2e_upsert_subgraph_known_causes(client, engine):
    # 1) Upsert service + datastore + edge
    svc = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "kind": "service",
            "node_key": "api-checkout",
            "name": "Checkout API",
            "attrs": {"team": "payments"},
        },
    )
    assert svc.status_code == 200, svc.text
    svc_body = svc.json()
    assert svc_body["kind"] == "service"
    assert svc_body["node_key"] == "api-checkout"
    svc_id = svc_body["id"]

    db = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "kind": "datastore",
            "node_key": "pg-checkout",
            "name": "Checkout Postgres",
        },
    )
    assert db.status_code == 200, db.text
    db_id = db.json()["id"]

    edge = client.post(
        "/v1/knowledge/graph/edges",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "src_id": svc_id,
            "dst_id": db_id,
            "kind": "depends_on",
        },
    )
    assert edge.status_code == 200, edge.text
    assert edge.json()["kind"] == "depends_on"
    assert edge.json()["src_id"] == svc_id

    # Idempotent re-upsert merges attrs and returns same id
    again = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "kind": "service",
            "node_key": "api-checkout",
            "name": "Checkout",
            "attrs": {"lang": "go"},
        },
    )
    assert again.status_code == 200
    assert again.json()["id"] == svc_id

    # 2) GET node by natural key
    listed = client.get(
        "/v1/knowledge/graph/nodes",
        params={
            "project_id": PROJECT_ID,
            "kind": "service",
            "node_key": "api-checkout",
        },
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == svc_id
    assert items[0]["attrs"].get("team") == "payments"
    assert items[0]["attrs"].get("lang") == "go"

    # 3) Subgraph depth 1 includes the datastore
    sub = client.get(
        "/v1/knowledge/graph/subgraph",
        params={
            "project_id": PROJECT_ID,
            "kind": "service",
            "node_key": "api-checkout",
            "depth": 1,
        },
    )
    assert sub.status_code == 200, sub.text
    sub_body = sub.json()
    assert sub_body["root"]["node_key"] == "api-checkout"
    keys = {n["node_key"] for n in sub_body["nodes"]}
    assert keys == {"api-checkout", "pg-checkout"}
    assert any(e["kind"] == "depends_on" for e in sub_body["edges"])

    # 4) Neighbors by node_id and by kind+key
    by_id = client.get(
        "/v1/knowledge/graph/neighbors",
        params={"project_id": PROJECT_ID, "node_id": svc_id},
    )
    assert by_id.status_code == 200, by_id.text
    assert any(n["node_key"] == "pg-checkout" for n in by_id.json()["items"])

    by_key = client.get(
        "/v1/knowledge/graph/neighbors",
        params={
            "project_id": PROJECT_ID,
            "kind": "service",
            "node_key": "api-checkout",
        },
    )
    assert by_key.status_code == 200
    assert by_key.json()["node_id"] == svc_id

    # 5) Record root causes + known-causes ranked by confidence
    rc1 = client.post(
        "/v1/knowledge/graph/root-causes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "service": "api-checkout",
            "cause_kind": "deployment",
            "cause_key": "deploy-abc123",
            "cause_name": "deploy abc123",
            "confidence": 0.9,
            "evidence": {"onset": "14:32", "diff": "db.py"},
        },
    )
    assert rc1.status_code == 200, rc1.text
    assert rc1.json()["service_id"]
    assert rc1.json()["cause_id"]
    assert rc1.json()["edge_id"]

    rc2 = client.post(
        "/v1/knowledge/graph/root-causes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "service": "api-checkout",
            "cause_kind": "root_cause",
            "cause_key": "connpool-exhaustion",
            "cause_name": "connection pool exhaustion",
            "confidence": 0.6,
        },
    )
    assert rc2.status_code == 200, rc2.text

    causes = client.get(
        "/v1/knowledge/graph/known-causes",
        params={"project_id": PROJECT_ID, "service": "api-checkout"},
    )
    assert causes.status_code == 200, causes.text
    cause_keys = [c["node_key"] for c in causes.json()["items"]]
    assert cause_keys == ["deploy-abc123", "connpool-exhaustion"]
    assert causes.json()["items"][0]["attrs"]["evidence"]["diff"] == "db.py"


def test_knowledge_graph_seed_from_infra(client, engine):
    rid = make_id("infra")
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO infra_resources
                  (id, project_id, provider, resource_type, resource_name,
                   external_resource_id, region, size, image, ttl_minutes,
                   expires_at, status)
                VALUES (:id, :p, 'digitalocean', 'droplet', 'web-1', 'do-kg-e2e',
                        'nyc3', 's-1vcpu', 'ubuntu-22', 60, :exp, 'active')
                """
            ),
            {"id": rid, "p": PROJECT_ID, "exp": now_utc()},
        )

    seed = client.post(
        "/v1/knowledge/graph/seed-from-infra",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert seed.status_code == 200, seed.text
    assert seed.json()["resources"] >= 1

    listed = client.get(
        "/v1/knowledge/graph/nodes",
        params={
            "project_id": PROJECT_ID,
            "kind": "resource",
            "node_key": "do-kg-e2e",
        },
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["attrs"]["provider"] == "digitalocean"
    assert items[0]["layer"] == "deterministic"


def test_knowledge_graph_mutations_require_actor(client):
    bad = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": "unknown_nobody",
            "kind": "service",
            "node_key": "denied",
        },
    )
    assert bad.status_code == 403

    unknown_kind = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "kind": "not-a-kind",
            "node_key": "x",
        },
    )
    assert unknown_kind.status_code == 400

    missing = client.get(
        "/v1/knowledge/graph/neighbors",
        params={"project_id": PROJECT_ID},
    )
    assert missing.status_code == 400


def test_knowledge_graph_subgraph_missing_root(client):
    r = client.get(
        "/v1/knowledge/graph/subgraph",
        params={
            "project_id": PROJECT_ID,
            "kind": "service",
            "node_key": "does-not-exist-e2e",
            "depth": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["root"] is None
    assert body["nodes"] == []
    assert body["edges"] == []
