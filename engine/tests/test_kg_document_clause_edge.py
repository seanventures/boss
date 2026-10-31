"""P7a — a regulation document links each clause to its document with a real edge.

The console Graph view (criterion 2) reads the document→clause relationship as a
``kg_edges`` row, not merely the clause's ``document_id`` attribute. These tests run in
the engine suite (they import the changed ``documents`` service), so a one-line mutation
that drops the edge, changes its kind, or stops storing the clause text verbatim is caught
where boss.verify.yaml can actually run them (the apps/api suite cannot).
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import ontology_packs
from boss_engine.services.knowledge.documents import add_document

P = "proj_doc_clause_edge"


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
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_doc_edge', 'Doc Edge')"))
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_doc_edge', 'org_doc_edge', 'Doc Edge')"))
            conn.execute(text(
                f"INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                f"('{P}', 'ws_doc_edge', 'Doc Edge', 'doc-edge')"))
            yield conn
        finally:
            transaction.rollback()


def test_add_document_creates_a_contains_edge_from_document_to_each_clause(connection):
    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    result = add_document(
        connection, project_id=P, title="Reg Z",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026",
        clauses=[
            {"section": "1026.36(c)(1)(i)", "text": "A loan originator shall not."},
            {"section": "1026.36(c)(2)", "text": "A servicer must handle prepayments."},
        ],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )
    document_id = result["document_id"]
    clause_ids = result["clause_ids"]
    assert len(clause_ids) == 2

    for clause_id in clause_ids:
        kind = connection.execute(
            text("SELECT kind FROM kg_edges WHERE src_id = :src AND dst_id = :dst "
                 "AND valid_to IS NULL"),
            {"src": document_id, "dst": clause_id},
        ).scalar_one_or_none()
        assert kind == "contains", (
            "add_document must link the document to each clause with a 'contains' edge, "
            f"got {kind!r} for clause {clause_id!r}")


def test_add_document_stores_the_clause_text_verbatim(connection):
    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    # Leading/trailing whitespace inside the pasted clause is kept exactly; only emptiness is checked.
    verbatim = "  A periodic payment\tmust be credited.  "
    result = add_document(
        connection, project_id=P, title="Reg Z verbatim",
        source_url="https://www.ecfr.gov/x/1026.37", citation="12 CFR 1026",
        clauses=[{"section": "1026.36(c)(1)(i)", "text": verbatim}],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )
    clause_id = result["clause_ids"][0]
    row = connection.execute(
        text("SELECT body, attrs->>'text' AS attr_text FROM kg_nodes WHERE id = :i"),
        {"i": clause_id},
    ).mappings().one()
    assert row["body"] == verbatim, "clause body must be the pasted text, unstripped"
    assert row["attr_text"] == verbatim, "clause attrs.text must be the pasted text, unstripped"
