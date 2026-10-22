"""Stage knowledge scopes are enforced where data is read (BOSSAPP-622; PR #14's PHASE3 "Next" 5).

Breaks covered: a triage pack or tool that reads the code layer; an RCA read that walks the whole
code graph instead of the incident's seeds, or past its hop and node bounds; a review read wider
than the diff and one hop; a declared step policy with a key nobody reads; the generated layer
column drifting from scopes.LAYER_KINDS; a non-code search whose cost grows with the code graph.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import scopes
from boss_engine.services.knowledge.code_index import index_files, symbol_key
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
ROOT = Path(__file__).resolve().parents[2]
# A chain of calls: s0 <- s1 <- s2 <- s3 <- s4, every name carrying the word "ledger".
CHAIN = "".join(
    f"def ledger_step{i}():\n    return {'ledger_step' + str(i - 1) + '()' if i else '1'}\n\n" for i in range(5)
)


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_sc', 'SC')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_sc', 'org_sc', 'SC')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_sc', 'ws_sc', 'SC', 'sc')"))
            index_files(conn, STORE, project_id="proj_sc",
                        files=[{"path": "ledger.py", "language": "python", "source": CHAIN}])
            for kind, key, body in (
                ("incident", "incident:inc-1", "ledger totals drifted after the release"),
                ("fact", "fact:ledger|rounds|half-even", "ledger rounds half-even"),
                ("decision", "decision:dec-1", "keep the ledger append-only"),
            ):
                STORE.upsert_node(conn, project_id="proj_sc", kind=kind, node_key=key, name=key,
                                  body=body, trust_tier="observed", actor_kind="system")
            yield conn
        finally:
            transaction.rollback()
            clear_llm_context()


def _sym(connection, i: int) -> str:
    return STORE.find_by_key(connection, project_id="proj_sc", kind="symbol",
                             node_key=symbol_key(None, "ledger.py", f"ledger_step{i}"))["id"]


def _layers(connection, ids) -> list[str]:
    return [connection.execute(text("SELECT knowledge_layer FROM kg_nodes WHERE id = :i"), {"i": i}).scalar_one()
            for i in ids]


def test_a_triage_pack_has_no_code_and_the_code_tools_refuse(connection):
    triage = scopes.policy_for(stage="triage")
    pack = SERVICE.pack(connection, project_id="proj_sc", role="sre", stage="triage", query="ledger",
                        budget_tokens=2000, policy=triage)
    assert pack["fact_ids"] and "code" not in _layers(connection, pack["fact_ids"])
    unscoped = SERVICE.search(connection, project_id="proj_sc", query="ledger")["hits"]
    assert "code" in _layers(connection, [h["node_id"] for h in unscoped])      # the code is there
    with pytest.raises(PermissionError, match="Code isn't available while triaging; it opens in Investigate"):
        SERVICE.blast(connection, project_id="proj_sc", node_id=_sym(connection, 2), policy=triage)

    from contextlib import contextmanager

    from boss_engine.tools.builtin import build_default_registry

    class _Ctx:
        @contextmanager
        def begin(self):
            yield connection

    set_llm_context(stage="triage")
    tool = build_default_registry().get("kg_symbol")
    refused = tool.handler(_Ctx(), project_id="proj_sc", actor="agent", args={"name": "ledger_step2"})
    assert refused == {"error": "Code isn't available while triaging; it opens in Investigate."}
    assert not scopes.offers_tool(scopes.current_policy(), "kg_blast_radius")
    assert scopes.offers_tool(scopes.current_policy(), "kg_search")


def test_an_rca_read_is_seeded_and_bounded(connection):
    rca = scopes.policy_for(stage="investigate").with_seeds([_sym(connection, 0)])     # max_hops 2
    hits = SERVICE.search(connection, project_id="proj_sc", query="ledger", policy=rca)["hits"]
    code = {h["node_key"] for h in hits if h["node_key"].startswith("symbol:")}
    assert code == {symbol_key(None, "ledger.py", f"ledger_step{i}") for i in (0, 1, 2)}
    assert {"incident:inc-1", "fact:ledger|rounds|half-even"} <= {h["node_key"] for h in hits}
    narrow = rca.__class__(**{**rca.__dict__, "max_nodes": 2})
    assert len(STORE.code_reach(connection, project_id="proj_sc", policy=narrow)) == 2
    unseeded = scopes.policy_for(stage="investigate")
    assert not [h for h in SERVICE.search(connection, project_id="proj_sc", query="ledger",
                                          policy=unseeded)["hits"] if h["node_key"].startswith("symbol:")]
    with pytest.raises(PermissionError):                                            # outside the reach
        SERVICE.blast(connection, project_id="proj_sc", node_id=_sym(connection, 4), policy=rca)


def test_a_review_read_is_the_diff_and_one_hop(connection):
    review = scopes.policy_for(stage="review_code").with_seeds([_sym(connection, 2)])
    assert review.stage == "review" and review.max_hops == 1
    reach = set(STORE.code_reach(connection, project_id="proj_sc", policy=review))
    assert reach == {_sym(connection, i) for i in (1, 2, 3)}
    blast = SERVICE.blast(connection, project_id="proj_sc", node_id=_sym(connection, 2), policy=review)
    assert {item["node_id"] for item in blast["affected"]} == {_sym(connection, 1), _sym(connection, 3)}


def test_a_decide_read_is_lifecycle_knowledge_and_ops_only(connection):
    decide = scopes.policy_for(stage="decide")
    found = SERVICE.precedent(connection, project_id="proj_sc", query="ledger", policy=decide)
    assert found["cited_fact_ids"] and "code" not in _layers(connection, found["cited_fact_ids"])


def test_a_step_declares_its_scope_and_an_unknown_key_is_refused():
    step = {"kind": "investigate", "knowledge": {"max_hops": 1, "max_nodes": 50, "budget_tokens": 600}}
    declared = scopes.policy_for(step=step)
    assert (declared.stage, declared.max_hops, declared.max_nodes, declared.budget_tokens) == (
        "investigate", 1, 50, 600)
    no_code = scopes.policy_for(step={"kind": "implement", "knowledge": {"code_seed": "none"}})
    assert not no_code.reads_code and "code" not in no_code.layers
    with pytest.raises(ValueError, match="unknown knowledge keys"):
        scopes.policy_for(step={"kind": "triage", "knowledge": {"layer": ["code"]}})
    assert scopes.policy_for(stage="converse", role="sre") is None          # a chat turn reads unscoped
    assert scopes.policy_for(role="sre").stage == "investigate"            # the role is only a fallback


def test_the_generated_layer_column_matches_the_layer_kinds():
    schema = (ROOT / "db" / "schema.sql").read_text()
    assert scopes.layer_sql_case("kind") in schema
    for layer, kinds in scopes.LAYER_KINDS.items():
        for kind in kinds:
            assert scopes.layer_of(kind) == layer
    assert scopes.layer_of("something_new") == "knowledge"


def _buffers(connection, sql: str, params: dict) -> int:
    """Shared pages the whole plan touched (the root node's Buffers line)."""
    plan = connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql), params).scalars().all()
    line = next((row for row in plan if "Buffers: shared" in row), "")
    return sum(int(n) for n in re.findall(r"(?:hit|read)=(\d+)", line))


