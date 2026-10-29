"""KG P5a: the two deterministic graph steps (design: ~/boss-ops design §4.4).

`boss_engine.services.knowledge.graph_steps` runs a FIXED knowledge-graph query per step --
never a model call -- and writes its outputs through the existing Context Graph primitives
(context_graph.write_agent_decision / write_coverage_gap, already shipped in Build B).

- Context Enrichment: within a hop cap, collect what is connected to a subject node; what the
  cap couldn't reach becomes a CoverageGap, never a silent omission.
- Historical Pattern: find prior incidents/precedents that share the subject's links, and link
  them with `correlates_with` (an existing core edge kind -- no new ontology grant needed) so a
  later run's Historical Pattern step can find what an earlier run's Closure wrote (the learning
  loop the design calls for).

Both run at $0: no governed agent, no model spend. Runs against a real, disposable postgres
(DATABASE_URL), like test_regulatory_trace.py.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import graph_steps
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL must be set to a reachable postgres for these tests")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        org = f"org_kggraph_{uuid.uuid4().hex[:8]}"
        ws = f"ws_kggraph_{uuid.uuid4().hex[:8]}"
        project = f"p_kggraph_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'KG Graph Steps')"), {"i": org})
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'KG Graph Steps')"),
                {"i": ws, "o": org})
            conn.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "(:i, :w, 'KG Graph Steps', :s)"), {"i": project, "w": ws, "s": project})
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _rule(connection, name: str) -> str:
    return STORE.upsert_node(
        connection, project_id=connection.project_id, kind="rule", node_key=f"rule:{name}",
        name=name, body=f"{name} body", trust_tier="governed", actor_kind="person",
        actor_ref="qa@example.com")


def _symbol(connection, name: str) -> str:
    return STORE.upsert_node(
        connection, project_id=connection.project_id, kind="symbol", node_key=f"symbol:{name}",
        name=name, body="", trust_tier="observed", actor_kind="agent", actor_ref="indexer")


def _incident(connection, name: str) -> str:
    return STORE.upsert_node(
        connection, project_id=connection.project_id, kind="incident", node_key=f"incident:{name}",
        name=name, body=f"{name} body", trust_tier="observed", actor_kind="agent",
        actor_ref="ingest")


def _link(connection, src: str, dst: str) -> None:
    STORE.upsert_edge(connection, project_id=connection.project_id, src_id=src, dst_id=dst,
                       kind="about", actor_kind="agent", actor_ref="qa@example.com")


# ── P1: Context Enrichment collects within the hop cap and reports what it couldn't reach ─────

def test_p1_context_enrichment_writes_an_assembled_decision_and_a_gap_for_what_the_hop_cap_missed(
        connection):
    subject = _rule(connection, "subject-rule")
    within_cap = _symbol(connection, "within-cap-symbol")
    beyond_cap = _incident(connection, "beyond-cap-incident")
    _link(connection, subject, within_cap)
    _link(connection, within_cap, beyond_cap)  # 2 hops from subject

    outcome = graph_steps.run_context_enrichment(
        connection, project_id=connection.project_id, run_id="wfr_ctx_1", step_id="context_enrichment",
        subject_id=subject, max_hops=1, expected_kinds=("symbol", "incident"))

    assert outcome["cost_usd_micros"] == 0, "a deterministic step must never spend"
    decision = connection.execute(
        text("SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'agent_decision' "
             "AND attrs->>'run_id' = 'wfr_ctx_1'"),
        {"p": connection.project_id}).mappings().first()
    assert decision is not None, "Context Enrichment must write an AgentDecision"
    assert decision["attrs"]["decision_type"] == "ContextAssembled"

    gap = connection.execute(
        text("SELECT body FROM kg_nodes WHERE project_id = :p AND kind = 'coverage_gap' "
             "AND attrs->>'run_id' = 'wfr_ctx_1'"),
        {"p": connection.project_id}).mappings().first()
    assert gap is not None, "the incident beyond the 1-hop cap must be reported as a CoverageGap"


# ── P1: Historical Pattern links the subject to a matching precedent ──────────────────────────

def test_p1_historical_pattern_links_the_subject_to_a_sibling_incident(connection):
    subject = _rule(connection, "pattern-subject-rule")
    shared_symbol = _symbol(connection, "pattern-shared-symbol")
    sibling_incident = _incident(connection, "pattern-sibling-incident")
    _link(connection, subject, shared_symbol)
    _link(connection, sibling_incident, shared_symbol)

    outcome = graph_steps.run_historical_pattern(
        connection, project_id=connection.project_id, run_id="wfr_hist_1",
        step_id="historical_pattern", subject_id=subject)

    assert outcome["cost_usd_micros"] == 0, "a deterministic step must never spend"
    edge = connection.execute(
        text("SELECT src_id, dst_id FROM kg_edges WHERE project_id = :p AND kind = 'correlates_with' "
             "AND valid_to IS NULL AND (src_id = :sib OR dst_id = :sib)"),
        {"p": connection.project_id, "sib": sibling_incident}).mappings().first()
    assert edge is not None, "a sibling incident sharing the subject's link must be correlated"
    assert subject in (edge["src_id"], edge["dst_id"])


# ── P1: the learning loop — a second run's Historical Pattern finds what Closure wrote ────────

def test_p1_a_later_runs_historical_pattern_finds_a_precedent_an_earlier_closure_wrote(connection):
    earlier_subject = _rule(connection, "closure-earlier-rule")
    written_precedent = STORE.upsert_node(
        connection, project_id=connection.project_id, kind="precedent",
        node_key="precedent:closure-written", name="closure-written",
        body="resolved by closure", trust_tier="inferred", actor_kind="agent",
        actor_ref="closure")
    shared_symbol = _symbol(connection, "closure-shared-symbol")
    _link(connection, earlier_subject, shared_symbol)
    _link(connection, written_precedent, shared_symbol)

    new_subject = _rule(connection, "closure-later-rule")
    _link(connection, new_subject, shared_symbol)

    outcome = graph_steps.run_historical_pattern(
        connection, project_id=connection.project_id, run_id="wfr_hist_2",
        step_id="historical_pattern", subject_id=new_subject)

    assert outcome["cost_usd_micros"] == 0
    edge = connection.execute(
        text("SELECT src_id, dst_id FROM kg_edges WHERE project_id = :p AND kind = 'correlates_with' "
             "AND valid_to IS NULL AND (src_id = :prec OR dst_id = :prec)"),
        {"p": connection.project_id, "prec": written_precedent}).mappings().first()
    assert edge is not None, "the second run must find the precedent the first run's Closure wrote"
    assert new_subject in (edge["src_id"], edge["dst_id"])
