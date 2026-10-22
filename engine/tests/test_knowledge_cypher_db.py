"""Cypher works, read-only, over a per-project AGE projection (operator decision 2026-10-22).

Breaks covered: a projection that disagrees with the recursive CTE; a write through Cypher (by
keyword, and past the keyword check by the READ ONLY transaction); a query that runs on past its
timeout; one project's graph reachable from another's; a triage stage reading the Code label or
an unlabelled node; a forgotten node lingering in the projection; drift nobody repairs.
Needs Apache AGE (the boss-community-db image); skipped on a stock postgres.
"""
from __future__ import annotations

import os

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import age, scopes
from boss_engine.services.knowledge.code_index import index_files
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
CHAIN = "def one():\n    return two()\n\ndef two():\n    return three()\n\ndef three():\n    return 3\n"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            if not connection.execute(text(
                    "SELECT 1 FROM pg_available_extensions WHERE name = 'age'")).first():
                pytest.skip("Apache AGE is not available on this server")
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_cy', 'CY')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_cy', 'org_cy', 'CY')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                              "('proj_cy', 'ws_cy', 'CY', 'cy'), ('proj_cy2', 'ws_cy', 'CY2', 'cy2')"))
            index_files(conn, STORE, project_id="proj_cy",
                        files=[{"path": "chain.py", "language": "python", "source": CHAIN}])
            index_files(conn, STORE, project_id="proj_cy2",
                        files=[{"path": "other.py", "language": "python", "source": "def elsewhere():\n    return 1\n"}])
            incident = STORE.upsert_node(conn, project_id="proj_cy", kind="incident", node_key="incident:inc-9",
                                         name="Checkout 500s", body="three() raised", trust_tier="observed",
                                         actor_kind="system")
            age.after_write(conn, "proj_cy")
            yield conn, incident
        finally:
            transaction.rollback()
            clear_llm_context()


def _sym(conn, qual: str) -> str:
    return STORE.find_by_key(conn, project_id="proj_cy", kind="symbol", node_key=f"symbol:chain.py:{qual}")["id"]


def _open_counts(conn, project_id: str) -> dict[str, int]:
    return {"nodes": conn.execute(text("SELECT COUNT(*) FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL"),
                                  {"p": project_id}).scalar_one(),
            "edges": conn.execute(text("SELECT COUNT(*) FROM kg_edges WHERE project_id = :p AND valid_to IS NULL"),
                                  {"p": project_id}).scalar_one()}


def test_the_projection_matches_the_tables_and_the_cte(connection):
    conn, _incident = connection
    assert age.projection_counts(conn, "proj_cy") == _open_counts(conn, "proj_cy")
    start = _sym(conn, "one")
    walked = [(r["node_id"], r["depth"]) for r in STORE.traverse(conn, project_id="proj_cy", start_id=start, max_depth=2)]
    aged = [(r["node_id"], r["depth"]) for r in age.age_neighbors(conn, project_id="proj_cy", start_id=start, max_depth=2)]
    assert walked == aged and walked
    calls = SERVICE.cypher(conn, project_id="proj_cy", columns=["caller", "callee"],
                           query="MATCH (a:Code)-[:calls]->(b:Code) RETURN a.name, b.name")
    assert sorted((r["caller"], r["callee"]) for r in calls["rows"]) == [("one", "two"), ("two", "three")]


def test_writes_are_refused_by_keyword_and_by_the_read_only_transaction(connection, monkeypatch):
    conn, _incident = connection
    for query in ("CREATE (n:Ops {name: 'x'}) RETURN n", "MATCH (n) DETACH DELETE n RETURN 1",
                  "MATCH (n) SET n.name = 'y' RETURN n", "MERGE (n:Ops {name: 'z'}) RETURN n"):
        with pytest.raises(ValueError, match="read-only"):
            SERVICE.cypher(conn, project_id="proj_cy", query=query)
    assert SERVICE.cypher(conn, project_id="proj_cy", query="MATCH (n) WHERE n.name = 'CREATE' RETURN n.name")
    monkeypatch.setattr(age, "check_read_only", lambda query: None)                  # past the keyword check
    with pytest.raises(ValueError, match="read-only"):
        age.run_cypher(conn, project_id="proj_cy", query="CREATE (n:Ops {name: 'x'}) RETURN n")
    assert age.projection_counts(conn, "proj_cy") == _open_counts(conn, "proj_cy")      # nothing written


