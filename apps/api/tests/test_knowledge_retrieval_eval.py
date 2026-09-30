"""BOSS-357 — knowledge retrieval evaluation, coverage metrics, regression alarms."""
from __future__ import annotations

from apps.api.app.db import begin
from apps.api.app.services.knowledge_graph import KnowledgeGraphService

KG = KnowledgeGraphService()
PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def _seed_graph(connection, project_id: str) -> None:
    """Synthetic connected topology for coverage scoring."""
    svc = KG.upsert_node(
        connection, project_id, "service", "eval-api", "Eval API",
        source="manual", confidence=1.0, layer="deterministic",
    )
    pay = KG.upsert_node(
        connection, project_id, "service", "eval-pay", "Eval Pay",
        source="manual", confidence=0.95, layer="deterministic",
    )
    res = KG.upsert_node(
        connection, project_id, "resource", "eval-pg", "Eval Postgres",
        source="infra_resources", confidence=1.0,
    )
    repo = KG.upsert_node(
        connection, project_id, "repo", "org/eval-api",
        source="walk_k8s", confidence=0.9,
    )
    KG.upsert_edge(connection, project_id, svc, pay, "depends_on", confidence=1.0)
    KG.upsert_edge(connection, project_id, pay, res, "runs_on", confidence=1.0)
    KG.upsert_edge(connection, project_id, svc, repo, "deployed_from", confidence=0.9)
    KG.record_root_cause(
        connection, project_id, "eval-api", "deployment", "eval-deploy-1",
        "deploy eval-1", confidence=0.85,
    )


def test_compute_retrieval_eval_coverage_on_synthetic_nodes(engine):
    with begin(engine) as c:
        # Empty graph → low score + alarm under default threshold
        empty = KG.compute_retrieval_eval(
            c, PROJECT_ID, coverage_threshold=0.35, persist=True
        )
        assert empty["project_id"] == PROJECT_ID
        assert empty["coverage_score"] == 0.0
        assert empty["alarm"] is True
        assert empty["below_threshold"] is True
        assert empty["snapshot_id"]
        assert empty["metrics"]["node_count"] == 0

        _seed_graph(c, PROJECT_ID)
        evaled = KG.compute_retrieval_eval(
            c, PROJECT_ID,
            coverage_threshold=0.2,
            persist=True,
        )
        m = evaled["metrics"]
        assert m["node_count"] >= 4
        assert m["edge_count"] >= 3
        assert m["services"] >= 2
        assert m["resources"] >= 1
        assert m["services_with_dependency"] >= 1
        assert m["services_with_known_cause"] >= 1
        assert 0.0 < evaled["coverage_score"] <= 1.0
        assert evaled["coverage_score"] >= 0.2
        assert evaled["below_threshold"] is False
        # prior was 0.0 — no relative regression when prior is zero
        assert "by_kind" in m
        assert "service" in m["by_kind"]


def test_regression_alarm_when_coverage_drops(engine):
    with begin(engine) as c:
        _seed_graph(c, PROJECT_ID)
        high = KG.compute_retrieval_eval(
            c, PROJECT_ID, coverage_threshold=0.05, persist=True
        )
        assert high["alarm"] is False or high["below_threshold"] is False
        high_score = high["coverage_score"]
        assert high_score > 0.1

        # Wipe the graph to force a large relative drop
        from sqlalchemy import text as sql_text

        c.execute(sql_text("DELETE FROM kg_edges WHERE project_id = :p"), {"p": PROJECT_ID})
        c.execute(sql_text("DELETE FROM kg_nodes WHERE project_id = :p"), {"p": PROJECT_ID})
        low = KG.compute_retrieval_eval(
            c, PROJECT_ID, coverage_threshold=0.0, persist=True
        )
        assert low["coverage_score"] == 0.0
        assert low["regression"] is True
        assert low["alarm"] is True
        assert any("regressed" in r for r in low["alarm_reasons"])
        assert low["prior_coverage_score"] == high_score


def test_http_knowledge_eval_endpoint(client, engine):
    # Seed via HTTP so the eval sees data
    for kind, key, name in (
        ("service", "http-eval-svc", "HTTP Eval Svc"),
        ("resource", "http-eval-res", "HTTP Eval Res"),
    ):
        r = client.post(
            "/v1/knowledge/graph/nodes",
            json={
                "project_id": PROJECT_ID,
                "actor": ACTOR,
                "kind": kind,
                "node_key": key,
                "name": name,
                "source": "manual",
                "confidence": 1.0,
            },
        )
        assert r.status_code == 200, r.text

    # Link them
    nodes = client.get(
        "/v1/knowledge/graph/nodes",
        params={"project_id": PROJECT_ID, "kind": "service", "node_key": "http-eval-svc"},
    ).json()["items"]
    res_nodes = client.get(
        "/v1/knowledge/graph/nodes",
        params={"project_id": PROJECT_ID, "kind": "resource", "node_key": "http-eval-res"},
    ).json()["items"]
    edge = client.post(
        "/v1/knowledge/graph/edges",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "src_id": nodes[0]["id"],
            "dst_id": res_nodes[0]["id"],
            "kind": "runs_on",
            "source": "manual",
            "confidence": 1.0,
        },
    )
    assert edge.status_code == 200, edge.text

    res = client.get(
        "/v1/knowledge/eval",
        params={"project_id": PROJECT_ID, "threshold": 0.05},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project_id"] == PROJECT_ID
    assert body["metrics"]["node_count"] >= 2
    assert body["metrics"]["edge_count"] >= 1
    assert "coverage_score" in body
    assert "alarm" in body
    assert body["snapshot_id"]
