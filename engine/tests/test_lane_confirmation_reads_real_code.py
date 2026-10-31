"""KG P7c: the Lane Agent's confirmation must be judged against the real source lines of the
code symbol it cites, not against the node's name/body text alone. These tests exercise
``run_lane_confirmation`` through the real pipeline/postgres path (same base fixture as
``test_banking_regulatory_role_agents.py``), swapping only the governed agent for a scripted
fake and the repo reader for a fake reader — never touching a paid model or a real git host.
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


def _install_fake_agent(monkeypatch, agent: FakeAgent) -> None:
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agent, "scripted"))


def _install_fake_reader(monkeypatch, read_fn) -> None:
    """Swaps the read-only repo_reader wrapper so tests never hit a real git host."""
    monkeypatch.setattr(pipeline, "_repo_reader", lambda *a, **k: read_fn)


class LaneConfirmationReadsRealCodeTests(unittest.TestCase):
    """Base: a real, disposable postgres project seeded fresh per test (mirrors
    test_banking_regulatory_role_agents.py's fixture)."""

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
        self.org_id = f"org_lanekg_{suffix}"
        self.ws_id = f"ws_lanekg_{suffix}"
        self.project_id = f"p_lanekg_{suffix}"
        self.connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'Lane Confirmation KG')"),
            {"i": self.org_id})
        self.connection.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'Lane Confirmation KG')"),
            {"i": self.ws_id, "o": self.org_id})
        self.connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                 "(:i, :w, 'Lane Confirmation KG', :s)"),
            {"i": self.project_id, "w": self.ws_id, "s": self.project_id})
        ontology_packs.set_project_pack(
            self.connection, project_id=self.project_id, pack_id="banking-regulatory")

    def tearDown(self):
        self.transaction.rollback()
        self._conn_ctx.__exit__(None, None, None)

    def _add_clause_case(self, *, key: str = "subject", clause_text: str,
                         citation: str = "Reg Z §1026.17") -> tuple[str, str]:
        clause_id = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="clause",
            node_key=f"clause:{key}", name=f"Clause {key}", body=clause_text,
            trust_tier="governed", actor_kind="person",
            actor_ref="compliance.lead@example.com", attrs={"citation": citation})
        case_id = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="compliance_case",
            node_key=f"case:{key}", name=f"Compliance case: {key}", body="",
            trust_tier="inferred", actor_kind="agent", actor_ref="test",
            attrs={"status": "open", "clause_id": clause_id})
        return case_id, clause_id

    def _add_rule_with_symbol(self, *, name: str, body: str, symbol_path: str = "payments/alloc.py",
                              line: int = 10, end_line: int = 20) -> tuple[str, str]:
        rule_id = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="rule", node_key=f"rule:{name}",
            name=name, body=body, trust_tier="governed", actor_kind="person",
            actor_ref="compliance.lead@example.com")
        symbol_id = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="symbol",
            node_key=f"symbol:{name}", name=f"{name}_fn", body="",
            trust_tier="governed", actor_kind="person",
            actor_ref="compliance.lead@example.com",
            attrs={"path": symbol_path, "line": line, "end_line": end_line})
        STORE.upsert_edge(
            self.connection, project_id=self.project_id, src_id=rule_id, dst_id=symbol_id,
            kind="evidenced_by", actor_kind="person", actor_ref="compliance.lead@example.com")
        return rule_id, symbol_id

    # P0: the happy path — real clause text, assessment statement and fetched source all reach
    # the Lane Agent's prompt, and a confirmed answer is recorded as Confirmed.
    def test_p0_confirmed_answer_sees_clause_assessment_and_source_and_is_recorded(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            case_id, clause_id = self._add_clause_case(
                clause_text="A creditor must credit a payment as of the date of receipt.")
            rule_id, symbol_id = self._add_rule_with_symbol(
                name="allocation rule", body="Apply interest before principal.")

            def fake_read(path, start, end, repo):
                self.assertEqual(path, "payments/alloc.py")
                return {"content": "def allocate(payment):\n    return apply_interest_first(payment)",
                        "start": start, "end": end}

            _install_fake_reader(mp, fake_read)
            agent = FakeAgent({"verdict": "confirmed", "reason": "clause and code agree"})
            _install_fake_agent(mp, agent)

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p0"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": rule_id, "subject_id": case_id,
                        "note": "Assessed as implemented (confidence 0.90): the rule covers this"}}},
                inputs={})

            self.assertEqual(result["verdict"], "confirmed")
            self.assertTrue(result["confirmed"])

            prompt = agent.prompts[0]
            self.assertIn("A creditor must credit a payment as of the date of receipt.", prompt)
            self.assertIn("Assessed as implemented", prompt)
            self.assertIn("apply_interest_first", prompt)

            decision = self.connection.execute(
                text("SELECT decision_type FROM agent_decisions WHERE project_id = :p "
                     "AND decision_type = 'Confirmed' ORDER BY created_at DESC LIMIT 1"),
                {"p": self.project_id},
            ).mappings().first()
            self.assertIsNotNone(decision)
        finally:
            mp.undo()

    # P0: a symbol exactly as code_index writes it -- repository and file in the node KEY
    # (symbol:<repo>:<path>:<name>), only line numbers in attrs. Reading attrs alone made every
    # real symbol "has no path to read" (2026-10-31, NorthPeak).
    def test_p0_a_symbol_is_read_from_where_the_indexer_records_it(self):
        import pytest
        from boss_engine.services.knowledge.code_index import symbol_key
        mp = pytest.MonkeyPatch()
        try:
            case_id, _ = self._add_clause_case(key="indexed", clause_text="Credit as of receipt.")
            rule_id = STORE.upsert_node(
                self.connection, project_id=self.project_id, kind="rule",
                node_key="rule:indexed", name="indexed rule", body="Repayment-like classification",
                trust_tier="governed", actor_kind="person", actor_ref="compliance.lead@example.com")
            path = "fineract-loan/src/main/java/org/x/LoanTransactionType.java"
            symbol_id = STORE.upsert_node(
                self.connection, project_id=self.project_id, kind="symbol",
                node_key=symbol_key("acme/lending", path, "LoanTransactionType.isRepaymentType"),
                name="LoanTransactionType.isRepaymentType", body="", trust_tier="observed",
                actor_kind="system", actor_ref="code_index",
                attrs={"lang": "java", "line": 193, "end_line": 196, "symbol_kind": "method"})
            STORE.upsert_edge(
                self.connection, project_id=self.project_id, src_id=rule_id, dst_id=symbol_id,
                kind="evidenced_by", actor_kind="person", actor_ref="compliance.lead@example.com")
            seen = {}

            def fake_read(p, start, end, repo):
                seen.update(path=p, repo=repo, start=start, end=end)
                return {"content": "boolean isRepaymentType() { return this == REPAYMENT; }"}

            _install_fake_reader(mp, fake_read)
            _install_fake_agent(mp, FakeAgent({"verdict": "confirmed", "reason": "it classifies"}))
            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_idx"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": rule_id, "subject_id": case_id, "note": "gap"}}},
                inputs={})
            self.assertEqual(seen["path"], path)
            self.assertEqual(seen["repo"], "acme/lending")
            self.assertEqual((seen["start"], seen["end"]), (190, 199))
            self.assertEqual(result["verdict"], "confirmed")
        finally:
            mp.undo()

    # P0: the lane also reads the code the correlated incident was found in, not only the code
    # the regulation's rule points at (2026-10-31: INC-2088's allocation code was never read).
    def test_p0_the_lane_reads_the_code_the_incident_was_found_in(self):
        import pytest
        from boss_engine.services.knowledge.code_index import symbol_key
        mp = pytest.MonkeyPatch()
        try:
            case_id, _ = self._add_clause_case(key="incident", clause_text="Credit as of receipt.")
            rule_id, _sym = self._add_rule_with_symbol(name="classification", body="types")
            alloc_path = "fineract-loan/src/main/java/org/x/LoanPaymentAllocationRule.java"
            alloc_id = STORE.upsert_node(
                self.connection, project_id=self.project_id, kind="symbol",
                node_key=symbol_key("acme/lending", alloc_path, "LoanPaymentAllocationRule"),
                name="LoanPaymentAllocationRule", body="", trust_tier="observed",
                actor_kind="system", actor_ref="code_index",
                attrs={"line": 40, "end_line": 90, "symbol_kind": "class"})
            incident_id = STORE.upsert_node(
                self.connection, project_id=self.project_id, kind="incident",
                node_key="incident:INC-2088", name="INC-2088", body="misallocated",
                trust_tier="observed", actor_kind="agent", actor_ref="test")
            STORE.upsert_edge(
                self.connection, project_id=self.project_id, src_id=incident_id, dst_id=alloc_id,
                kind="found_in", actor_kind="agent", actor_ref="test")
            paths = []

            def fake_read(p, start, end, repo):
                paths.append(p)
                return {"content": "class X { void apply() {} }"}

            _install_fake_reader(mp, fake_read)
            agent = FakeAgent({"verdict": "confirmed", "reason": "the allocation code shows it"})
            _install_fake_agent(mp, agent)
            pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_inc"}, context={
                    "steps": {"compliance_assessment": {
                                  "citation": rule_id, "subject_id": case_id, "note": "gap"},
                              "historical_pattern": {"matched": [incident_id]}}},
                inputs={})
            self.assertIn(alloc_path, paths)
            self.assertIn("LoanPaymentAllocationRule", agent.prompts[0])
        finally:
            mp.undo()

    # P1 (AC3): the source cannot be read for any connected symbol -> cannot_verify, never
    # ruled_out, and the agent is never called.
    def test_p1_unreadable_source_yields_cannot_verify_and_never_calls_the_agent(self):
        class ExplodingAgent:
            def run_role(self, *a, **k):
                raise AssertionError("source unreadable: must not call the agent")

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            case_id, clause_id = self._add_clause_case(
                clause_text="A creditor must credit a payment as of the date of receipt.")
            rule_id, symbol_id = self._add_rule_with_symbol(
                name="allocation rule 2", body="Apply interest before principal.")

            def fake_read(path, start, end, repo):
                return {"error": "file not found"}

            _install_fake_reader(mp, fake_read)
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (ExplodingAgent(), "scripted"))

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p1"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": rule_id, "subject_id": case_id}}},
                inputs={})

            self.assertEqual(result["verdict"], "cannot_verify")
            self.assertFalse(result["confirmed"])

            decision = self.connection.execute(
                text("SELECT decision_type FROM agent_decisions WHERE project_id = :p "
                     "AND decision_type = 'CannotVerify' ORDER BY created_at DESC LIMIT 1"),
                {"p": self.project_id},
            ).mappings().first()
            self.assertIsNotNone(decision)
        finally:
            mp.undo()

    # P1 (AC4): no citation at all -> cannot_verify (not ruled_out), the agent is still never
    # called. Replaces the old "rules_out_with_no_citation" expectation.
    def test_p1_no_citation_yields_cannot_verify_and_never_calls_the_agent(self):
        class ExplodingAgent:
            def run_role(self, *a, **k):
                raise AssertionError("no citation to read: must not call the agent")

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (ExplodingAgent(), "scripted"))
            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p1b"}, context={}, inputs={})
            self.assertEqual(result["verdict"], "cannot_verify")
            self.assertFalse(result["confirmed"])
            self.assertIsNone(result["cited_node_id"])
        finally:
            mp.undo()

    # P1 (AC5): ruled_out is reachable, but only on a path where real code was successfully
    # read and the agent explicitly rules it out.
    def test_p1_ruled_out_answer_requires_a_successfully_read_symbol(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            case_id, clause_id = self._add_clause_case(
                clause_text="A creditor must credit a payment as of the date of receipt.")
            rule_id, symbol_id = self._add_rule_with_symbol(
                name="allocation rule 3", body="Apply interest before principal.")

            def fake_read(path, start, end, repo):
                return {"content": "def allocate(payment):\n    return apply_principal_first(payment)",
                        "start": start, "end": end}

            _install_fake_reader(mp, fake_read)
            agent = FakeAgent({"verdict": "ruled_out",
                               "reason": "the code applies principal first, not interest first"})
            _install_fake_agent(mp, agent)

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p1c"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": rule_id, "subject_id": case_id}}},
                inputs={})

            self.assertEqual(result["verdict"], "ruled_out")
            self.assertFalse(result["confirmed"])

            decision = self.connection.execute(
                text("SELECT decision_type FROM agent_decisions WHERE project_id = :p "
                     "AND decision_type = 'RuledOut' ORDER BY created_at DESC LIMIT 1"),
                {"p": self.project_id},
            ).mappings().first()
            self.assertIsNotNone(decision)
        finally:
            mp.undo()

    # P1 (AC6): a malformed/unexpected agent answer degrades to cannot_verify, never an
    # invented verdict.
    def test_p1_malformed_agent_answer_degrades_to_cannot_verify(self):
        class UnreadableAgent:
            def run_role(self, *a, **k):
                return {"summary": "not json at all", "usage": {}, "cost_usd_micros": None,
                        "cost_unreported": True, "model": "fake-scripted"}

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            case_id, clause_id = self._add_clause_case(
                clause_text="A creditor must credit a payment as of the date of receipt.")
            rule_id, symbol_id = self._add_rule_with_symbol(
                name="allocation rule 4", body="Apply interest before principal.")

            def fake_read(path, start, end, repo):
                return {"content": "def allocate(payment):\n    pass", "start": start, "end": end}

            _install_fake_reader(mp, fake_read)
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (UnreadableAgent(), "scripted"))

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p1d"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": rule_id, "subject_id": case_id}}},
                inputs={})

            self.assertEqual(result["verdict"], "cannot_verify")
            self.assertFalse(result["confirmed"])
        finally:
            mp.undo()

    # P1 (AC7): the citation names a real node, but one of a kind that is never code evidence
    # (e.g. a `clause`) -> cannot_verify, and the agent is never called since there is nothing
    # to connect a symbol to.
    def test_p1_citation_of_non_rule_symbol_node_yields_cannot_verify_and_never_calls_the_agent(self):
        class ExplodingAgent:
            def run_role(self, *a, **k):
                raise AssertionError("citation is not a rule/symbol: must not call the agent")

        import pytest
        mp = pytest.MonkeyPatch()
        try:
            case_id, clause_id = self._add_clause_case(
                clause_text="A creditor must credit a payment as of the date of receipt.")
            mp.setattr(pipeline, "_role_agent", lambda *a, **k: (ExplodingAgent(), "scripted"))

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p1e"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": clause_id, "subject_id": case_id}}},
                inputs={})

            self.assertEqual(result["verdict"], "cannot_verify")
            self.assertFalse(result["confirmed"])
            self.assertEqual(result["cited_node_id"], clause_id)
        finally:
            mp.undo()

    # P0: when the citation names a rule connected to several symbols and only some of their
    # sources can be read, the agent still judges against what WAS read, and the recorded
    # reason names the symbol(s) that could not be read rather than hiding the gap.
    def test_p0_confirmed_answer_with_one_unreadable_symbol_notes_it_in_the_reason(self):
        import pytest
        mp = pytest.MonkeyPatch()
        try:
            case_id, clause_id = self._add_clause_case(
                clause_text="A creditor must credit a payment as of the date of receipt.")
            rule_id, symbol_id = self._add_rule_with_symbol(
                name="allocation rule 5", body="Apply interest before principal.",
                symbol_path="payments/alloc.py")
            missing_symbol_id = STORE.upsert_node(
                self.connection, project_id=self.project_id, kind="symbol",
                node_key="symbol:allocation rule 5 missing", name="missing_fn", body="",
                trust_tier="governed", actor_kind="person",
                actor_ref="compliance.lead@example.com",
                attrs={"path": "payments/gone.py", "line": 1, "end_line": 2})
            STORE.upsert_edge(
                self.connection, project_id=self.project_id, src_id=rule_id,
                dst_id=missing_symbol_id, kind="evidenced_by", actor_kind="person",
                actor_ref="compliance.lead@example.com")

            def fake_read(path, start, end, repo):
                if path == "payments/gone.py":
                    return {"error": "file not found"}
                return {"content": "def allocate(payment):\n    return apply_interest_first(payment)",
                        "start": start, "end": end}

            _install_fake_reader(mp, fake_read)
            agent = FakeAgent({"verdict": "confirmed", "reason": "clause and code agree"})
            _install_fake_agent(mp, agent)

            result = pipeline.run_lane_confirmation(
                self.connection, settings=None, project_id=self.project_id,
                step={"id": "lane_confirmation", "run_id": "run_p0b"}, context={
                    "steps": {"compliance_assessment": {
                        "citation": rule_id, "subject_id": case_id,
                        "note": "Assessed as implemented (confidence 0.90): the rule covers this"}}},
                inputs={})

            self.assertEqual(result["verdict"], "confirmed")
            self.assertTrue(result["confirmed"])

            prompt = agent.prompts[0]
            self.assertIn("apply_interest_first", prompt)
            self.assertIn("COULD NOT READ", prompt)
            self.assertIn("missing_fn", prompt)
        finally:
            mp.undo()


if __name__ == "__main__":
    unittest.main()
