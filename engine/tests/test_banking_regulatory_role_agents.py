"""KG P5b follow-up: the Compliance Analyst, Pipeline Orchestrator, Lane Agent and Remediation
Advisor capabilities of `banking_regulatory_pipeline.py` reason through the governed agent-
decision path (`_role_agent`/`_ask_role`, built on `make_code_agent(...).run_role(...)` exactly
like `regulatory_trace._trace_agent`) instead of a keyword match or a fixed confidence ladder.

These tests run the real pipeline functions against a real, disposable postgres (like
test_banking_regulatory_compliance.py and test_regulatory_trace.py) and swap only the governed
agent for a scripted fake that answers the JSON-between-markers contract — the SCRIPTED engine
convention this codebase already uses for judged, model-backed steps.
"""
from __future__ import annotations

import os
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import banking_regulatory_pipeline as pipeline
from boss_engine.services.knowledge import ontology_packs
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


class FakeAgent:
    """Answers a role prompt with a scripted JSON judgement — no paid model touched."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        assert read_only is True, "a role judgement call never writes code"
        self.prompts.append(str(prompt))
        import json as _json
        summary = (f"{pipeline._MARK_OPEN}\n{_json.dumps(self.payload)}\n"
                   f"{pipeline._MARK_CLOSE}\n")
        return {"summary": summary, "usage": {}, "cost_usd_micros": None,
                "cost_unreported": True, "model": "fake-scripted"}


def _install_fake(monkeypatch, agent: FakeAgent) -> None:
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agent, "scripted"))


class BankingRegulatoryRoleAgentTests(unittest.TestCase):
    """Base: a real, disposable postgres project seeded fresh per test."""

    @classmethod
    def setUpClass(cls):
        url = _database_url()
        if not url:
            raise unittest.SkipTest("DATABASE_URL must be set to a reachable postgres for these tests")
        cls.engine = create_engine(url, future=True)
        try:
            with cls.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise unittest.SkipTest(f"postgres is not reachable: {exc.__class__.__name__}")
        ensure_schema(cls.engine)

    def setUp(self):
        self._conn_ctx = self.engine.connect()
        self.connection = self._conn_ctx.__enter__()
        self.transaction = self.connection.begin()
        suffix = uuid.uuid4().hex[:8]
        self.org_id = f"org_bankrole_{suffix}"
        self.ws_id = f"ws_bankrole_{suffix}"
        self.project_id = f"p_bankrole_{suffix}"
        self.connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'Banking Reg Roles')"), {"i": self.org_id})
        self.connection.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'Banking Reg Roles')"),
            {"i": self.ws_id, "o": self.org_id})
        self.connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                 "(:i, :w, 'Banking Reg Roles', :s)"),
            {"i": self.project_id, "w": self.ws_id, "s": self.project_id})
        ontology_packs.set_project_pack(
            self.connection, project_id=self.project_id, pack_id="banking-regulatory")

    def tearDown(self):
        self.transaction.rollback()
        self._conn_ctx.__exit__(None, None, None)

    def _add_rule(self, *, name: str, body: str) -> str:
        return STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="rule", node_key=f"rule:{name}",
            name=name, body=body, trust_tier="governed", actor_kind="person",
            actor_ref="compliance.lead@example.com")

    def _add_case(self, *, key: str = "subject") -> str:
        return STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="compliance_case",
            node_key=f"case:{key}", name=f"Compliance case: {key}", body="",
            trust_tier="inferred", actor_kind="agent", actor_ref="test",
            attrs={"status": "open"})

    # ── Compliance Analyst: cites only the candidate the agent chose, by node_id ───────────────

    def test_compliance_assessment_cites_the_candidate_the_agent_chose(self, *, monkeypatch=None):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            subject_id = self._add_case()
            rule_id = self._add_rule(
                name="repayment order", body="Apply interest before principal.")
            agent = FakeAgent({"status": "implemented", "confidence": 0.9,
                               "citation": rule_id, "rationale": "the rule covers this"})
            _install_fake(mp, agent)

            result = pipeline.run_compliance_assessment(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "compliance_assessment", "run_id": "run_a"},
                context={"steps": {"context_enrichment": {"collected": [rule_id]}}},
                inputs={"subject_id": subject_id})

            self.assertEqual(result["assessment"], "implemented")
            self.assertEqual(result["citation"], rule_id)
            self.assertAlmostEqual(result["confidence"], 0.9)

            edge = self.connection.execute(
                text("SELECT kind FROM kg_edges WHERE project_id = :p AND src_id = :s "
                     "AND dst_id = :d"),
                {"p": self.project_id, "s": subject_id, "d": rule_id},
            ).mappings().first()
            self.assertIsNotNone(edge, "an implemented_by edge must be proposed to the cited rule")
            self.assertEqual(edge["kind"], "implemented_by")
        finally:
            mp.undo()

    def test_compliance_assessment_ignores_a_citation_outside_the_offered_candidates(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            subject_id = self._add_case()
            rule_id = self._add_rule(name="unrelated rule", body="Something else entirely.")
            # The agent hallucinates a node_id never offered to it — must not be trusted.
            agent = FakeAgent({"status": "implemented", "confidence": 0.9,
                               "citation": "node_not_offered", "rationale": "made up"})
            _install_fake(mp, agent)

            result = pipeline.run_compliance_assessment(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "compliance_assessment", "run_id": "run_b"},
                context={"steps": {"context_enrichment": {"collected": [rule_id]}}},
                inputs={"subject_id": subject_id})

            self.assertEqual(result["assessment"], "gap")
            self.assertEqual(result["citation"], subject_id)
        finally:
            mp.undo()

    def test_compliance_assessment_is_a_gap_with_no_candidates_and_makes_no_agent_call(self):
        subject_id = self._add_case()

        class ExplodingAgent:
            def run_role(self, *a, **k):
                raise AssertionError("no candidate and no incident: must not call the agent")

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (ExplodingAgent(), "scripted"))
            result = pipeline.run_compliance_assessment(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "compliance_assessment", "run_id": "run_c"}, context={},
                inputs={"subject_id": subject_id})
            self.assertEqual(result["assessment"], "gap")
            self.assertEqual(result["citation"], subject_id)
        finally:
            mp.undo()

    # ── Pipeline Orchestrator: routes by the agent's family choice, never a keyword match ──────

    def test_compliance_orchestration_routes_by_the_agents_family_choice(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            rule_id = self._add_rule(
                name="prepay rule",
                body="This text intentionally names no family keyword at all.")
            agent = FakeAgent({"family": "prepayment", "confidence": 0.8, "rationale": "prepay"})
            _install_fake(mp, agent)

            result = pipeline.run_compliance_orchestration(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "compliance_orchestration"}, context={
                    "steps": {"compliance_assessment": {"citation": rule_id}}},
                inputs={})
            self.assertEqual(result["finding_families"], ["prepayment"])
        finally:
            mp.undo()

    def test_compliance_orchestration_drops_a_family_outside_the_allowed_set(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            agent = FakeAgent({"family": "not_a_real_family", "confidence": 0.5})
            _install_fake(mp, agent)
            result = pipeline.run_compliance_orchestration(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "compliance_orchestration"}, context={}, inputs={})
            self.assertEqual(result["finding_families"], [])
        finally:
            mp.undo()

    # ── Lane Agent: confirms/rules out from its own judgement, with a stated reason ────────────

    def test_lane_confirmation_confirms_the_inc_2088_allocation_citation(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            rule_id = self._add_rule(
                name="allocation rule", body="Apply interest before principal on each payment.")
            agent = FakeAgent({"confirmed": True, "reason": "clause and code agree"})
            _install_fake(mp, agent)

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_d"}, context={
                    "steps": {"compliance_assessment": {"citation": rule_id}}}, inputs={})
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["cited_node_id"], rule_id)

            decision = self.connection.execute(
                text("SELECT decision_type FROM agent_decisions WHERE project_id = :p "
                     "AND decision_type = 'Confirmed' ORDER BY created_at DESC LIMIT 1"),
                {"p": self.project_id},
            ).mappings().first()
            self.assertIsNotNone(decision)
        finally:
            mp.undo()

    def test_lane_confirmation_rules_out_with_no_citation_and_never_calls_the_agent(self):
        class ExplodingAgent:
            def run_role(self, *a, **k):
                raise AssertionError("no citation to read: must not call the agent")

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (ExplodingAgent(), "scripted"))
            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_e"}, context={}, inputs={})
            self.assertFalse(result["confirmed"])
            self.assertIsNone(result["cited_node_id"])
        finally:
            mp.undo()

    # ── Remediation Advisor: its own change brief and test ref back the written package ────────

    def test_remediation_package_uses_the_advisors_change_brief_and_test_ref(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            rule_id = self._add_rule(name="allocation rule 2", body="Interest before principal.")
            agent = FakeAgent({"change_brief": "Fix the allocation order for repayments.",
                               "test_ref": "payments/test_alloc.py::test_fix"})
            _install_fake(mp, agent)

            result = pipeline.run_remediation_package(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "remediation_package", "run_id": "run_f"}, context={
                    "steps": {"compliance_assessment": {"citation": rule_id},
                             "compliance_orchestration": {"finding_families": ["payment_allocation"]}}},
                inputs={})

            package_id = result["remediation_package_id"]
            row = self.connection.execute(
                text("SELECT attrs FROM kg_nodes WHERE project_id = :p AND id = :i"),
                {"p": self.project_id, "i": package_id},
            ).mappings().first()
            self.assertEqual(row["attrs"]["change_brief"], "Fix the allocation order for repayments.")
            self.assertEqual(row["attrs"]["test_ref"], "payments/test_alloc.py::test_fix")
        finally:
            mp.undo()

    def test_remediation_package_falls_back_when_the_advisor_answers_nothing_readable(self):
        class UnreadableAgent:
            def run_role(self, *a, **k):
                return {"summary": "not json at all", "usage": {}, "cost_usd_micros": None,
                        "cost_unreported": True, "model": "fake-scripted"}

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            rule_id = self._add_rule(name="allocation rule 3", body="Interest before principal.")
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (UnreadableAgent(), "scripted"))

            result = pipeline.run_remediation_package(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "remediation_package", "run_id": "run_g"}, context={
                    "steps": {"compliance_assessment": {"citation": rule_id},
                             "compliance_orchestration": {"finding_families": ["payment_allocation"]}}},
                inputs={})

            package_id = result["remediation_package_id"]
            row = self.connection.execute(
                text("SELECT attrs FROM kg_nodes WHERE project_id = :p AND id = :i"),
                {"p": self.project_id, "i": package_id},
            ).mappings().first()
            self.assertEqual(
                row["attrs"]["change_brief"],
                "Credit a periodic payment as of the date of receipt, applying it "
                "interest-first then to principal, before any later-dated payment is "
                "considered.")
            self.assertEqual(
                row["attrs"]["test_ref"],
                "payments/test_repayment_order.py::test_interest_before_principal")
        finally:
            mp.undo()

    def test_remediation_package_credits_the_owner_connected_to_the_citation(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            rule_id = self._add_rule(name="allocation rule 4", body="Interest before principal.")
            owner_id = STORE.upsert_node(
                self.connection, project_id=self.project_id, kind="owner",
                node_key="owner:cards-team", name="Cards Team", body="",
                trust_tier="governed", actor_kind="person",
                actor_ref="compliance.lead@example.com", attrs={"team": "Cards Team"})
            STORE.upsert_edge(
                self.connection, project_id=self.project_id, src_id=rule_id, dst_id=owner_id,
                kind="owns", actor_kind="person", actor_ref="compliance.lead@example.com")
            agent = FakeAgent({"change_brief": "Fix it.", "test_ref": "t.py::t"})
            _install_fake(mp, agent)

            result = pipeline.run_remediation_package(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "remediation_package", "run_id": "run_h"}, context={
                    "steps": {"compliance_assessment": {"citation": rule_id},
                             "compliance_orchestration": {"finding_families": ["payment_allocation"]}}},
                inputs={})

            self.assertEqual(result["owner_team"], "Cards Team")

            edge = self.connection.execute(
                text("SELECT kind FROM kg_edges WHERE project_id = :p AND src_id = :s "
                     "AND dst_id = :d AND kind = 'owns'"),
                {"p": self.project_id, "s": result["remediation_package_id"], "d": owner_id},
            ).mappings().first()
            self.assertIsNotNone(edge, "the remediation package must link to its owner via 'owns'")
        finally:
            mp.undo()


if __name__ == "__main__":
    unittest.main()