def test_the_timeout_fires(connection):
    conn, _incident = connection
    for i in range(300):
        STORE.upsert_node(conn, project_id="proj_cy", kind="topic", node_key=f"topic:t{i}", name=f"t{i}",
                          body="", trust_tier="observed", actor_kind="system")
    age.after_write(conn, "proj_cy")
    with pytest.raises(TimeoutError, match="ran past 1 ms"):
        age.run_cypher(conn, project_id="proj_cy", timeout_ms=1,
                       query="MATCH (a), (b), (c) RETURN count(*)")
    assert conn.execute(text("SELECT 1")).scalar() == 1                                  # the caller goes on


def test_a_project_reads_only_its_own_graph(connection):
    conn, _incident = connection
    age.after_write(conn, "proj_cy2")
    assert age.graph_name("proj_cy") != age.graph_name("proj_cy2")
    mine = {r["result"] for r in SERVICE.cypher(conn, project_id="proj_cy2", query="MATCH (n) RETURN n.node_key")["rows"]}
    assert "symbol:other.py:elsewhere" in mine and not any("chain.py" in key for key in mine)


def test_triage_is_refused_the_code_label_in_words(connection):
    conn, incident = connection
    triage = scopes.policy_for(stage="triage")
    with pytest.raises(PermissionError, match="Code isn't available while triaging; it opens in Investigate"):
        SERVICE.cypher(conn, project_id="proj_cy", policy=triage, query="MATCH (n:Code) RETURN n.name")
    with pytest.raises(PermissionError, match="every node in a pattern names its label"):
        SERVICE.cypher(conn, project_id="proj_cy", policy=triage, query="MATCH (n) RETURN n.name")
    with pytest.raises(PermissionError, match="variable-length"):
        SERVICE.cypher(conn, project_id="proj_cy", policy=triage, query="MATCH (a:Ops)-[*1..3]-(b:Ops) RETURN b")
    found = SERVICE.cypher(conn, project_id="proj_cy", policy=triage, query="MATCH (i:Ops) RETURN i.node_id")
    assert [r["result"] for r in found["rows"]] == [incident]

    from contextlib import contextmanager

    from boss_engine.tools.builtin import build_default_registry

    class _Ctx:
        @contextmanager
        def begin(self):
            yield conn

    set_llm_context(stage="triage")
    out = build_default_registry().get("kg_cypher").handler(
        _Ctx(), project_id="proj_cy", actor="agent", args={"query": "MATCH (n:Code) RETURN n"})
    assert out == {"error": "Code isn't available while triaging; it opens in Investigate."}


def test_the_projection_follows_writes_forgets_and_repairs_drift(connection):
    conn, incident = connection
    fact = STORE.upsert_node(conn, project_id="proj_cy", kind="fact", node_key="fact:three|raises|on-empty",
                             name="three raises on empty", body="three raises on an empty cart",
                             trust_tier="observed", actor_kind="system")
    STORE.upsert_edge(conn, project_id="proj_cy", src_id=fact, dst_id=_sym(conn, "three"), kind="about",
                      actor_kind="system")
    age.after_write(conn, "proj_cy")
    about = SERVICE.cypher(conn, project_id="proj_cy", query="MATCH (f:Knowledge)-[:about]->(s:Code) RETURN s.name")
    assert [r["result"] for r in about["rows"]] == ["three"]
    STORE.delete_node(conn, project_id="proj_cy", node_id=fact, actor_kind="person")       # a forget
    assert not SERVICE.cypher(conn, project_id="proj_cy", query="MATCH (f:Knowledge) RETURN f")["rows"]
    name = age.graph_name("proj_cy")
    conn.execute(text(f'DELETE FROM "{name}"._ag_label_vertex WHERE id::text IN '
                      f'(SELECT graphid FROM kg_age_vertices WHERE node_id = :n)'),
                 {"n": incident})                                                          # drift
    assert age.reconcile(conn, "proj_cy")["mode"] == "rebuild"
    assert age.projection_counts(conn, "proj_cy") == _open_counts(conn, "proj_cy")
    assert age.reconcile(conn, "proj_cy")["mode"] == "current"
