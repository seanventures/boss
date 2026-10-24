"""Impact questions answered by the code graph, against a real database (recorded session 2026-10-24).

BOSS's agent, on camera, on Apache Fineract: "what calls into the due/in-advance strategy's
repayment allocation" and "who uses determineProcessor" returned 0 ``calls`` edges (the callers
went through ``this``, an injected field, a factory's result, an interface); variable-length
Cypher (``extends*1..6``) ran past the 5 s limit on 51k nodes (AGE's label tables had no
indexes and a fresh graph no statistics); ``kg_schema`` failed twice on a freshly indexed
project (a parallel hash join's shared memory, refused by the database container's /dev/shm);
and a plain phrase found nothing in Knowledge search. Each is checked here end to end: index,
project into AGE, ask through the service the tools and the console use.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import age
from boss_engine.services.knowledge.code_index import index_files, index_repository, symbol_key
from boss_engine.services.knowledge.service import KnowledgeService, graph_schema
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
P = "proj_cg"
REPO = "bank/loan"

TREE = {
    "core/src/bank/core/Processor.java": """
        package bank.core;
        public interface Processor { void process(String code); }
        """,
    "core/src/bank/core/AbstractProcessor.java": """
        package bank.core;
        public abstract class AbstractProcessor implements Processor {
            public void process(String code) { if (code != null) { allocateRepaymentInAdvance(code); } }
            protected abstract void allocateRepaymentInAdvance(String code);
        }
        """,
    "loan/src/bank/loan/DueInAdvance.java": """
        package bank.loan;
        import bank.core.AbstractProcessor;
        public class DueInAdvance extends AbstractProcessor {
            protected void allocateRepaymentInAdvance(String code) { }
        }
        """,
    "loan/src/bank/loan/Fifo.java": """
        package bank.loan;
        public class Fifo extends DueInAdvance { }
        """,
    "loan/src/bank/loan/ProcessorFactory.java": """
        package bank.loan;
        import bank.core.Processor;
        public class ProcessorFactory { public Processor determineProcessor(String c) { return new Fifo(); } }
        """,
    "loan/src/bank/loan/OtherFactory.java": """
        package bank.loan;
        public class OtherFactory { public Object determineProcessor(String c) { return null; } }
        """,
    "svc/src/bank/svc/LoanService.java": """
        package bank.svc;
        import bank.core.Processor;
        import bank.loan.ProcessorFactory;
        public class LoanService {
            private final ProcessorFactory factory;
            public LoanService(ProcessorFactory factory) { this.factory = factory; }
            void repay(String c) { final Processor p = factory.determineProcessor(c); p.process(c); }
            void preview(String c) { factory.determineProcessor(c).process(c); }
        }
        """,
}


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
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_cg', 'CG')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_cg', 'org_cg', 'CG')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_cg', 'ws_cg', 'CG', 'cg')"))
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture()
def indexed(connection, tmp_path) -> Path:
    for path, body in TREE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())
    index_repository(connection, STORE, project_id=P, root=tmp_path, repo=REPO)
    return tmp_path


def _age(connection) -> None:
    if not age.available(connection):
        pytest.skip("Apache AGE is not available on this server")


def _node(connection, path: str, qual: str) -> str:
    return connection.execute(text("SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = :k"),
                              {"p": P, "k": symbol_key(REPO, path, qual)}).scalar_one()


def _cypher(connection, query: str, *columns: str) -> list[dict]:
    return SERVICE.cypher(connection, project_id=P, query=query, columns=list(columns or ["name"]))["rows"]


def test_the_two_impact_questions_return_the_real_callers_through_cypher(connection, indexed):
    _age(connection)
    callers = _cypher(connection, "MATCH (c:Code)-[:calls]->(m:Code {name: 'ProcessorFactory.determineProcessor'}) "
                                  "RETURN c.name")
    assert {r["name"] for r in callers} == {"LoanService.repay", "LoanService.preview"}
    # Who reaches the in-advance strategy's allocation: the bare this-call in the abstract base
    # dispatches to it (and to Fifo, which inherits it, nothing of its own).
    reach = _cypher(connection, "MATCH (c:Code)-[:dispatches_to]->(m:Code {name: 'DueInAdvance.allocateRepaymentInAdvance'}) "
                                "RETURN c.name")
    assert [r["name"] for r in reach] == ["AbstractProcessor.process"]
    via_overrides = _cypher(connection, "MATCH (c:Code)-[:calls]->(:Code)<-[:overrides]-(m:Code "
                                        "{name: 'DueInAdvance.allocateRepaymentInAdvance'}) RETURN c.name")
    assert [r["name"] for r in via_overrides] == ["AbstractProcessor.process"]
    # Through the interface, two hops back from the strategy to the service that triggers it.
    two_hops = _cypher(connection, "MATCH (s:Code)-[:calls]->(:Code {name: 'Processor.process'}) RETURN DISTINCT s.name")
    assert {r["name"] for r in two_hops} == {"LoanService.repay", "LoanService.preview"}


def test_kg_callers_lists_dispatch_callers_with_their_edge_kind(connection, indexed):
    target = _node(connection, "loan/src/bank/loan/DueInAdvance.java", "DueInAdvance.allocateRepaymentInAdvance")
    rows = SERVICE.callers(connection, project_id=P, node_id=target)["callers"]
    assert [(r["name"], r["edge_kind"]) for r in rows] == [("AbstractProcessor.process", "dispatches_to")]
    decl = _node(connection, "core/src/bank/core/AbstractProcessor.java", "AbstractProcessor.allocateRepaymentInAdvance")
    assert [(r["name"], r["edge_kind"]) for r in SERVICE.callers(connection, project_id=P, node_id=decl)["callers"]] \
        == [("AbstractProcessor.process", "calls")]


def test_every_label_table_is_indexed_and_analyzed_and_variable_length_patterns_run(connection, indexed):
    _age(connection)
    graph = age.graph_name(P)
    labels = connection.execute(text(
        "SELECT l.name, l.kind, l.relation::regclass::text FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph WHERE g.name = :g AND l.name NOT LIKE '\\_ag%'"),
        {"g": graph}).all()
    assert {"Code", "calls", "dispatches_to", "overrides", "extends", "implements"} <= {r[0] for r in labels}
    for label, kind, _rel in labels:
        defs = " ".join(connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = :g AND tablename = :t"),
            {"g": graph, "t": label}).scalars().all())
        wanted = ("(id)", "gin (properties)", '"name"', '"node_id"') if kind == "v" else ("(id)", "(start_id)", "(end_id)")
        assert all(w in defs for w in wanted), (label, defs)
    assert age.index_graph(connection, graph) == 0                     # idempotent
    rows = _cypher(connection, "MATCH (c)-[:extends*1..6]->(b)-[:implements]->(i) WHERE i.name = 'Processor' "
                               "WITH DISTINCT c RETURN c.name")
    assert {r["name"] for r in rows} == {"DueInAdvance", "Fifo"}
    # A graph just written has statistics: the planner does not guess at an empty table.
    tuples = connection.execute(text("SELECT reltuples FROM pg_class WHERE oid = to_regclass(:t)"),
                                {"t": f'"{graph}"."Code"'}).scalar_one()
    assert tuples >= 8


def test_kg_schema_counts_serially_and_restores_the_session(connection, indexed, monkeypatch):
    from boss_engine.services.knowledge import service

    # Make a parallel plan the planner's first choice, as on a large graph.
    for setting in ("parallel_setup_cost = 0", "parallel_tuple_cost = 0", "min_parallel_table_scan_size = 0"):
        connection.exec_driver_sql(f"SET LOCAL {setting}")
    seen = {}
    real = service._count_schema

    def spy(conn, project_id, layers):
        seen["workers"] = conn.exec_driver_sql("SHOW max_parallel_workers_per_gather").scalar()
        return real(conn, project_id, layers)

    monkeypatch.setattr(service, "_count_schema", spy)
    service._SCHEMA_CACHE.clear()
    schema = graph_schema(connection, project_id=P)
    assert seen["workers"] == "0"
    assert connection.exec_driver_sql("SHOW max_parallel_workers_per_gather").scalar() != "0"
    rel = schema["relationships"]
    assert rel["calls"]["count"] >= 4 and rel["dispatches_to"]["count"] >= 1 and rel["overrides"]["count"] >= 2
    assert "dispatches_to" in schema["notes"]["calls"]


def test_a_plain_phrase_finds_the_symbols_it_names_and_a_symbol_name_still_does(connection, indexed):
    # The console's Knowledge search and kg_search are this one call.
    phrase = SERVICE.search(connection, project_id=P, query="repayment allocation in advance")
    names = [h["name"] for h in phrase["hits"]]
    assert "DueInAdvance.allocateRepaymentInAdvance" in names[:5], names
    # Not every word in one name: the closest names still come back, ranked by words matched.
    partial = SERVICE.search(connection, project_id=P, query="repayment allocation strategy")
    assert any(n.endswith("allocateRepaymentInAdvance") for n in (h["name"] for h in partial["hits"]))
    exact = SERVICE.search(connection, project_id=P, query="determineProcessor")
    assert {"ProcessorFactory.determineProcessor", "OtherFactory.determineProcessor"} <= {h["name"] for h in exact["hits"]}


def test_an_incremental_index_moves_a_call_when_a_field_changes_type(connection, indexed):
    _age(connection)
    changed = textwrap.dedent(TREE["svc/src/bank/svc/LoanService.java"]).replace(
        "import bank.loan.ProcessorFactory;", "import bank.loan.OtherFactory;").replace(
        "ProcessorFactory factory", "OtherFactory factory").replace(
        "final Processor p = factory.determineProcessor(c); p.process(c);", "factory.determineProcessor(c);").replace(
        "factory.determineProcessor(c).process(c);", "factory.determineProcessor(c);")
    index_files(connection, STORE, project_id=P, repo=REPO,
                files=[{"path": "svc/src/bank/svc/LoanService.java", "language": "java", "source": changed}])
    callers = {r["name"]: r["target"] for r in _cypher(
        connection, "MATCH (c:Code)-[:calls]->(m:Code) WHERE m.name ENDS WITH '.determineProcessor' "
                    "RETURN c.name, m.name", "name", "target")}
    assert callers == {"LoanService.repay": "OtherFactory.determineProcessor",
                       "LoanService.preview": "OtherFactory.determineProcessor"}