def test_a_non_code_search_does_not_read_the_code_rows(connection):
    """The ops-latency-vs-code-size check, by buffers rather than milliseconds: the same triage
    search reads the same pages with 0 and with 20,000 code nodes of the same word."""
    triage = scopes.policy_for(stage="triage")
    scope_sql, scope_params = STORE._scope_sql(connection, "proj_sc", triage)
    sql = ("SELECT id FROM kg_nodes WHERE project_id = :p AND search_tsv @@ plainto_tsquery('english', 'ledger') "
           "AND (valid_to IS NULL OR valid_to > now())" + scope_sql)
    params = {"p": "proj_sc", **scope_params}
    # The tables are tiny here, so the planner would scan them whole either way; at size it picks
    # the index. Disable the sequential scan to compare what the index reads.
    connection.execute(text("SET LOCAL enable_seqscan = off"))
    before = _buffers(connection, sql, params)
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, body, search_tsv) "
        "SELECT 'kgn_bulk_' || g, 'proj_sc', 'symbol', 'symbol:bulk.py:ledger_' || g, 'ledger_' || g, "
        "'python ledger', to_tsvector('english', 'ledger ledger') FROM generate_series(1, 20000) g"))
    connection.execute(text("ANALYZE kg_nodes"))
    after = _buffers(connection, sql, params)
    assert after <= before + 2, (before, after)
    found = connection.execute(text(sql), params).scalars().all()
    assert found and not any(i.startswith("kgn_bulk_") for i in found)
