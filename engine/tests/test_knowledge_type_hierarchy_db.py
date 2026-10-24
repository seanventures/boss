"""The code graph answers "who extends or implements this type" by relationships, not names.

Exam, 2026-10-24: asked which classes implement Fineract's loan repayment schedule transaction
processor, BOSS's agent name-matched symbols with LIMIT 50, got a sample, and called the one
sample class "the only implementation": the graph had no extends/implements edges, the agent was
not told what the graph holds, and a LIMITed result did not say it was cut. Covered here against
a real database: inheritance edges across files, packages and languages (and the Cypher that
walks them), a facts cache of an older shape re-parsed, an incremental index adding and closing
an edge, a LIMITed result that says it is truncated, and the schema summary the agent reads.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import age
from boss_engine.services.knowledge.code_index import index_files, index_repository, symbol_key
from boss_engine.services.knowledge.scopes import policy_for
from boss_engine.services.knowledge.service import KnowledgeService, graph_schema
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
P = "proj_th"

REPO = {
    "core/src/org/bank/core/Processor.java": """
        package org.bank.core;
        public interface Processor<T> { void process(T t); }
        """,
    "core/src/org/bank/core/Named.java": """
        package org.bank.core;
        public interface Named { }
        """,
    "core/src/org/bank/core/AbstractProcessor.java": """
        package org.bank.core;
        public abstract class AbstractProcessor<T> implements Processor<T>, Named {
            public static class Nested extends AbstractProcessor<String> { public void process(String s) { } }
        }
        """,
    "loan/src/org/bank/loan/DueFirstProcessor.java": """
        package org.bank.loan;
        import org.bank.core.AbstractProcessor;
        public class DueFirstProcessor extends AbstractProcessor<Long> { public void process(Long l) { } }
        """,
    "loan/src/org/bank/loan/EarlyDueProcessor.java": """
        package org.bank.loan;
        public class EarlyDueProcessor extends DueFirstProcessor { }
        """,
    "samples/src/com/acme/AcmeProcessor.java": """
        package com.acme;
        public class AcmeProcessor extends org.bank.loan.DueFirstProcessor { }
        """,
    "loan/src/org/bank/loan/ProcessorFactory.java": """
        package org.bank.loan;
        import org.bank.core.Processor;
        public class ProcessorFactory { Processor<Long> make() { return new DueFirstProcessor(); } }
        """,
    "app/base.py": "class Repository:\n    pass\n",
    "app/orders.py": "from app.base import Repository\n\n\nclass Orders(Repository):\n    pass\n",
    "web/src/store.ts": "export interface Store { get(): number }\nexport class BaseStore { }\n",
    "web/src/cart.ts": ("import { Store, BaseStore } from './store';\n"
                        "export class Cart extends BaseStore implements Store { get() { return 1 } }\n"),
    "net/Repo.cs": "namespace Net;\npublic interface IStore { }\npublic class BaseRepo { }\npublic class Repo : BaseRepo, IStore { }\n",
}
STRATEGIES = {"AbstractProcessor", "AbstractProcessor.Nested", "DueFirstProcessor", "EarlyDueProcessor",
              "AcmeProcessor"}


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_th', 'TH')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_th', 'org_th', 'TH')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_th', 'ws_th', 'TH', 'th')"))
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture()
def tree(tmp_path) -> Path:
    for path, body in REPO.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def _inheritance(connection) -> dict[tuple[str, str, str], float]:
    rows = connection.execute(text(
        "SELECT s.name AS src, d.name AS dst, e.kind, e.confidence FROM kg_edges e "
        "JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id "
        "WHERE e.project_id = :p AND e.valid_to IS NULL AND e.kind IN ('extends', 'implements')"),
        {"p": P}).all()
    return {(r.src, r.dst, r.kind): float(r.confidence) for r in rows}


def _has_age(connection) -> bool:
    return age.available(connection) or bool(connection.execute(text(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'age'")).first())


def test_extends_and_implements_edges_resolve_across_files_packages_and_languages(connection, tree):
    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    edges = _inheritance(connection)
    assert {k for k in edges} == {
        ("AbstractProcessor", "Processor", "implements"),
        ("AbstractProcessor", "Named", "implements"),
        ("AbstractProcessor.Nested", "AbstractProcessor", "extends"),   # same file, nested
        ("DueFirstProcessor", "AbstractProcessor", "extends"),          # through the import
        ("EarlyDueProcessor", "DueFirstProcessor", "extends"),          # same package, no import
        ("AcmeProcessor", "DueFirstProcessor", "extends"),              # fully qualified name
        ("Orders", "Repository", "extends"),                            # Python, through the import
        ("Cart", "BaseStore", "extends"), ("Cart", "Store", "implements"),   # TypeScript
        ("Repo", "BaseRepo", "extends"), ("Repo", "IStore", "implements"),   # C#: decided by the base's kind
    }
    assert {k: v for k, v in edges.items() if v != 1.0} == {}
    kinds = dict(connection.execute(text(
        "SELECT name, attrs->>'type_kind' FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' "
        "AND name IN ('Processor', 'AbstractProcessor', 'IStore')"), {"p": P}).all())
    assert kinds == {"Processor": "interface", "AbstractProcessor": "class", "IStore": "interface"}


def test_a_base_found_only_by_its_unique_simple_name_is_marked_lower_confidence(connection, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/Base.java").write_text("package a;\npublic class Base { }\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b/Sub.java").write_text("package b;\nimport c.*;\npublic class Sub extends Base { }\n")
    index_repository(connection, STORE, project_id=P, root=tmp_path, repo="x/y")
    assert _inheritance(connection) == {("Sub", "Base", "extends"): 0.5}


def test_cypher_walks_the_hierarchy_to_every_implementation(connection, tree):
    if not _has_age(connection):
        pytest.skip("Apache AGE is not available on this server")
    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    out = SERVICE.cypher(connection, project_id=P, columns=["name", "path", "type_kind"], query=(
        "MATCH (c:Code)-[:extends*0..3]->(:Code)-[:implements]->(i:Code {name: 'Processor'}) "
        "RETURN DISTINCT c.name, c.path, c.type_kind"))
    assert {r["name"] for r in out["rows"]} == STRATEGIES
    assert all(r["path"].endswith(".java") and r["type_kind"] == "class" for r in out["rows"])
    assert out["truncated"] is False and out["rows_returned"] == len(STRATEGIES)
    # The alternation other Cypher dialects allow is refused with how to write it in AGE.
    with pytest.raises(ValueError, match="UNION"):
        SERVICE.cypher(connection, project_id=P, columns=["name"],
                       query="MATCH (c:Code)-[:extends|implements*1..3]->(i:Code {name: 'Processor'}) RETURN c.name")


def test_a_limited_result_says_it_is_a_sample(connection, tree):
    if not _has_age(connection):
        pytest.skip("Apache AGE is not available on this server")
    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    pattern = "MATCH (c:Code)-[:extends*0..3]->(:Code)-[:implements]->(:Code {name: 'Processor'}) RETURN DISTINCT c.name"
    cut = SERVICE.cypher(connection, project_id=P, columns=["name"], query=pattern + " LIMIT 2")
    assert cut["rows_returned"] == 2 and cut["truncated"] is True and cut["query_limit"] == 2
    assert "LIMIT 2" in cut["truncated_by"] and "sample" in cut["note"]
    exact = SERVICE.cypher(connection, project_id=P, columns=["name"], query=pattern + f" LIMIT {len(STRATEGIES)}")
    assert exact["truncated"] is False and exact["rows_returned"] == len(STRATEGIES) and "note" not in exact
    # The tool's own row limit is reported the same way.
    capped = SERVICE.cypher(connection, project_id=P, columns=["name"], query=pattern, limit=3)
    assert capped["truncated"] is True and capped["truncated_by"] == "the row limit of 3"
    # A LIMIT inside a string is not the query's LIMIT.
    quoted = SERVICE.cypher(connection, project_id=P, columns=["name"],
                            query="MATCH (c:Code {name: 'LIMIT 1'}) RETURN c.name")
    assert quoted["query_limit"] is None and quoted["rows"] == [] and "kg_schema" in quoted["note"]


def test_the_schema_summary_is_counted_from_the_graph_cached_and_scoped(connection, tree):
    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    schema = SERVICE.schema(connection, project_id=P)
    rel = schema["relationships"]
    assert rel["extends"]["count"] == 7 and rel["implements"]["count"] == 4
    assert rel["extends"]["between"] == {"Code->Code": 7}
    assert {"references", "defines", "imports"} <= set(rel)
    code = schema["labels"]["Code"]
    assert code["kinds"]["symbol"] > 10 and code["type_kind"]["interface"] == 4
    assert {"path", "symbol_kind", "type_kind"} <= set(schema["vertex_properties"])
    assert "confidence" in schema["edge_properties"]
    assert any("sample" in line for line in schema["guidance"]) and schema["dialect"]
    # Short enough to read every time it is asked for.
    import json
    assert len(json.dumps(schema)) < 4000
    # Cached until the graph is written again (same object back).
    assert SERVICE.schema(connection, project_id=P) is schema
    # A stage that reads no code is not told about code.
    triage = graph_schema(connection, project_id=P, policy=policy_for(stage="triage"))
    assert "Code" not in triage["labels"] and "extends" not in triage["relationships"]


def test_a_cached_file_of_an_older_facts_shape_is_parsed_again_and_gains_its_edges(connection, tree):
    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    # As a graph built before inheritance was recorded: facts without bases, no version, no edges.
    connection.execute(text(
        "UPDATE kg_nodes SET attrs = jsonb_set(attrs, '{facts}', (attrs->'facts') - 'bases' - 'types' - 'v') "
        "WHERE project_id = :p AND kind = 'file'"), {"p": P})
    connection.execute(text("DELETE FROM kg_edges WHERE project_id = :p AND kind IN ('extends', 'implements')"),
                       {"p": P})
    run = index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    assert run["parsed"] == run["files"] == len(REPO)
    assert len(_inheritance(connection)) == 11
    # And a current cache is not parsed again.
    assert index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")["parsed"] == 0


def test_an_incremental_index_adds_and_closes_an_inheritance_edge(connection, tree):
    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    path = "loan/src/org/bank/loan/LateProcessor.java"
    source = "package org.bank.loan;\npublic class LateProcessor extends DueFirstProcessor { }\n"
    index_files(connection, STORE, project_id=P, repo="bank/core",
                files=[{"path": path, "language": "java", "source": source}])
    assert ("LateProcessor", "DueFirstProcessor", "extends") in _inheritance(connection)
    index_files(connection, STORE, project_id=P, repo="bank/core",
                files=[{"path": path, "language": "java", "source": "package org.bank.loan;\npublic class LateProcessor { }\n"}])
    assert ("LateProcessor", "DueFirstProcessor", "extends") not in _inheritance(connection)
    closed = connection.execute(text(
        "SELECT count(*) FROM kg_edges e WHERE e.project_id = :p AND e.kind = 'extends' AND e.valid_to IS NOT NULL "
        "AND e.src_id = (SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = :k)"),
        {"p": P, "k": symbol_key("bank/core", path, "LateProcessor")}).scalar()
    assert closed == 1


class _Ctx:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        from contextlib import nullcontext
        return nullcontext(self.connection)


def test_the_agent_tools_offer_the_schema_and_say_how_to_read_a_result(connection, tree):
    from boss_engine.services.agent_baseline import KNOWLEDGE_TOOLS
    from boss_engine.tools.builtin import build_default_registry

    index_repository(connection, STORE, project_id=P, root=tree, repo="bank/core")
    reg = build_default_registry()
    assert "kg_schema" in KNOWLEDGE_TOOLS and reg.get("kg_schema").kind == "read"
    out = reg.get("kg_schema").handler(_Ctx(connection), project_id=P, actor="agent", args={})
    assert out["relationships"]["implements"]["count"] == 4, out
    described = reg.get("kg_cypher").description
    assert "kg_schema" in described and "truncated" in described and "relationships" in described
