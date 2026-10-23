"""knowledge_graph = keyword: full-text keyword search only, on every path that gives an agent knowledge.

The case study's A′ arm (scripts/case-study/knowledge, BOSSAPP-758). Breaks covered: a keyword pack or
kg_search that returns a learned fact, a code node reached by graph traversal, or a fused graph or vector
list; a precedent, citation or graph tool (callers, blast radius, why, Cypher, propose) still offered
under keyword; the off switch leaving the kg tools open; the on switch losing its full retrieval.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.code_index import index_files, symbol_key
from boss_engine.services.knowledge.runtime import quoted_pack_for_turn, remember_written_files
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
# A chain of calls: s0 <- s1 <- s2 <- s3 <- s4, every name carrying the word "ledger".
CHAIN = "".join(
    f"def ledger_step{i}():\n    return {'ledger_step' + str(i - 1) + '()' if i else '1'}\n\n" for i in range(5)
)
FACT_BODY = "ledger rounds half-even"


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kw', 'KW')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kw', 'org_kw', 'KW')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_kw', 'ws_kw', 'KW', 'kw')"))
            index_files(conn, STORE, project_id="proj_kw",
                        files=[{"path": "ledger.py", "language": "python", "source": CHAIN}])
            for kind, key, body, tier in (
                ("incident", "incident:inc-1", "ledger totals drifted after the release", "observed"),
                ("fact", "fact:ledger|rounds|half-even", FACT_BODY, "observed"),
                ("decision", "decision:dec-1", "keep the ledger append-only", "observed"),
            ):
                STORE.upsert_node(conn, project_id="proj_kw", kind=kind, node_key=key, name=key,
                                  body=body, trust_tier=tier, actor_kind="system")
            yield conn
        finally:
            transaction.rollback()
            clear_llm_context()


def _switch(connection, value: str) -> None:
    connection.execute(text(
        "INSERT INTO project_settings (project_id, key, value) VALUES ('proj_kw', 'knowledge_graph', :v) "
        "ON CONFLICT (project_id, key) DO UPDATE SET value = EXCLUDED.value"), {"v": f'"{value}"'})


def _sym(connection, i: int) -> str:
    return STORE.find_by_key(connection, project_id="proj_kw", kind="symbol",
                             node_key=symbol_key(None, "ledger.py", f"ledger_step{i}"))["id"]


def _layers(connection, ids) -> set[str]:
    return {connection.execute(text("SELECT knowledge_layer FROM kg_nodes WHERE id = :i"), {"i": i}).scalar_one()
            for i in ids}


class _Ctx:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def _tool(connection, tool_name: str, /, **args):
    from boss_engine.tools.builtin import build_default_registry

    return build_default_registry().get(tool_name).handler(_Ctx(connection), project_id="proj_kw", actor="agent", args=args)


def test_keyword_is_full_text_only_on_every_agent_path(connection):
    from boss_engine.services.workflow_capability_runner import _knowledge_precedent

    # The control: with the switch on, the pack carries the learned fact and precedent cites it.
    assert FACT_BODY in quoted_pack_for_turn(connection, project_id="proj_kw", query="ledger")
    assert FACT_BODY in str(_tool(connection, "kg_search", query="ledger"))
    assert _tool(connection, "kg_precedent", query="ledger")["cited_fact_ids"]

    _switch(connection, "keyword")
    # Injected pack: keyword hits, and never the learned fact.
    pack = quoted_pack_for_turn(connection, project_id="proj_kw", query="ledger")
    assert "ledger totals drifted" in pack and FACT_BODY not in pack
    # The MCP search: text only, no graph or vector list, no knowledge layer.
    found = _tool(connection, "kg_search", query="ledger")
    assert found["retrieval"] == {"mode": "keyword", "lists": {"text": len(found["hits"])}}
    assert found["hits"] and "knowledge" not in _layers(connection, [h["node_id"] for h in found["hits"]])
    # A seeded implement stage reads its seed and nothing the call graph reaches from it.
    set_llm_context(stage="implement", knowledge_seeds=[_sym(connection, 2)])
    seeded = _tool(connection, "kg_search", query="ledger")
    code = [h["node_id"] for h in seeded["hits"] if _layers(connection, [h["node_id"]]) == {"code"}]
    assert code == [_sym(connection, 2)]
    clear_llm_context()
    # Every graph, precedent or learning tool is refused in words; precedent and citations are empty.
    for tool, args in (("kg_precedent", {"query": "ledger"}), ("kg_callers", {"node_id": _sym(connection, 2)}),
                       ("kg_blast_radius", {"node_id": _sym(connection, 2)}), ("kg_why", {"node_id": _sym(connection, 2)}),
                       ("kg_symbol", {"name": "ledger_step2"}), ("kg_cypher", {"query": "MATCH (n) RETURN n"}),
                       ("kg_propose_fact", {"statement": "x", "subject": "a", "predicate": "b", "object": "c"})):
        assert _tool(connection, tool, **args) == {
            "error": "Only keyword search is on for this project (setting knowledge_graph = keyword): use kg_search."}, tool
    assert _knowledge_precedent(connection, "proj_kw", "ledger", "run-kw", "decide", "agent") == ""
    remembered = remember_written_files(
        connection, project_id="proj_kw", source_id="pr-kw", title="ledger", body="change",
        files=[SimpleNamespace(path="ledger.py", content="def ledger_step2():\n    return 1\n")])
    assert remembered["cited_fact_ids"] == [] and remembered["linked"]
    assert SERVICE.status(connection, project_id="proj_kw")["agent_access"] == "keyword"
    # A person's own search (the console) is not the agents' switch: it still finds everything.
    assert FACT_BODY in str(SERVICE.search(connection, project_id="proj_kw", query="ledger"))


def test_off_closes_the_kg_tools_too(connection):
    _switch(connection, "off")
    assert quoted_pack_for_turn(connection, project_id="proj_kw", query="ledger") == ""
    for tool in ("kg_search", "kg_precedent"):
        assert _tool(connection, tool, query="ledger") == {
            "error": "The knowledge graph is off for this project (setting knowledge_graph = off)."}
