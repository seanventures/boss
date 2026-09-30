"""RB-15 — knowledge graph: idempotent upserts, RCA-context subgraph, learning write-back,
and deterministic seeding from infra_resources. Runs against the isolated boss_test DB."""
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.core import make_id, now_utc
from apps.api.app.services.knowledge_graph import KnowledgeGraphService

KG = KnowledgeGraphService()


def _a_project(connection):
    return connection.execute(
        text("SELECT id FROM projects ORDER BY created_at LIMIT 1")
    ).scalar_one()


def test_upsert_node_is_idempotent_and_merges(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        a = KG.upsert_node(c, pid, "service", "checkout", "Checkout API", attrs={"team": "payments"})
        # same natural key -> same node id, attrs shallow-merged, name refreshed
        b = KG.upsert_node(c, pid, "service", "checkout", "Checkout", attrs={"lang": "go"})
        assert a == b
        node = KG.get_node(c, pid, "service", "checkout")
        assert node["name"] == "Checkout"
        assert node["attrs"] == {"team": "payments", "lang": "go"}


def test_deterministic_sighting_outranks_inferred(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        KG.upsert_node(c, pid, "service", "billing", layer="inferred", confidence=0.4)
        KG.upsert_node(c, pid, "service", "billing", layer="deterministic", confidence=1.0)
        node = KG.get_node(c, pid, "service", "billing")
        assert node["layer"] == "deterministic" and node["confidence"] == 1.0


def test_subgraph_gathers_rca_context(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        # sg_web --depends_on--> sg_pay --runs_on--> sg_db ; sg_web --deployed_from--> repo
        # (keys namespaced to this test so the exact-membership assertions can't collide with others)
        svc = KG.upsert_node(c, pid, "service", "sg_web")
        pay = KG.upsert_node(c, pid, "service", "sg_pay")
        db = KG.upsert_node(c, pid, "datastore", "sg_db")
        repo = KG.upsert_node(c, pid, "repo", "org/sg_web")
        KG.upsert_edge(c, pid, svc, pay, "depends_on")
        KG.upsert_edge(c, pid, pay, db, "runs_on")
        KG.upsert_edge(c, pid, svc, repo, "deployed_from")

        # depth 1 from sg_web: sg_pay + repo, not the 2-hop datastore
        near = KG.subgraph(c, pid, "service", "sg_web", depth=1)
        keys = {n["node_key"] for n in near["nodes"]}
        assert keys == {"sg_web", "sg_pay", "org/sg_web"}

        # depth 2 pulls in the datastore behind sg_pay
        far = KG.subgraph(c, pid, "service", "sg_web", depth=2)
        assert "sg_db" in {n["node_key"] for n in far["nodes"]}
        assert any(e["kind"] == "runs_on" for e in far["edges"])


def test_subgraph_missing_root_is_empty(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        assert KG.subgraph(c, pid, "service", "nope")["nodes"] == []


def test_record_root_cause_and_known_causes_learn(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        KG.record_root_cause(c, pid, "checkout", "deployment", "deploy-a5cb9fe",
                             "deploy a5cb9fe", confidence=0.9,
                             evidence={"onset": "14:32", "diff": "db.py"})
        # a second, lower-confidence cause for the same service
        KG.record_root_cause(c, pid, "checkout", "root_cause", "connpool-exhaustion",
                             "connection pool exhaustion", confidence=0.6)
        causes = KG.known_causes(c, pid, "checkout")
        assert [x["node_key"] for x in causes] == ["deploy-a5cb9fe", "connpool-exhaustion"]  # by confidence
        assert causes[0]["attrs"]["evidence"]["diff"] == "db.py"


def test_record_root_cause_is_idempotent(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        KG.record_root_cause(c, pid, "api", "deployment", "d1", "deploy 1", confidence=0.5)
        KG.record_root_cause(c, pid, "api", "deployment", "d1", "deploy 1", confidence=0.8)
        causes = KG.known_causes(c, pid, "api")
        assert len(causes) == 1 and causes[0]["confidence"] == 0.8  # merged, confidence raised


def test_seed_from_infra_resources(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        rid = make_id("infra")
        c.execute(
            text(
                """
                INSERT INTO infra_resources
                  (id, project_id, provider, resource_type, resource_name, external_resource_id,
                   region, size, image, ttl_minutes, expires_at, status)
                VALUES (:id, :p, 'digitalocean', 'droplet', 'web-1', 'do-12345',
                        'nyc3', 's-1vcpu', 'ubuntu-22', 60, :exp, 'active')
                """
            ),
            {"id": rid, "p": pid, "exp": now_utc()},
        )
        out = KG.seed_from_infra_resources(c, pid)
        assert out["resources"] >= 1
        node = KG.get_node(c, pid, "resource", "do-12345")
        assert node and node["attrs"]["provider"] == "digitalocean" and node["layer"] == "deterministic"
