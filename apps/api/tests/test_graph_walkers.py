"""RB-15 (remainder) — graph walkers: k8s deterministic walk (stubbed wire), incident
co-occurrence inference, and the graph read/walk endpoints. Against boss_test."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services import graph_walkers as gw
from apps.api.app.services.core import make_id, now_utc
from apps.api.app.services.knowledge_graph import KnowledgeGraphService

PID = "boss-dogfood"
ACTOR = "executive_console"
KG = KnowledgeGraphService()


@pytest.fixture
def k8s_wire(monkeypatch):
    def fake(url, *, headers=None):
        if "/services" in url:
            return {"items": [{"metadata": {"name": "checkout", "namespace": "prod"},
                               "spec": {"selector": {"app": "checkout"}}}]}
        if "/pods" in url:
            return {"items": [
                {"metadata": {"labels": {"app": "checkout"}}, "spec": {"nodeName": "node-a"}},
                {"metadata": {"labels": {"app": "checkout"}}, "spec": {"nodeName": "node-b"}}]}
        if "/nodes" in url:
            return {"items": [{"metadata": {"name": "node-a"}}, {"metadata": {"name": "node-b"}}]}
        raise AssertionError(url)
    monkeypatch.setattr(gw, "_http_json", fake)


def test_walk_kubernetes_builds_topology(k8s_wire, engine):
    with begin(engine) as c:
        counts = gw.walk_kubernetes(c, PID, KG, "https://k8s:6443")
        assert counts == {"services": 1, "pods": 2, "nodes": 2, "edges": 2}
        sub = KG.subgraph(c, PID, "service", "checkout", depth=1)
        keys = {n["node_key"] for n in sub["nodes"]}
        assert {"checkout", "node-a", "node-b"} <= keys
        assert all(e["kind"] == "runs_on" for e in sub["edges"])
        # idempotent — a second walk converges, no duplicates
        counts2 = gw.walk_kubernetes(c, PID, KG, "https://k8s:6443")
        assert counts2["edges"] == 2


def _mk_incident(c, service, start, end):
    c.execute(
        text("INSERT INTO incidents (id, project_id, dedupe_key, title, service, first_seen, last_seen) "
             "VALUES (:id, :p, :k, :t, :svc, :fs, :ls)"),
        {"id": make_id("inc"), "p": PID, "k": make_id("dk"), "t": f"inc {service}",
         "svc": service, "fs": start, "ls": end},
    )


def test_infer_co_occurrence(engine):
    with begin(engine) as c:
        t0 = now_utc()
        # co-a and co-b fail together twice; co-c fails alone far away
        for k in range(2):
            base = t0 + timedelta(hours=k)
            _mk_incident(c, "co-a", base, base + timedelta(minutes=5))
            _mk_incident(c, "co-b", base + timedelta(minutes=10), base + timedelta(minutes=15))
        _mk_incident(c, "co-c", t0 + timedelta(days=2), t0 + timedelta(days=2, minutes=5))
        out = gw.infer_co_occurrence(c, PID, KG, window_minutes=30, min_overlap=2)
        assert out["edges"] >= 1
        sub = KG.subgraph(c, PID, "service", "co-a", depth=1)
        hit = [e for e in sub["edges"] if e["kind"] == "correlates_with"]
        assert hit and hit[0]["confidence"] < 1.0                  # inferred, not certain
        assert "co-c" not in {n["node_key"] for n in sub["nodes"]}


def test_http_graph_read_and_walk(client, engine):
    with begin(engine) as c:
        svc = KG.upsert_node(c, PID, "service", "gw-http")
        db = KG.upsert_node(c, PID, "datastore", "gw-db")
        KG.upsert_edge(c, PID, svc, db, "depends_on")
    r = client.get("/v1/reliability/graph",
                   params={"project_id": PID, "service": "gw-http", "depth": 1})
    assert r.status_code == 200
    assert {n["node_key"] for n in r.json()["nodes"]} == {"gw-http", "gw-db"}

    w = client.post("/v1/reliability/graph/walk", json={
        "project_id": PID, "actor": ACTOR, "walker": "infra", "config": {}})
    assert w.status_code == 200 and w.json()["walker"] == "infra"

    bad = client.post("/v1/reliability/graph/walk", json={
        "project_id": PID, "actor": ACTOR, "walker": "nope"})
    assert bad.status_code == 400
