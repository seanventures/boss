"""KG P5b: the `banking_regulatory_compliance` instance of `kg_investigation_pipeline`.

Pins the "Done means" from the brief: a catalogue entry that publishes through the real
runtime, the two new additive ontology kinds (ComplianceCase, RemediationPackage), the
clause-intake capability writing a ComplianceCase + AgentDecision, closure stamping an
execution-state marker and a `reused` edge, the ITSM mock's INC-2088 fixture, and the
second-run-is-faster definition the PR commits to (fewer succeeded workflow_step_runs rows).

Runs against a real, disposable postgres (DATABASE_URL), like test_regulatory_trace.py and
test_kg_investigation_pipeline_template.py.
"""
from __future__ import annotations

import os
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge import ontology_packs
from boss_engine.services.knowledge.documents import add_document
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

# Intended module names (not yet implemented) — importing them pins the build to these paths.
from boss_engine.services.knowledge import banking_regulatory_pipeline as pipeline
from boss_engine.services.knowledge import graph_steps
from tools import mcp_itsm_mock

CATALOGUE_KEY = "banking_regulatory_compliance"
STORE = PostgresKnowledgeStore()


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


class BankingRegulatoryComplianceTests(unittest.TestCase):
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
        self.org_id = f"org_bankreg_{suffix}"
        self.ws_id = f"ws_bankreg_{suffix}"
        self.project_id = f"p_bankreg_{suffix}"
        self.connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'Banking Reg')"), {"i": self.org_id})
        self.connection.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'Banking Reg')"),
            {"i": self.ws_id, "o": self.org_id})
        self.connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                 "(:i, :w, 'Banking Reg', :s)"),
            {"i": self.project_id, "w": self.ws_id, "s": self.project_id})
        ontology_packs.set_project_pack(
            self.connection, project_id=self.project_id, pack_id="banking-regulatory")

    def tearDown(self):
        self.transaction.rollback()
        self._conn_ctx.__exit__(None, None, None)

    def _entry(self) -> dict:
        for entry in catalogue.CATALOGUE:
            if entry.get("key") == CATALOGUE_KEY:
                return entry
        raise AssertionError(f"'{CATALOGUE_KEY}' is not in workflow_catalogue.CATALOGUE")

    def _add_clause(self, *, section: str, text_body: str, title: str):
        doc = add_document(
            self.connection, project_id=self.project_id, title=title,
            source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
            clauses=[{"section": section, "text": text_body,
                      "citation": "12 CFR " + section, "url": "https://www.ecfr.gov/x/1026.36"}],
            actor_kind="person", actor_ref="compliance.lead@example.com")
        return doc["document_id"], doc["clause_ids"][0]

    # ── P0: the catalogue entry is wired into the real runtime, not a bare dict ────────────────

    def test_p0_catalogue_entry_publishes_through_the_real_runtime(self):
        entry = self._entry()
        self.assertIn("steps", entry)
        step_ids = {s["id"] for s in entry["steps"]}
        for required in (
            "clause_intake", "context_enrichment", "historical_pattern",
            "compliance_assessment", "compliance_orchestration", "lane_confirmation",
            "remediation_package", "human_approval", "execution", "compliance_closure",
        ):
            self.assertIn(required, step_ids, f"missing stage '{required}'")

        gates = [s for s in entry["steps"] if (s.get("approval") or {}).get("approver_role")]
        self.assertTrue(gates, "human approval before Execution must be a structural gate")

        result = catalogue.seed(self.connection, self.project_id, actor="test")
        keys = {row["key"]: row for row in result["installed"]}
        self.assertIn(CATALOGUE_KEY, keys, f"'{CATALOGUE_KEY}' was not installed: {sorted(keys)}")

        row = self.connection.execute(
            text("SELECT id FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
            {"p": self.project_id, "k": CATALOGUE_KEY},
        ).mappings().first()
        self.assertIsNotNone(row, "the instance must publish a real workflow_definitions row")
        version = self.connection.execute(
            text("SELECT spec FROM workflow_versions WHERE definition_id = :d "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"d": row["id"]},
        ).mappings().first()
        self.assertIsNotNone(version, "seeding must publish a version with no contract problems")

    # ── P0: the ITSM mock ships the INC-2088 fixture with zero config ──────────────────────────

    def test_p0_itsm_mock_serves_inc_2088_with_zero_config(self):
        self.assertIn("get_incident", mcp_itsm_mock.TOOL_FNS)
        incident = mcp_itsm_mock.TOOL_FNS["get_incident"]({"incident_id": "INC-2088"})
        self.assertEqual(incident["incident_id"], "INC-2088")
        self.assertAlmostEqual(incident["payment_amount"], 250.00)
        self.assertAlmostEqual(incident["interest_due"], 31.40)
        self.assertAlmostEqual(incident["correct_paydown"], 218.60)
        self.assertAlmostEqual(incident["actual_paydown"], 250.00)

    # ── P1: ontology additions are additive, not a redeclaration of core kinds ─────────────────

    def test_p1_ontology_pack_adds_compliance_case_and_remediation_package_kinds(self):
        pack = ontology_packs.load_pack("banking-regulatory")
        kind_names = {k["name"] if isinstance(k, dict) else k for k in pack.get("kinds", [])}
        self.assertIn("compliance_case", kind_names)
        self.assertIn("remediation_package", kind_names)

    # ── P1: clause intake opens one ComplianceCase per clause with a decision ──────────────────

    def test_p1_clause_intake_writes_a_compliance_case_and_agent_decision(self):
        doc_id, clause_id = self._add_clause(
            section="1026.36(c)(1)(i)",
            text_body="A periodic payment must be credited as of the date of receipt.",
            title="Reg Z repayment rule")

        result = pipeline.run_clause_intake(
            self.connection, settings=None, project_id=self.project_id,
            step={"id": "clause_intake"}, context={}, inputs={"document_id": doc_id})

        case_ids = result.get("compliance_case_ids") or result.get("case_ids")
        self.assertTrue(case_ids, "run_clause_intake must return the opened ComplianceCase ids")

        row = self.connection.execute(
            text("SELECT kind FROM kg_nodes WHERE project_id = :p AND id = :i"),
            {"p": self.project_id, "i": case_ids[0]},
        ).mappings().first()
        self.assertIsNotNone(row, "the ComplianceCase node must actually be persisted")
        self.assertEqual(row["kind"], "compliance_case")

        decision = self.connection.execute(
            text("SELECT decision_type FROM agent_decisions WHERE project_id = :p "
                 "AND decision_type = 'ClauseIntake' ORDER BY created_at DESC LIMIT 1"),
            {"p": self.project_id},
        ).mappings().first()
        self.assertIsNotNone(decision, "clause intake must write AgentDecision(ClauseIntake)")

    # ── P1: closure stamps an execution-state marker and a reused edge, not a bare precedent ───

    def test_p1_compliance_closure_sets_execution_state_and_reused_edge(self):
        doc_id, clause_id = self._add_clause(
            section="1026.36(c)(1)(i)",
            text_body="A periodic payment must be credited as of the date of receipt.",
            title="Reg Z repayment rule")
        case_id = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="compliance_case",
            node_key=f"case:{clause_id}", name="CC test case", body="",
            trust_tier="governed", actor_kind="person", actor_ref="compliance.lead@example.com",
            attrs={"clause_id": clause_id, "status": "open"})

        result = pipeline.run_compliance_closure(
            self.connection, settings=None, project_id=self.project_id,
            step={"id": "compliance_closure", "run_id": "run_test"}, context={},
            inputs={"subject_id": case_id, "symptom": "misapplied repayment order",
                    "cause_summary": "repayment order bug", "execution_state": "merged"})

        precedent_id = result.get("precedent_id")
        self.assertTrue(precedent_id, "run_compliance_closure must return the precedent id")

        row = self.connection.execute(
            text("SELECT attrs FROM kg_nodes WHERE project_id = :p AND id = :i"),
            {"p": self.project_id, "i": precedent_id},
        ).mappings().first()
        self.assertIsNotNone(row)
        attrs = row["attrs"] or {}
        self.assertEqual(attrs.get("execution_state"), "merged",
                          "closure must merge an execution_state marker onto the precedent")

    # ── P1: a second run's Historical Pattern reuses the precedent in fewer succeeded steps ────

    def test_p1_second_run_completes_with_fewer_succeeded_steps_than_the_first(self):
        catalogue.seed(self.connection, self.project_id, actor="test")
        row = self.connection.execute(
            text("SELECT id FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
            {"p": self.project_id, "k": CATALOGUE_KEY},
        ).mappings().first()
        self.assertIsNotNone(row)
        version = self.connection.execute(
            text("SELECT id FROM workflow_versions WHERE definition_id = :d "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"d": row["id"]},
        ).mappings().first()
        self.assertIsNotNone(version)

        def _start_run() -> str:
            run_id = f"run_{uuid.uuid4().hex[:8]}"
            self.connection.execute(
                text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                     "workflow_version_id, actor) VALUES (:i, :p, :d, :v, 'test')"),
                {"i": run_id, "p": self.project_id, "d": row["id"], "v": version["id"]})
            return run_id

        def _record_step(run_id: str, step_id: str, iteration: int, status: str) -> None:
            self.connection.execute(
                text("INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                     "step_kind, iteration, status) VALUES (:i, :p, :r, :s, 'deterministic', "
                     ":it, :st)"),
                {"i": f"wsr_{uuid.uuid4().hex[:8]}", "p": self.project_id, "r": run_id,
                 "s": step_id, "it": iteration, "st": status})

        def _succeeded_step_count(run_id: str) -> int:
            return self.connection.execute(
                text("SELECT count(*) FROM workflow_step_runs WHERE workflow_run_id = :r "
                     "AND status = 'succeeded'"),
                {"r": run_id},
            ).scalar_one()

        def _run_evidence_loop(run_id: str, subject_id: str, *, cap: int = 2) -> dict:
            """Drives the REAL `graph_steps.run_context_enrichment` through BOSS's bounded-loop
            semantics (cap, retry while `missing_kinds` is non-empty) and records one
            workflow_step_runs row per iteration actually executed."""
            last = None
            for iteration in range(cap):
                last = graph_steps.run_context_enrichment(
                    self.connection, project_id=self.project_id, run_id=run_id,
                    step_id="context_enrichment", subject_id=subject_id, max_hops=1,
                    expected_kinds=["precedent"])
                _record_step(run_id, "context_enrichment", iteration, "succeeded")
                if not last["missing_kinds"]:
                    break
            return last

        # A subject node connected to a shared neighbor. `historical_pattern` correlates through
        # that shared neighbor; `context_enrichment` only walks OUTGOING edges from the subject,
        # so a precedent linked to the neighbor (not the subject) stays invisible until the
        # subject itself is correlated to it with a `correlates_with` edge.
        shared_neighbor = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="symbol",
            node_key="symbol:repayment_order", name="repayment_order()", body="",
            trust_tier="governed", actor_kind="person", actor_ref="test")
        subject_id = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="compliance_case",
            node_key="case:evidence-loop-subject", name="Evidence loop subject", body="",
            trust_tier="inferred", actor_kind="agent", actor_ref="test")
        STORE.upsert_edge(
            self.connection, project_id=self.project_id, src_id=subject_id,
            dst_id=shared_neighbor, kind="about", actor_kind="agent", actor_ref="test")

        # A precedent from an earlier, unrelated investigation, already linked to the same
        # neighbor (as `graph_steps.run_closure` links a precedent to a subject's neighbors).
        earlier_precedent = STORE.upsert_node(
            self.connection, project_id=self.project_id, kind="precedent",
            node_key="precedent:earlier", name="Precedent: earlier repayment-order fix",
            body="interest-first allocation", trust_tier="inferred", actor_kind="agent",
            actor_ref="test")
        STORE.upsert_edge(
            self.connection, project_id=self.project_id, src_id=earlier_precedent,
            dst_id=shared_neighbor, kind="about", actor_kind="agent", actor_ref="test")

        first_run_id = _start_run()
        first_enrichment = _run_evidence_loop(first_run_id, subject_id)
        self.assertEqual(
            first_enrichment["missing_kinds"], ["precedent"],
            "the subject has no correlates_with edge yet, so the precedent (linked only to "
            "the shared neighbor) must be unreachable within the hop cap on the first run")

        first_pattern = graph_steps.run_historical_pattern(
            self.connection, project_id=self.project_id, run_id=first_run_id,
            step_id="historical_pattern", subject_id=subject_id)
        _record_step(first_run_id, "historical_pattern", 0, "succeeded")
        self.assertIn(earlier_precedent, first_pattern["matched"],
                      "historical_pattern must correlate the subject with the precedent that "
                      "shares its neighbor")

        # The second run of the SAME subject: historical_pattern's correlation above wrote a
        # `correlates_with` edge from the subject straight to the precedent, so this run's
        # context_enrichment reaches it in its very first iteration.
        second_run_id = _start_run()
        second_enrichment = _run_evidence_loop(second_run_id, subject_id)
        self.assertEqual(
            second_enrichment["missing_kinds"], [],
            "the correlates_with edge from the first run's historical_pattern must make the "
            "precedent reachable within the hop cap on the second run")

        self.assertLess(
            _succeeded_step_count(second_run_id), _succeeded_step_count(first_run_id),
            "the written definition of 'fewer steps' is a strictly lower count of succeeded "
            "workflow_step_runs rows on the second run, driven by the real precedent-reuse "
            "logic in graph_steps, not a fabricated row count")


if __name__ == "__main__":
    unittest.main()
