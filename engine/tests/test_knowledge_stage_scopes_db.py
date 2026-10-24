"""Stage knowledge scopes are enforced where data is read (BOSSAPP-622; PR #14's PHASE3 "Next" 5).

Breaks covered: a triage pack or tool that reads the code layer; a stage that cannot read
approved rules, or a declaration that cannot refuse them; the product layer drifting between the
engine and the console; an RCA read that walks the whole
code graph instead of the incident's seeds, or past its hop and node bounds; a review read wider
than the diff and one hop; a declared step policy with a key nobody reads; the generated layer
column drifting from scopes.LAYER_KINDS; a non-code search whose cost grows with the code graph.
"""
from __future__ import annotations

import json
import os
import re
import uuid
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


def test_the_live_column_is_the_layer_kinds_for_every_kind(connection):
    """The database's generated column, not only the text: every node kind the store accepts lands
    in the layer scopes.layer_of names (the KGR block rebuilt the column for `rules`)."""
    from boss_engine.services.knowledge.store import NODE_KINDS

    for kind in sorted(NODE_KINDS):
        node = STORE.upsert_node(connection, project_id="proj_sc", kind=kind, node_key=f"{kind}:layer-probe",
                                 name=kind, body="", trust_tier="observed", actor_kind="system")
        assert _layers(connection, [node]) == [scopes.layer_of(kind)], kind
    assert scopes.layer_of("rule") == "rules"


def test_every_default_stage_reads_approved_rules_and_a_declaration_can_refuse_them():
    for name, policy in scopes.DEFAULTS.items():
        assert "rules" in policy.layers, name
        assert scopes.offers_tool(policy, "kg_rules") and scopes.offers_tool(policy, "kg_propose_rule")
    assert "rules" in scopes.policy_for(step={"kind": "something_custom", "knowledge": {"budget_tokens": 10}}).layers
    refused = scopes.policy_for(step={"kind": "triage", "knowledge": {"layers": ["ops", "lifecycle"]}})
    assert not scopes.offers_tool(refused, "kg_rules")


def test_the_product_layer_is_one_mapping_on_both_sides():
    """scopes.product_layer (engine) and productLayerOf (apps/web/lib/kg-view-types.ts) agree on
    every kind either side names; the SQL CASE says the same."""
    from boss_engine.services.knowledge.store import NODE_KINDS

    ts = (ROOT / "apps" / "web" / "lib" / "kg-view-types.ts").read_text()

    def ts_list(name: str) -> set[str]:
        block = re.search(rf"{name}\s*=\s*(?:new Set\()?\[([^\]]*)\]", ts, re.S)
        assert block, name
        return set(re.findall(r'"([a-z_]+)"', block.group(1)))

    assert ts_list("KG_LEARNING_KINDS") == scopes.LEARNED_KINDS
    assert ts_list("KG_ANCHOR_KINDS") == scopes.ANCHOR_KINDS
    assert ts_list("CODE_TOPOLOGY") == scopes.CODE_TOPOLOGY_KINDS
    assert ts_list("CODE_STORAGE") == scopes.LAYER_KINDS["code"]
    assert ts_list("DOMAIN") == scopes.DOMAIN_KINDS
    assert scopes.product_layer("rule") == "rules" and scopes.product_layer("symbol") == "code"
    assert scopes.product_layer("service") == "code" and scopes.product_layer("run") == "learning"
    assert scopes.product_layer("person") is None
    case = scopes.product_layer_sql_case("k")
    for kind in sorted(NODE_KINDS):
        layer = scopes.product_layer(kind)
        assert (f"'{kind}'" in case) == (layer is not None), kind


def _rows_read(connection, sql: str, params: dict) -> int:
    """Rows the plan read from kg_nodes and its indexes: what each table or index scan returned,
    plus what a scan read and threw away (filter, recheck). Unlike pages or a plan's shape, this
    does not move with the planner's choice between equally narrow indexes, nor with what other
    tests left in a shared database; an index walk over the code rows (the full-text index
    without the non-code predicate) counts every code row it returns."""
    indexes = set(connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'kg_nodes'")).scalars().all())
    plan = connection.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql), params).scalar_one()
    plan = json.loads(plan) if isinstance(plan, str) else plan
    total = 0
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop()
        stack.extend(node.get("Plans") or [])
        if node.get("Relation Name") == "kg_nodes" or node.get("Index Name") in indexes:
            total += int(node.get("Actual Rows", 0)) * int(node.get("Actual Loops", 1) or 1)
            total += int(node.get("Rows Removed by Filter", 0)) + int(node.get("Rows Removed by Index Recheck", 0))
    return total


def test_a_non_code_search_does_not_read_the_code_rows(connection):
    """The ops-latency-vs-code-size check, by rows read rather than milliseconds: with 20,000 code
    nodes of the same word added, the triage search reads the rows it read before, while the same
    search without the scope reads the code rows.

    Deterministic in any order, in a shared database: the word is this run's own (no other test's
    rows, committed or dead, match it), and what is measured is the rows the plan read, which the
    planner's choice of index does not change. A plan that reads the code rows to filter them out
    is counted, so the regression this guards still fails it."""
    word = f"ledg{uuid.uuid4().hex[:10]}"
    STORE.upsert_node(connection, project_id="proj_sc", kind="fact", node_key=f"fact:{word}", name=word,
                      body=f"{word} rounds half-even", trust_tier="observed", actor_kind="system")
    triage = scopes.policy_for(stage="triage")
    scope_sql, scope_params = STORE._scope_sql(connection, "proj_sc", triage)
    sql = ("SELECT id FROM kg_nodes WHERE project_id = :p AND search_tsv @@ plainto_tsquery('english', :w) "
           "AND (valid_to IS NULL OR valid_to > now())" + scope_sql)
    params = {"p": "proj_sc", "w": word, **scope_params}
    # The tables are tiny here, so the planner would scan them whole either way; at size it picks
    # an index. Disable the sequential scan, and plan both on fresh statistics.
    connection.execute(text("SET LOCAL enable_seqscan = off"))
    connection.execute(text("ANALYZE kg_nodes"))
    before = _rows_read(connection, sql, params)
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, body, search_tsv) "
        "SELECT 'kgn_bulk_' || g, 'proj_sc', 'symbol', 'symbol:bulk.py:ledger_' || g, 'ledger_' || g, "
        "'python ' || :w, to_tsvector('english', :w || ' ' || :w) FROM generate_series(1, 20000) g"), {"w": word})
    connection.execute(text("ANALYZE kg_nodes"))
    after = _rows_read(connection, sql, params)
    unscoped = _rows_read(connection, sql.replace(scope_sql, ""), {"p": "proj_sc", "w": word})
    assert 1 <= before and after <= 2 * before + 2, (before, after)
    assert unscoped >= 20000, (after, unscoped)
    found = connection.execute(text(sql), params).scalars().all()
    assert found and not any(i.startswith("kgn_bulk_") for i in found)
