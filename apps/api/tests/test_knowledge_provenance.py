"""BOSS-355 — knowledge provenance, temporal validity, confidence, tenant scope.

Covers service + HTTP filters for source / confidence / valid_from / valid_to
and hard project isolation on neighbors / edges.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.knowledge_graph import KnowledgeGraphService

KG = KnowledgeGraphService()
PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def _a_project(connection):
    return connection.execute(
        text("SELECT id FROM projects ORDER BY created_at LIMIT 1")
    ).scalar_one()


def test_upsert_carries_provenance_and_temporal_validity(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        now = datetime.now(timezone.utc)
        node_id = KG.upsert_node(
            c,
            pid,
            "service",
            "prov-svc",
            "Provenance Service",
            source="walk_k8s",
            confidence=0.72,
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=30),
            attrs={"cluster": "prod"},
        )
        node = KG.get_node(c, pid, "service", "prov-svc")
        assert node is not None
        assert node["id"] == node_id
        assert node["source"] == "walk_k8s"
        assert float(node["confidence"]) == 0.72
        assert node["valid_from"] is not None
        assert node["valid_to"] is not None
        assert node["attrs"]["cluster"] == "prod"


def test_list_nodes_filters_source_confidence_and_valid_at(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        now = datetime.now(timezone.utc)
        KG.upsert_node(
            c, pid, "service", "live-svc", source="manual", confidence=0.9,
            valid_from=now - timedelta(hours=1), valid_to=now + timedelta(days=1),
        )
        KG.upsert_node(
            c, pid, "service", "stale-svc", source="walk_k8s", confidence=0.4,
            valid_from=now - timedelta(days=10), valid_to=now - timedelta(days=1),
        )
        KG.upsert_node(
            c, pid, "resource", "res-1", source="infra_resources", confidence=1.0,
        )

        by_source = KG.list_nodes(c, pid, source="manual")
        assert {n["node_key"] for n in by_source} == {"live-svc"}

        high_conf = KG.list_nodes(c, pid, min_confidence=0.8)
        keys = {n["node_key"] for n in high_conf}
        assert "live-svc" in keys
        assert "res-1" in keys
        assert "stale-svc" not in keys

        # valid_at=now excludes expired stale-svc
        live = KG.list_nodes(c, pid, valid_at=now.isoformat())
        live_keys = {n["node_key"] for n in live}
        assert "live-svc" in live_keys
        assert "res-1" in live_keys  # unbounded validity
        assert "stale-svc" not in live_keys

        # include_expired surfaces the stale node
        all_nodes = KG.list_nodes(c, pid, include_expired=True)
        assert "stale-svc" in {n["node_key"] for n in all_nodes}


def test_tenant_scope_rejects_cross_project_edge(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        # Create a second project for isolation
        other = c.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                SELECT 'proj-other-kg', workspace_id, 'Other KG', 'other-kg'
                FROM projects WHERE id = :p
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """
            ),
            {"p": pid},
        ).scalar()
        if other is None:
            # already exists from a prior run
            other = "proj-other-kg"

        a = KG.upsert_node(c, pid, "service", "tenant-a")
        b = KG.upsert_node(c, other, "service", "tenant-b")

        try:
            KG.upsert_edge(c, pid, a, b, "depends_on")
            assert False, "expected PermissionError for cross-tenant edge"
        except PermissionError:
            pass

        # neighbors with project_id must refuse foreign node ids
        try:
            KG.neighbors(c, b, project_id=pid)
            assert False, "expected PermissionError for foreign node"
        except PermissionError:
            pass


def test_http_upsert_and_filter_provenance(client, engine):
    now = datetime.now(timezone.utc)
    created = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "kind": "service",
            "node_key": "http-prov",
            "name": "HTTP Prov",
            "source": "investigation",
            "confidence": 0.55,
            "valid_from": (now - timedelta(days=1)).isoformat(),
            "valid_to": (now + timedelta(days=7)).isoformat(),
            "attrs": {"ticket": "INC-1"},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source"] == "investigation"
    assert body["confidence"] == 0.55
    assert body["valid_from"] is not None

    listed = client.get(
        "/v1/knowledge/graph/nodes",
        params={
            "project_id": PROJECT_ID,
            "source": "investigation",
            "min_confidence": 0.5,
        },
    )
    assert listed.status_code == 200, listed.text
    keys = {i["node_key"] for i in listed.json()["items"]}
    assert "http-prov" in keys
    assert listed.json()["count"] == len(listed.json()["items"])

    low = client.get(
        "/v1/knowledge/graph/nodes",
        params={
            "project_id": PROJECT_ID,
            "source": "investigation",
            "min_confidence": 0.9,
        },
    )
    assert low.status_code == 200
    assert "http-prov" not in {i["node_key"] for i in low.json()["items"]}


def test_http_unknown_project_404(client):
    res = client.get(
        "/v1/knowledge/graph/nodes",
        params={"project_id": "does-not-exist-project"},
    )
    assert res.status_code == 404
