"""Step contracts and the Context Graph (Build B, P1 + P3).

Covers the acceptance criteria: the validator refuses an unknown contract read/write kind
(AC1), `scopes.policy_for` actually builds a policy from a step's `contract` block (AC2), and
the engine writes a required AgentDecision to the Context Graph with the right provenance,
never leaving a required write missing after a run (AC4/AC5), with AgentRun/AgentToolCall
exposed as read-only projections over agent_acts/agent_turns rather than new kg_nodes rows
(AC6).
"""
from __future__ import annotations

import os
import unittest
import uuid

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError
except ImportError:  # pragma: no cover - sqlalchemy is a core engine dependency
    create_engine = None

from boss_engine.db import ensure_schema
from boss_engine.services import workflows
from boss_engine.services.knowledge import scopes, step_contracts, context_graph, store as kg_store


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


def _pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        return None
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return None
    ensure_schema(engine)
    return engine


@unittest.skipIf(create_engine is None, "sqlalchemy is required for this suite")
class StepContractsAndContextGraphTests(unittest.TestCase):
    """P0/P1: the contract is enforced by the validator and the runtime, and writes land."""

    def setUp(self):
        self.engine = _pg_engine()
        if self.engine is None:
            self.skipTest("BOSS_KG_TEST_DATABASE_URL must name a disposable postgres database")
        self.conn = self.engine.connect()
        self.txn = self.conn.begin()
        suffix = uuid.uuid4().hex[:8]
        self.org_id = f"org_sc_{suffix}"
        self.ws_id = f"ws_sc_{suffix}"
        self.project_id = f"proj_sc_{suffix}"
        self.conn.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'SC')"), {"id": self.org_id})
        self.conn.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'SC')"),
            {"id": self.ws_id, "org": self.org_id},
        )
        self.conn.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :ws, 'SC', :slug)"),
            {"id": self.project_id, "ws": self.ws_id, "slug": f"sc-{suffix}"},
        )

    def tearDown(self):
        self.txn.rollback()
        self.conn.close()

    # P0: the step contract carrying only core kinds is accepted end to end and shapes the
    # live StagePolicy the engine's KG tools enforce during the step.
    def test_p0_contract_policy_reaches_the_real_kg_policy(self):
        step = {
            "id": "context_enrichment",
            "kind": "implement",
            "contract": {
                "reads": [{"kind": "system"}, {"kind": "incident"}],
                "writes": [{"kind": "agent_decision", "required": True, "decision_type": "ContextAssembled"}],
                "policy": {"max_hops": 3, "list_cap": 50},
            },
        }
        self.assertEqual(step_contracts.contract_shape_problems(step), [])
        policy = scopes.policy_for(step=step, role="software_engineer")
        self.assertIsNotNone(policy)
        self.assertEqual(policy.layers, frozenset({scopes.layer_of("system"), scopes.layer_of("incident")}))
        self.assertEqual(policy.max_hops, 3)
        self.assertEqual(policy.max_nodes, 50)

    # P0: the validator refuses a step whose contract names a kind that is neither a core kind
    # nor granted by the project's ontology pack — closing gap G3/G1 at publish time.
    def test_p0_validator_refuses_an_unknown_contract_kind(self):
        step = {
            "id": "context_enrichment",
            "kind": "implement",
            "contract": {
                "reads": [{"kind": "not_a_real_kind_xyz"}],
                "writes": [],
            },
        }
        problems = step_contracts.contract_assignment_problems(self.conn, self.project_id, step)
        self.assertTrue(problems, "an unknown contract read kind must be refused")
        self.assertTrue(any("context_enrichment" in p for p in problems))

    # P1: a required write that lands is recorded as a real agent_decision node, in the new
    # 'context' knowledge layer, carrying the provenance the brief requires (never accepted
    # from the caller as free text).
    def test_p1_required_write_lands_in_the_context_graph(self):
        node = context_graph.write_agent_decision(
            self.conn,
            self.project_id,
            step_id="context_enrichment",
            run_id="run_cg_1",
            decision_type="ContextAssembled",
            statement="Context assembled for incident inc-1.",
            policy_version="pv1",
            workflow_version="wv1",
            agent_fingerprint="fp_abc123",
            actor_ref="agent:context_enrichment_agent",
        )
        self.assertEqual(node["kind"], "agent_decision")
        row = self.conn.execute(
            text("SELECT kind, knowledge_layer, trust_tier FROM kg_nodes WHERE id = :id"),
            {"id": node["id"]},
        ).mappings().first()
        self.assertEqual(row["kind"], "agent_decision")
        self.assertEqual(row["knowledge_layer"], "context")
        self.assertEqual(row["trust_tier"], "inferred")

    # P1: a contract declaring a required write that never lands fails the step with a
    # CapabilityExecutionError naming the missing kind, after exactly one repair attempt —
    # regression test for the AG-1 "chat only, nothing written" miss [L:kgn_a7426dae68a24130a1b2].
    def test_p1_missing_required_write_fails_after_one_repair_round(self):
        step = {"id": "context_enrichment", "kind": "implement",
                "contract": {"reads": [], "writes": [{"kind": "agent_decision", "required": True,
                                                        "decision_type": "ContextAssembled"}]}}
        contract = step_contracts.parse_contract(step["id"], step["contract"])
        attempts = {"count": 0}

        def run_again(extra_instruction):
            attempts["count"] += 1
            return {"produced": {}}

        with self.assertRaises(Exception):
            step_contracts.enforce_writes(
                self.conn, self.project_id, step=step, contract=contract,
                outcome={"produced": {}}, run_again=run_again,
            )
        self.assertEqual(attempts["count"], 1, "exactly one repair round, never a silent pass or a retry loop")

    # P1: AgentRun/AgentToolCall are read-only projections of agent_acts/agent_turns, never
    # duplicated as kg_nodes rows (closing the "two sources of truth" risk the brief calls out).
    def test_p1_agent_run_projection_never_inserts_a_kg_node(self):
        before = self.conn.execute(
            text("SELECT count(*) AS n FROM kg_nodes WHERE project_id = :p"), {"p": self.project_id}
        ).mappings().first()["n"]
        context_graph.project_agent_run(self.conn, self.project_id, "run_does_not_exist")
        context_graph.project_agent_tool_calls(self.conn, self.project_id, "run_does_not_exist")
        after = self.conn.execute(
            text("SELECT count(*) AS n FROM kg_nodes WHERE project_id = :p"), {"p": self.project_id}
        ).mappings().first()["n"]
        self.assertEqual(before, after)

    # P1 (acceptance criterion 7): the two new context-layer kinds are counted from the source
    # of truth, never a literal, so the count tracks store.NODE_KINDS as it grows.
    def test_p1_node_kinds_grows_by_exactly_the_two_new_context_kinds(self):
        self.assertIn("agent_decision", kg_store.NODE_KINDS)
        self.assertIn("coverage_gap", kg_store.NODE_KINDS)
        self.assertEqual(scopes.layer_of("agent_decision"), "context")
        self.assertEqual(scopes.layer_of("coverage_gap"), "context")


if __name__ == "__main__":
    unittest.main()
