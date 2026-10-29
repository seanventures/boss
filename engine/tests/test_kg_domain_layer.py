"""KG Domain layer v1 & ontology packs (A2) — critical-path tests, written before implementation.

Covers (see task PRD/spec resolutions):
- store.upsert_node enforces core ∪ active-pack kinds, refusing a pack-only kind with no pack set
  (SPEC resolution #2 "mid-rollout"; acceptance criteria #1).
- the shipped ontology packs load and parse without colliding with core kinds (criterion #4).
- add_document writes one document node and N clause nodes, requiring no approval (criterion #5).
- regulatory_trace proposals are never auto-approved; a clause with no match becomes a gap, one
  with a match becomes an inferred implemented_by edge pending review (criterion #7).
- AG-1 closure: propose_action("trace_regulation") / ("regulatory_trace") resolve to a
  start_workflow offer rather than a dead-end refusal (criterion #11a), mirroring
  test_a_proposed_workflow_name_becomes_start_workflow.py's assertion style.

None of the modules under test exist yet; these tests are expected to fail/error until the
implementation lands.
"""
from __future__ import annotations

import os
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
P = "proj_domain_a2"


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_domain_a2', 'Domain A2')"))
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_domain_a2', 'org_domain_a2', 'Domain A2')"))
            conn.execute(text(
                f"INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                f"('{P}', 'ws_domain_a2', 'Domain A2', 'domain-a2')"))
            yield conn
        finally:
            transaction.rollback()


# --------------------------------------------------------------------------- #
# P0 — a pack-only kind is refused (never a 500) until a pack grants it
# --------------------------------------------------------------------------- #
def test_p0_upsert_node_refuses_a_pack_only_kind_with_no_active_pack(connection):
    with pytest.raises(ValueError) as excinfo:
        STORE.upsert_node(
            connection, project_id=P, kind="clause", node_key="clause:none:1",
            name="clause", body="text", trust_tier="fact", actor_kind="agent",
        )
    message = str(excinfo.value).lower()
    assert "clause" in message, f"the refusal must name the refused kind: {excinfo.value}"
    assert "pack" in message, f"the refusal must say no pack grants this kind: {excinfo.value}"


# --------------------------------------------------------------------------- #
# P1 — the shipped ontology packs parse and don't collide with core kinds
# --------------------------------------------------------------------------- #
def test_p1_shipped_ontology_packs_load_without_colliding_with_core_kinds():
    from boss_engine.services.knowledge import ontology_packs
    from boss_engine.services.knowledge.store import NODE_KINDS

    packs = ontology_packs.shipped_packs()
    assert "banking-regulatory" in packs, "banking-regulatory pack must be shipped"
    assert "banking-dq" in packs, "banking-dq pack must be shipped"

    regulatory = packs["banking-regulatory"]
    assert {"document", "clause", "regulation"}.issubset(regulatory.kinds), (
        f"banking-regulatory must declare the Domain kinds, got {regulatory.kinds!r}")
    assert not (regulatory.kinds & NODE_KINDS), (
        f"a pack kind must never collide with a core kind: {regulatory.kinds & NODE_KINDS!r}")

    dq = packs["banking-dq"]
    assert not (dq.kinds & NODE_KINDS), (
        f"banking-dq must not collide with core kinds either: {dq.kinds & NODE_KINDS!r}")


# --------------------------------------------------------------------------- #
# P1 — add_document writes one document + N clause nodes, no approval required
# --------------------------------------------------------------------------- #
def test_p1_add_document_writes_document_and_clause_nodes_unapproved(connection):
    from boss_engine.services.knowledge import ontology_packs
    from boss_engine.services.knowledge.documents import add_document

    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    result = add_document(
        connection, project_id=P, title="Reg Z 1026.36(c)",
        source_url="https://www.ecfr.gov/current/title-12/chapter-X/part-1026/subpart-C/section-1026.36",
        citation="12 CFR 1026.36(c)",
        clauses=[
            {"section": "1026.36(c)(1)(i)", "text": "A periodic payment must be credited as of the date of receipt.",
             "citation": "12 CFR 1026.36(c)(1)(i)", "url": "https://www.ecfr.gov/x/1026.36"},
            {"section": "1026.36(c)(2)", "text": "A servicer must handle prepayments per this paragraph.",
             "citation": "12 CFR 1026.36(c)(2)", "url": "https://www.ecfr.gov/x/1026.36"},
        ],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )

    assert result.get("document_id"), f"add_document must return the created document id: {result!r}"
    clause_ids = result.get("clause_ids") or []
    assert len(clause_ids) == 2, f"add_document must create one clause node per clause given: {result!r}"

    rows = [dict(r) for r in connection.execute(
        text("SELECT trust_tier FROM kg_nodes WHERE id = ANY(:ids)"),
        {"ids": clause_ids}).mappings()]
    assert len(rows) == 2
    for row in rows:
        assert row["trust_tier"] != "pending_review", (
            f"clauses are facts from the source and require no approval: {row!r}")


# --------------------------------------------------------------------------- #
# P1 — regulatory_trace proposals: matched clause -> inferred implemented_by,
#       unmatched clause -> gap; neither is ever auto-approved
# --------------------------------------------------------------------------- #
def test_p1_regulatory_trace_proposals_are_inferred_not_approved(connection):
    from boss_engine.services.knowledge import ontology_packs
    from boss_engine.services.knowledge.documents import add_document
    from boss_engine.services.knowledge.domain import propose_gap, propose_implementation

    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    doc = add_document(
        connection, project_id=P, title="Reg Z 1026.36(c) trace fixture",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(1)(i)", "text": "Credit payments as of date of receipt.",
                  "citation": "12 CFR 1026.36(c)(1)(i)", "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )
    clause_id = doc["clause_ids"][0]

    implemented = propose_implementation(
        connection, project_id=P, clause_id=clause_id, target_id=clause_id,
        rationale="matched against existing code for this test", confidence=0.7,
        actor_ref="regulatory_trace",
    )
    gap = propose_gap(
        connection, project_id=P, clause_id=clause_id,
        rationale="no rule or code implements this clause", actor_ref="regulatory_trace",
    )

    for proposal, edge_kind in ((implemented, "implemented_by"), (gap, "gap")):
        edge_id = proposal.get("edge_id")
        assert edge_id, f"{edge_kind} proposal must return the created edge id: {proposal!r}"
        row = dict(connection.execute(
            text("SELECT kind, trust_tier FROM kg_edges WHERE id = :i"), {"i": edge_id}).mappings().one())
        assert row["kind"] == edge_kind
        assert row["trust_tier"] == "inferred", (
            f"a {edge_kind} proposal must be written inferred, never auto-approved: {row!r}")


# --------------------------------------------------------------------------- #
# P0 — AG-1 closure: a plain-language trace capability/template resolves to
#       a real start_workflow offer, not a dead-end refusal
# --------------------------------------------------------------------------- #
def test_p0_proposing_trace_regulation_becomes_a_start_workflow_offer():
    from boss_engine.services.workflow_catalogue import CATALOGUE
    from boss_engine.tools.builtin import build_default_registry

    def _propose(tool_name):
        ctx = types.SimpleNamespace(settings=get_settings())
        handler = build_default_registry().get("propose_action").handler
        return handler(ctx, project_id=P, actor="u",
                        args={"tool": tool_name, "label": "Trace this regulation",
                              "args": {"directive": "trace this regulation against our code"}})

    keys = {entry.get("key") for entry in CATALOGUE}
    assert "regulatory_trace" in keys, "regulatory_trace must be a seeded catalogue template"
    capabilities = {
        (step.get("binding") or {}).get("capability")
        for entry in CATALOGUE for step in (entry.get("steps") or [])
    }
    assert "trace_regulation" in capabilities, "trace_regulation must be a catalogue step capability"

    for name in ("trace_regulation", "regulatory_trace"):
        out = _propose(name)
        assert out.get("ok") is True, (
            f"propose_action must rewrite {name!r} into a start_workflow offer, not refuse it: {out}")
        assert out.get("tool") == "start_workflow", (
            f"propose_action must redirect {name!r} to start_workflow, got tool={out.get('tool')!r}")
        assert out.get("args", {}).get("workflow_key") == "regulatory_trace", (
            f"propose_action must carry the resolved workflow key for {name!r}: {out.get('args')!r}")


# --------------------------------------------------------------------------- #
# P1 — review actions on a trace proposal: only a person may approve/reject,
#       approval is idempotent, and list_pending/open_proposals reflect state
# --------------------------------------------------------------------------- #
def test_p1_approve_link_requires_a_person_and_is_idempotent(connection):
    from boss_engine.services.knowledge import ontology_packs
    from boss_engine.services.knowledge.documents import add_document
    from boss_engine.services.knowledge.domain import (
        TraceRefused, approve_link, list_pending, open_proposals, propose_implementation,
    )

    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    doc = add_document(
        connection, project_id=P, title="Reg Z 1026.36(c) review fixture",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(1)(ii)", "text": "Credit payments as of date of receipt.",
                  "citation": "12 CFR 1026.36(c)(1)(ii)", "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )
    clause_id = doc["clause_ids"][0]
    proposal = propose_implementation(
        connection, project_id=P, clause_id=clause_id, target_id=clause_id,
        rationale="matched for review-flow test", confidence=0.8, actor_ref="regulatory_trace",
        trace_id="trace-1",
    )
    edge_id = proposal["edge_id"]

    with pytest.raises(TraceRefused):
        approve_link(connection, project_id=P, edge_id=edge_id, actor_kind="agent",
                      actor_ref="regulatory_trace")

    pending_before = list_pending(connection, project_id=P, trace_id="trace-1")
    assert any(row["id"] == edge_id for row in pending_before), (
        f"a freshly proposed, unreviewed edge must show up as pending: {pending_before!r}")
    assert open_proposals(connection, project_id=P, trace_id="trace-1") == 1

    result = approve_link(connection, project_id=P, edge_id=edge_id, actor_kind="person",
                           actor_ref="compliance.lead@example.com")
    assert result["trust_tier"] == "approved"
    assert result["already"] is False

    again = approve_link(connection, project_id=P, edge_id=edge_id, actor_kind="person",
                          actor_ref="compliance.lead@example.com")
    assert again["already"] is True, "approving an already-approved edge must be a no-op, not an error"

    pending_after = list_pending(connection, project_id=P, trace_id="trace-1")
    assert not any(row["id"] == edge_id for row in pending_after), (
        f"an approved edge must no longer be pending review: {pending_after!r}")
    assert open_proposals(connection, project_id=P, trace_id="trace-1") == 0


def test_p1_reject_link_requires_a_person_and_closes_validity(connection):
    from boss_engine.services.knowledge import ontology_packs
    from boss_engine.services.knowledge.documents import add_document
    from boss_engine.services.knowledge.domain import (
        TraceRefused, list_pending, propose_gap, reject_link,
    )

    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    doc = add_document(
        connection, project_id=P, title="Reg Z 1026.36(c) reject fixture",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(1)(iii)", "text": "Credit payments as of date of receipt.",
                  "citation": "12 CFR 1026.36(c)(1)(iii)", "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )
    clause_id = doc["clause_ids"][0]
    gap = propose_gap(
        connection, project_id=P, clause_id=clause_id, rationale="no implementation found",
        actor_ref="regulatory_trace", trace_id="trace-2",
    )
    edge_id = gap["edge_id"]

    with pytest.raises(TraceRefused):
        reject_link(connection, project_id=P, edge_id=edge_id, actor_kind="agent",
                     actor_ref="regulatory_trace")

    outcome = reject_link(connection, project_id=P, edge_id=edge_id, actor_kind="person",
                           actor_ref="compliance.lead@example.com", note="false positive")
    assert outcome["status"] == "rejected"

    pending = list_pending(connection, project_id=P, trace_id="trace-2")
    assert not any(row["id"] == edge_id for row in pending), (
        f"a rejected edge must no longer be pending review: {pending!r}")


def test_p1_review_actions_refuse_an_edge_outside_the_project_or_not_a_proposal(connection):
    from boss_engine.services.knowledge import ontology_packs
    from boss_engine.services.knowledge.documents import add_document
    from boss_engine.services.knowledge.domain import TraceRefused, approve_link

    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")

    with pytest.raises(TraceRefused):
        approve_link(connection, project_id=P, edge_id="nonexistent-edge-id",
                      actor_kind="person", actor_ref="compliance.lead@example.com")

    doc = add_document(
        connection, project_id=P, title="Reg Z 1026.36(c) non-proposal fixture",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(1)(iv)", "text": "Credit payments as of date of receipt.",
                  "citation": "12 CFR 1026.36(c)(1)(iv)", "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com",
    )
    document_id = doc["document_id"]
    clause_id = doc["clause_ids"][0]
    non_proposal_edge_id = STORE.upsert_edge(
        connection, project_id=P, src_id=clause_id, dst_id=document_id, kind="references",
        actor_kind="agent", actor_ref="system",
    )
    with pytest.raises(TraceRefused):
        approve_link(connection, project_id=P, edge_id=non_proposal_edge_id,
                      actor_kind="person", actor_ref="compliance.lead@example.com")


# --------------------------------------------------------------------------- #
# P1 — ontology pack lifecycle: unknown pack ids are refused, and rolling
#       back a project's pack removes its granted kinds immediately
# --------------------------------------------------------------------------- #
def test_p1_unknown_pack_id_is_refused_on_load_and_on_set(connection):
    from boss_engine.services.knowledge import ontology_packs

    with pytest.raises(ontology_packs.PackRefused):
        ontology_packs.load_pack("not-a-real-pack")

    with pytest.raises(ontology_packs.PackRefused):
        ontology_packs.set_project_pack(connection, project_id=P, pack_id="not-a-real-pack")


def test_p1_rollback_project_pack_revokes_granted_kinds_immediately(connection):
    from boss_engine.services.knowledge import ontology_packs

    ontology_packs.set_project_pack(connection, project_id=P, pack_id="banking-regulatory")
    assert "clause" in ontology_packs.effective_node_kinds(connection, project_id=P)

    STORE.upsert_node(
        connection, project_id=P, kind="clause", node_key="clause:rollback:1",
        name="clause", body="text", trust_tier="governed", actor_kind="person",
    )

    ontology_packs.rollback_project_pack(connection, project_id=P)
    assert "clause" not in ontology_packs.effective_node_kinds(connection, project_id=P), (
        "rolling back a project's pack must immediately revoke its granted kinds "
        "(no stale cache entry)."
    )

    with pytest.raises(ValueError):
        STORE.upsert_node(
            connection, project_id=P, kind="clause", node_key="clause:rollback:2",
            name="clause", body="text", trust_tier="governed", actor_kind="agent",
        )
