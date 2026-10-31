"""KG P7b console fix: picking a rule in the "Check compliance" missing-subject recovery must
PERSIST a clause→rule link (`documents.link_rule`), not just pass the rule as that one run's
subject_id — a later check must find the clause linked. Reuses `domain.propose_implementation`
(the same edge `regulatory_trace` proposes), so `documents.linked_rules` finds it afterward.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import documents, ontology_packs
from boss_engine.services.knowledge.documents import DocumentRefused
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
        org = f"org_doclink_{uuid.uuid4().hex[:8]}"
        ws = f"ws_doclink_{uuid.uuid4().hex[:8]}"
        project = f"p_doclink_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'DocLink')"), {"i": org})
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'DocLink')"),
                {"i": ws, "o": org})
            conn.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "(:i, :w, 'DocLink', :s)"), {"i": project, "w": ws, "s": project})
            ontology_packs.set_project_pack(conn, project_id=project, pack_id="banking-regulatory")
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _clause(c, project_id: str) -> str:
    return STORE.upsert_node(
        c, project_id=project_id, kind="clause", node_key="clause:doclink:1",
        name="1026.36(c)(1)", body="A loan originator shall not.", trust_tier="observed",
        actor_kind="agent", actor_ref="test")


def _rule(c, project_id: str) -> str:
    return STORE.upsert_node(
        c, project_id=project_id, kind="rule", node_key="rule:doclink:repayment-order",
        name="Repayment-order rule", body="rule body", trust_tier="governed",
        actor_kind="person", actor_ref="test")


def test_link_rule_persists_an_implemented_by_edge_linked_rules_then_finds(connection):
    c, project = connection, connection.project_id
    clause_id = _clause(c, project)
    rule_id = _rule(c, project)

    result = documents.link_rule(
        c, project_id=project, clause_id=clause_id, rule_id=rule_id, actor="ceo")
    assert result["kind"] == "implemented_by"

    found = documents.linked_rules(c, project_id=project, clause_id=clause_id)
    assert any(r["node_id"] == rule_id for r in found), (
        "link_rule must write an edge documents.linked_rules can find afterward")


def test_link_rule_rerun_does_not_duplicate_the_edge(connection):
    c, project = connection, connection.project_id
    clause_id = _clause(c, project)
    rule_id = _rule(c, project)

    documents.link_rule(c, project_id=project, clause_id=clause_id, rule_id=rule_id, actor="ceo")
    documents.link_rule(c, project_id=project, clause_id=clause_id, rule_id=rule_id, actor="ceo")

    count = c.execute(text(
        "SELECT count(*) FROM kg_edges WHERE project_id=:p AND src_id=:s AND dst_id=:d "
        "AND kind='implemented_by'"), {"p": project, "s": clause_id, "d": rule_id}).scalar()
    assert count == 1


def test_link_rule_refuses_an_unknown_clause_or_rule(connection):
    c, project = connection, connection.project_id
    rule_id = _rule(c, project)
    with pytest.raises(DocumentRefused):
        documents.link_rule(c, project_id=project, clause_id="kgn_missing", rule_id=rule_id,
                            actor="ceo")

    clause_id = _clause(c, project)
    with pytest.raises(DocumentRefused):
        documents.link_rule(c, project_id=project, clause_id=clause_id, rule_id="kgn_missing",
                            actor="ceo")
