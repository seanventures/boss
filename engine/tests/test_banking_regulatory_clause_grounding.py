"""KG P7d: the lane and the remediation advisor must be judged against the clause's own text,
in the code that computes it — `_clause_text_and_citation` (banking_regulatory_pipeline.py).

Evidence from wfr_ba338535a0924ebe9772 (laptop, project northpeak-lending):
1. The workflow's `subject_id` is a business rule (kgn_47932f4f45414476bd14), not a clause.
   `_clause_text_and_citation` must still resolve the clause reachable from that subject — via
   the `implemented_by` edge compliance_assessment writes (clause -> cited rule/code) and/or the
   compliance_case clause_intake opened for the clause's document — and return its real text and
   citation, never "the clause text itself is not provided".
2. INC-2088's `affected_components` must name the Fineract transaction-processor code where the
   misallocation is actually computed (AbstractLoanRepaymentScheduleTransactionProcessor /
   AcmeLoanRepaymentScheduleTransactionProcessor), not the plain JPA entity
   `LoanPaymentAllocationRule`.
3. The remediation package's `test_ref` must be a Java test path in the repo's own layout, not
   a Python one, for a Java repository.

These run against a real, disposable postgres (DATABASE_URL), building the graph with the real
NorthPeak shapes: `documents.add_document` for the clause, `code_index.symbol_key` for the
Fineract transaction-processor symbol, and `banking_regulatory_pipeline`'s own capability
functions for the case/edge that connect them — not invented fixtures.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import banking_regulatory_pipeline as pipeline
from boss_engine.services.knowledge import documents
from boss_engine.services.knowledge import ontology_packs
from boss_engine.services.knowledge.code_index import symbol_key
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from tools.mcp_itsm_mock import INCIDENTS

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
        org = f"org_p7d_{uuid.uuid4().hex[:8]}"
        ws = f"ws_p7d_{uuid.uuid4().hex[:8]}"
        project = f"p_p7d_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'P7d')"), {"i": org})
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'P7d')"),
                {"i": ws, "o": org})
            conn.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "(:i, :w, 'P7d', :s)"), {"i": project, "w": ws, "s": project})
            ontology_packs.set_project_pack(
                conn, project_id=project, pack_id="banking-regulatory")
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _add_document(connection, project):
    return documents.add_document(
        connection, project_id=project, title="Reg Z repayment order",
        source_url="https://example.test/regz", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(1)",
                  "text": "A periodic payment must be credited as of the date of receipt, "
                          "applied interest-first then to principal, before any later-dated "
                          "payment is considered.",
                  "citation": "12 CFR 1026.36(c)(1)"}],
        actor_kind="person", actor_ref="test")


def _rule_node(connection, project):
    return STORE.upsert_node(
        connection, project_id=project, kind="rule",
        node_key="rule:p7d:repayment-order", name="Repayment-like transaction classification",
        body="Classifies incoming transactions as repayments for allocation ordering.",
        trust_tier="governed", actor_kind="person", actor_ref="test", attrs={})


# ── P0: the lane is given the clause text and citation reachable from the rule subject ───────

def test_p0_clause_text_and_citation_resolves_from_the_rule_subject_via_implemented_by(connection):
    c, project = connection, connection.project_id
    doc = _add_document(c, project)
    clause_id = doc["clause_ids"][0]
    rule_id = _rule_node(c, project)

    # compliance_assessment's real write path: clause -(implemented_by)-> cited rule/code.
    from boss_engine.services.knowledge import domain
    domain.propose_implementation(
        c, project_id=project, clause_id=clause_id, target_id=rule_id,
        rationale="cited", confidence=0.9, actor_ref="test")

    clause_text, citation = pipeline._clause_text_and_citation(c, project, rule_id)

    assert "interest-first" in clause_text
    assert citation == "12 CFR 1026.36(c)(1)"


# ── P1: INC-2088's affected_components name the processor code, not the JPA entity ───────────

def test_p1_inc_2088_affected_components_name_the_transaction_processor():
    components = INCIDENTS["INC-2088"]["affected_components"]
    assert "LoanPaymentAllocationRule" not in components
    assert any(
        "TransactionProcessor" in component for component in components
    ), f"expected a *TransactionProcessor component, got {components!r}"


# ── P1: the advisor is shown the repo's own test layout; nothing is invented without it ──────

def test_p1_the_advisor_is_shown_a_real_test_near_the_code_and_invents_none(connection, monkeypatch):
    c, project = connection, connection.project_id
    doc = _add_document(c, project)
    clause_id = doc["clause_ids"][0]
    rule_id = _rule_node(c, project)
    src = "fineract-loan/src/main/java/org/x/AcmeLoanRepaymentScheduleTransactionProcessor.java"
    sym = STORE.upsert_node(
        c, project_id=project, kind="symbol",
        node_key=symbol_key("acme/lending", src, "AcmeLoanRepaymentScheduleTransactionProcessor"),
        name="AcmeLoanRepaymentScheduleTransactionProcessor", body="", trust_tier="observed",
        actor_kind="system", actor_ref="code_index", attrs={"line": 1, "end_line": 40})
    STORE.upsert_edge(c, project_id=project, src_id=rule_id, dst_id=sym, kind="evidenced_by",
                      actor_kind="person", actor_ref="test")
    test_file = "fineract-loan/src/test/java/org/x/LoanTransactionProcessorTest.java"
    STORE.upsert_node(c, project_id=project, kind="file",
                      node_key=f"file:acme/lending:{test_file}", name=test_file, body="",
                      trust_tier="observed", actor_kind="system", actor_ref="code_index")
    from boss_engine.services.knowledge import domain
    domain.propose_implementation(
        c, project_id=project, clause_id=clause_id, target_id=rule_id,
        rationale="cited", confidence=0.9, actor_ref="test")
    prompts = []
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (object(), "scripted"))
    monkeypatch.setattr(pipeline, "_ask_role", lambda agent, prompt: prompts.append(prompt) or None)

    step = {"id": "remediation_package", "run_id": "run_p7d"}
    context = {"steps": {"compliance_assessment": {"citation": rule_id, "note": "gap: no amount check"},
                         "compliance_orchestration": {"finding_families": ["payment_allocation"]}}}
    result = pipeline.run_remediation_package(
        c, settings=object(), project_id=project, step=step, context=context, inputs={})

    assert "REPOSITORY LANGUAGE: .java" in prompts[0] and test_file in prompts[0]
    assert "interest-first" in prompts[0]          # the clause reached the advisor
    node = STORE.get_node(c, project_id=project, node_id=result["remediation_package_id"])
    assert (node.get("attrs") or {}).get("test_ref", "") == ""   # never an invented test


# ── P1: the lane confirmation's reason is grounded in the clause's own text, not just code ───

def test_p1_lane_confirmation_reads_the_grounded_symbol_key_from_code_index(connection):
    c, project = connection, connection.project_id
    repo = "northpeak-lending"
    path = ("custom/acme/src/main/java/com/acme/loan/domain/transactionprocessor/"
           "AcmeLoanRepaymentScheduleTransactionProcessor.java")
    qualname = "AcmeLoanRepaymentScheduleTransactionProcessor.handleTransaction"
    expected_key = symbol_key(repo, path, qualname)

    symbol_id = STORE.upsert_node(
        c, project_id=project, kind="symbol", node_key=expected_key,
        name=qualname, body="Allocates a repayment across interest then principal.",
        trust_tier="observed", actor_kind="agent", actor_ref="test",
        attrs={"path": path, "qualname": qualname})

    doc = _add_document(c, project)
    clause_id = doc["clause_ids"][0]
    from boss_engine.services.knowledge import domain
    domain.propose_implementation(
        c, project_id=project, clause_id=clause_id, target_id=symbol_id,
        rationale="cited", confidence=0.9, actor_ref="test")

    clause_text, citation = pipeline._clause_text_and_citation(c, project, symbol_id)
    assert "interest-first" in clause_text
    assert citation == "12 CFR 1026.36(c)(1)"

    node = STORE.get_node(c, project_id=project, node_id=symbol_id)
    assert node["node_key"] == expected_key


if __name__ == "__main__":
    import unittest
    unittest.main()
