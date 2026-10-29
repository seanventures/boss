"""The ``regulatory_trace`` workflow (KG Domain layer v1, A2) on the scripted/fake engine.

These tests pin the behaviours the reviewer asked for on
``boss_engine/services/knowledge/regulatory_trace.py``:

- a clause never links to itself or another clause — only rule/code (implementation) nodes are
  candidates, and the implemented_by edge points at one of them;
- the trace THINKS: the governed agent judges the candidates, and its chosen candidate and its
  own rationale/confidence are what gets stored (no hard-coded verdict);
- budget exhaustion stops the trace with ``stopped_by='budget'``;
- a clause with no candidates is a gap with zero spend and no model call.

They run against a real, disposable postgres (DATABASE_URL), like test_kg_domain_layer.py, but
swap the governed agent and the budget governor for fakes so no paid model is touched.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import domain, ontology_packs, regulatory_trace
from boss_engine.services.knowledge.documents import add_document
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
P = "proj_regtrace_a2"


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
            conn.execute(text(
                "INSERT INTO orgs (id, name) VALUES ('org_regtrace', 'Reg Trace')"))
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES "
                "('ws_regtrace', 'org_regtrace', 'Reg Trace')"))
            conn.execute(text(
                f"INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                f"('{P}', 'ws_regtrace', 'Reg Trace', 'reg-trace')"))
            ontology_packs.set_project_pack(conn, project_id=P, pack_id="banking-regulatory")
            yield conn
        finally:
            transaction.rollback()


# ── the fakes around the trace's edges ───────────────────────────────────────────────────────

class FakeAgent:
    """Answers the judge prompt with a scripted JSON judgement (no model)."""

    def __init__(self, *, choice, rationale: str, confidence: float):
        self.choice = choice
        self.rationale = rationale
        self.confidence = confidence
        self.prompts: list[str] = []

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        assert read_only is True, "the trace judge never writes code"
        self.prompts.append(str(prompt))
        choice = "null" if self.choice is None else str(self.choice)
        summary = (
            f"{regulatory_trace._MARK_OPEN}\n"
            f'{{"choice": {choice}, "rationale": "{self.rationale}", '
            f'"confidence": {self.confidence}}}\n'
            f"{regulatory_trace._MARK_CLOSE}\n"
        )
        return {"summary": summary, "usage": {"model": "fake"}, "cost_usd_micros": None,
                "cost_unreported": True, "model": "fake"}


class FakeGovernor:
    def __init__(self, *, estimate: int = regulatory_trace.CALL_ESTIMATE_USD_MICROS,
                 cost: int = regulatory_trace.CALL_ESTIMATE_USD_MICROS):
        self.estimate_usd_micros = estimate
        self.cost = cost
        self.reserved = 0
        self.settled = 0

    def reserve(self):
        self.reserved += 1
        return ("res", self.reserved)

    def settle(self, held, usage):
        self.settled += 1
        return self.cost

    def fail(self, held, error):  # pragma: no cover - not exercised here
        pass


def _install_fakes(monkeypatch, agent: FakeAgent, governor: FakeGovernor):
    monkeypatch.setattr(regulatory_trace, "_trace_agent",
                        lambda *a, **k: (agent, "scripted"))
    from boss_engine.services.knowledge import rule_scan

    monkeypatch.setattr(rule_scan, "Governor", lambda *a, **k: governor)


def _add_rule(connection, *, body: str, name: str = "credit payment rule") -> str:
    return STORE.upsert_node(
        connection, project_id=P, kind="rule", node_key=f"rule:{name}",
        name=name, body=body, trust_tier="governed", actor_kind="person",
        actor_ref="compliance.lead@example.com")


_MATCHING = "A periodic payment must be credited as of the date of receipt."


def _add_document(connection, *, section: str, clause_text: str, title: str) -> str:
    doc = add_document(
        connection, project_id=P, title=title,
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": section, "text": clause_text,
                  "citation": "12 CFR " + section, "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com")
    return doc["document_id"], doc["clause_ids"][0]


# ── candidates never include the clause itself or another clause ──────────────────────────────

def test_candidates_are_only_implementation_nodes_never_clauses_or_documents(connection):
    rule_id = _add_rule(connection, body="credit the periodic payment as of the date of receipt")
    doc_id, clause_id = _add_document(
        connection, section="1026.36(c)(1)(i)", clause_text=_MATCHING,
        title="Reg Z candidate-filter fixture")
    # Another clause, in another document, with text that also matches: it must never be a
    # candidate (a clause is what is being traced, not what traces it).
    _other_doc, other_clause = _add_document(
        connection, section="1026.36(c)(2)", clause_text=_MATCHING,
        title="Reg Z sibling clause fixture")

    candidates = regulatory_trace._candidates(connection, P, clause_id, _MATCHING)
    ids = {c["node_id"] for c in candidates}
    assert rule_id in ids, f"the matching rule must be a candidate: {candidates!r}"
    assert clause_id not in ids, "a clause must never be a candidate for itself"
    assert other_clause not in ids, "another clause must never be a candidate"
    assert doc_id not in ids, "a document must never be a candidate"
    assert all(c["kind"] in regulatory_trace._implementation_kinds(connection, P)
               for c in candidates), candidates


# ── the agent's choice and rationale are what gets stored ─────────────────────────────────────

def test_the_agents_chosen_candidate_and_rationale_are_stored(connection, monkeypatch):
    rule_id = _add_rule(connection, body="credit the periodic payment as of the date of receipt")
    _doc_id, clause_id = _add_document(
        connection, section="1026.36(c)(1)(i)", clause_text=_MATCHING,
        title="Reg Z trace fixture")

    agent = FakeAgent(choice=0, rationale="this rule credits payments on the receipt date",
                      confidence=0.83)
    governor = FakeGovernor()
    _install_fakes(monkeypatch, agent, governor)

    result = regulatory_trace.run_step(
        connection, None, P, {"id": "trace"}, {"run_id": "wfr_trace_1"},
        {"document_id": _doc_id_of(connection, clause_id)})

    assert agent.prompts, "the agent must be asked to judge the candidate"
    assert result["implemented_count"] == 1 and result["gap_count"] == 0
    assert result["stopped_by"] == "scope_exhausted"
    assert result["spend_usd"] > 0

    row = dict(connection.execute(
        text("SELECT src_id, dst_id, kind, trust_tier, attrs FROM kg_edges "
             "WHERE project_id = :p AND kind = 'implemented_by' AND valid_to IS NULL"),
        {"p": P}).mappings().one())
    assert row["src_id"] == clause_id
    assert row["dst_id"] == rule_id, "the edge must point at the judged rule, not the clause"
    assert row["dst_id"] != clause_id, "a clause must never link to itself"
    assert row["trust_tier"] == "inferred", "a proposal is never auto-approved"
    assert row["attrs"]["rationale"] == "this rule credits payments on the receipt date"
    assert row["attrs"]["confidence"] == pytest.approx(0.83)


def _doc_id_of(connection, clause_id: str) -> str:
    return connection.execute(
        text("SELECT attrs->>'document_id' FROM kg_nodes WHERE id = :i"),
        {"i": clause_id}).scalar()


# ── a clause with no candidate is a gap, with no model call and zero spend ────────────────────

def test_a_clause_with_no_candidate_is_a_gap_with_zero_spend(connection, monkeypatch):
    _doc_id, clause_id = _add_document(
        connection, section="1026.36(c)(3)", clause_text="Nothing here resembles any rule zzxq.",
        title="Reg Z unmatched fixture")

    agent = FakeAgent(choice=0, rationale="unused", confidence=0.9)
    governor = FakeGovernor()
    _install_fakes(monkeypatch, agent, governor)

    result = regulatory_trace.run_step(
        connection, None, P, {"id": "trace"}, {"run_id": "wfr_trace_gap"},
        {"document_id": _doc_id_of(connection, clause_id)})

    assert agent.prompts == [], "a clause with no candidate must not call the model"
    assert governor.reserved == 0, "a gap with no candidate reserves no budget"
    assert result["gap_count"] == 1 and result["implemented_count"] == 0
    assert result["spend_usd"] == 0

    row = dict(connection.execute(
        text("SELECT src_id, dst_id, kind, trust_tier FROM kg_edges "
             "WHERE project_id = :p AND kind = 'gap' AND valid_to IS NULL"),
        {"p": P}).mappings().one())
    assert row["src_id"] == clause_id and row["trust_tier"] == "inferred"


# ── budget exhaustion stops the trace in words ────────────────────────────────────────────────

def test_budget_exhaustion_stops_with_stopped_by_budget(connection, monkeypatch):
    rule_body = "credit the periodic payment as of the date of receipt"
    _add_rule(connection, body=rule_body)
    doc = add_document(
        connection, project_id=P, title="Reg Z budget fixture",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(4)", "text": _MATCHING,
                  "citation": "12 CFR 1026.36(c)(4)", "url": "https://www.ecfr.gov/x/1026.36"},
                 {"section": "1026.36(c)(5)", "text": _MATCHING,
                  "citation": "12 CFR 1026.36(c)(5)", "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com")

    agent = FakeAgent(choice=0, rationale="matches", confidence=0.7)
    # One call fits the budget; the second would overrun it.
    governor = FakeGovernor(estimate=150_000, cost=150_000)
    _install_fakes(monkeypatch, agent, governor)

    result = regulatory_trace.run_step(
        connection, None, P, {"id": "trace"}, {"run_id": "wfr_trace_budget"},
        {"document_id": doc["document_id"], "budget_usd": 0.15})

    assert result["stopped_by"] == "budget", result
    assert len(agent.prompts) == 1, "the trace must stop before the call that would overrun"
    assert result["spend_usd"] == pytest.approx(0.15)
