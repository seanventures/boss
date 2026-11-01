"""KG P7b: `banking_regulatory_compliance` runs end to end on a REAL project, driven by the real
workflow runtime (claim_step/complete_step, as a worker would), not P5b's scripted test harness.

A dry run (wfr_8b71d37ca6f04edfb593) showed two generic runtime bugs and one deterministic-step
gap:

* [P0] A `bounded_loop` went straight to `succeeded` without ever running its body once — the
  evaluate-first order is wrong for any workflow using `bounded_loop` (`evidence_loop` here,
  and `kg_investigation_pipeline`'s equivalent). `test_p0_*` proves the generic fix with a
  synthetic spec, the same shape test_workflow_runtime.py's bounded_loop test already uses.
* [P1] A `for_each` body step (`lane_confirmation`, under `lanes`) was never claimable by a
  worker. `test_p1_the_real_lanes_for_each_body_is_claimable` drives the REAL
  `banking_regulatory_compliance` catalogue entry (not a synthetic spec) up to that point.
* [P1] `historical_pattern`'s graph write must be an upsert, not a one-off insert: re-running it
  must not duplicate the `correlates_with` edge it already wrote.
* [P1] The catalogue must declare which ITSM MCP tool `historical_pattern` is allowed to call
  (contract.calls), so the step can query it without tripping contract enforcement.

These run against a real, disposable postgres (DATABASE_URL), like test_kg_investigation_pipeline_template.py.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge import graph_steps
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

TEMPLATE_KEY = "banking_regulatory_compliance"
WORKER = "worker-a"


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
        org = f"org_p7b_{uuid.uuid4().hex[:8]}"
        ws = f"ws_p7b_{uuid.uuid4().hex[:8]}"
        project = f"p_p7b_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'P7b')"), {"i": org})
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'P7b')"),
                {"i": ws, "o": org})
            conn.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "(:i, :w, 'P7b', :s)"), {"i": project, "w": ws, "s": project})
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _entry() -> dict:
    for entry in catalogue.CATALOGUE:
        if entry.get("key") == TEMPLATE_KEY:
            return entry
    raise AssertionError(f"'{TEMPLATE_KEY}' is not in workflow_catalogue.CATALOGUE")


def _claim(c, project_id, run_id):
    return workflows.claim_step(c, project_id=project_id, run_id=run_id, worker_id=WORKER,
                                lease_seconds=600)


def _complete(c, project_id, claimed, output):
    workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, output=output, evidence=[])


def _finish(c, project_id, run_id, step_id, output=None):
    claimed = _claim(c, project_id, run_id)
    assert claimed is not None and claimed["step_id"] == step_id, claimed
    _complete(c, project_id, claimed, output if output is not None else {step_id: True})
    return claimed


def _publish_and_start(c, project_id, steps, entry_step, input_data=None):
    key = f"p7b_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product")
    version = workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry_step, "steps": steps}, actor="test")
    run = workflows.start_run(
        c, project_id=project_id, actor="test", input_data=input_data or {},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version["id"])
    return run["id"]


# ── P0: a bounded_loop runs its body at least once before it ever evaluates (criterion 1) ────

def test_p0_a_bounded_loop_runs_its_body_once_before_evaluating(connection):
    c, project = connection, connection.project_id
    steps = [
        {"id": "a", "kind": "task", "next": ["loop"]},
        {"id": "loop", "kind": "bounded_loop", "body": "body", "max_iterations": 2,
         "expression": {"path": "steps.body.output.again", "op": "eq", "value": True},
         "next": ["done"]},
        {"id": "body", "kind": "task", "next": []},
        {"id": "done", "kind": "task"},
    ]
    run_id = _publish_and_start(c, project, steps, "a")
    _finish(c, project, run_id, "a")

    # The body must run (and be claimable) BEFORE the loop ever reaches a terminal status —
    # the bug reported in wfr_8b71d37ca6f04edfb593 left `context_enrichment` pending forever
    # while `evidence_loop` had already gone to `succeeded` with iteration 0.
    body_claim = _claim(c, project, run_id)
    assert body_claim is not None, "the loop body was never made claimable on first reach"
    assert body_claim["step_id"] == "body"
    loop_row = c.execute(text(
        "SELECT status, output FROM workflow_step_runs WHERE workflow_run_id=:r "
        "AND step_id='loop' ORDER BY iteration DESC LIMIT 1"), {"r": run_id}).mappings().first()
    assert loop_row["status"] not in ("succeeded", "failed"), (
        "the loop finished before its body ever ran")

    _complete(c, project, body_claim, {"again": False})
    _finish(c, project, run_id, "done")

    loop_rows = c.execute(text(
        "SELECT iteration, output FROM workflow_step_runs WHERE workflow_run_id=:r "
        "AND step_id='loop' ORDER BY iteration"), {"r": run_id}).mappings().all()
    assert any(row["output"] and row["output"].get("iteration", 0) >= 1 for row in loop_rows), (
        "the loop's final output must record at least one completed iteration of its body")
    run_row = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                        {"r": run_id}).mappings().one()
    assert run_row["status"] == "completed"


# ── P1: the real `lanes` for_each body is claimable by a worker (criterion 2) ─────────────────

def test_p1_the_real_lanes_for_each_body_is_claimable(connection):
    c, project = connection, connection.project_id
    entry = _entry()
    steps = entry["steps"]
    step_ids = {s["id"] for s in steps}
    for required in ("evidence_loop", "context_enrichment", "historical_pattern",
                     "compliance_assessment", "compliance_orchestration", "lanes",
                     "lane_confirmation", "human_approval"):
        assert required in step_ids, f"catalogue entry is missing step '{required}'"

    lanes = next(s for s in steps if s["id"] == "lanes")
    assert lanes["kind"] == "for_each" and lanes["body"] == "lane_confirmation"

    # The steps' contracts name the banking-regulatory pack's kinds (compliance_case, clause…),
    # which a project grants only once it has set that pack.
    from boss_engine.services.knowledge import ontology_packs
    ontology_packs.set_project_pack(c, project_id=project, pack_id="banking-regulatory")

    def decided(decision_type, **more):
        # Each step's contract requires its writes; the engine persists these itself.
        return {"produced": {"agent_decision": {"decision_type": decision_type,
                                                "statement": decision_type}, **more}}

    run_id = _publish_and_start(
        c, project, steps, entry["steps"][0]["id"] if entry["steps"] else "clause_intake",
        {"document_id": "doc_1", "subject_id": "kgn_subject_1", "directive": "check"})

    # Drive the run up through compliance_orchestration without caring what each step's own
    # capability logic does — we only assert that once `lanes` has an item to fan out,
    # `lane_confirmation` becomes claimable, which is exactly what the dry run showed broken
    # (it sat `ready` for 30+ minutes, never leased).
    _finish(c, project, run_id, "clause_intake",
            decided("ClauseIntake", compliance_case={"statement": "case opened"}))
    _finish(c, project, run_id, "context_enrichment",
            {"missing_kinds": [], **decided("ContextAssembled")})
    _finish(c, project, run_id, "historical_pattern",
            {"matched": [], **decided("HistoricalPatternMatched")})
    _finish(c, project, run_id, "compliance_assessment", decided("Assessment"))
    _finish(c, project, run_id, "compliance_orchestration",
           {"finding_families": ["payment_allocation"], **decided("Routed")})

    lane_claim = _claim(c, project, run_id)
    assert lane_claim is not None, (
        "lane_confirmation was never made claimable after lanes received an item")
    assert lane_claim["step_id"] == "lane_confirmation"


def test_the_approval_card_names_the_rule_and_clause_and_shows_the_package(connection):
    """The Compliance Officer's card says WHICH rule against WHICH clause (the graph nodes'
    names, never their kgn_ ids) and the remediation package's title and owner — not
    "Workflow wfr_… is waiting at decision step human_approval"."""
    c, project = connection, connection.project_id
    from boss_engine.services.knowledge import ontology_packs
    ontology_packs.set_project_pack(c, project_id=project, pack_id="banking-regulatory")
    store = PostgresKnowledgeStore()
    rule = store.upsert_node(
        c, project_id=project, kind="rule", node_key="rule:card:repayment",
        name="Repayment-like transaction classification", body="", trust_tier="governed",
        actor_kind="person")
    document = store.upsert_node(
        c, project_id=project, kind="document", node_key="document:card:regz",
        name="Regulation Z, 12 CFR 1026.36(c)(1) — Payment processing", body="",
        trust_tier="governed", actor_kind="person",
        attrs={"citation": "12 CFR 1026.36(c)(1)"})

    def decided(decision_type, **more):
        return {"produced": {"agent_decision": {"decision_type": decision_type,
                                                "statement": decision_type}, **more}}

    entry = _entry()
    run_id = _publish_and_start(c, project, entry["steps"], "clause_intake",
                                {"document_id": document, "subject_id": rule})
    _finish(c, project, run_id, "clause_intake",
            decided("ClauseIntake", compliance_case={"statement": "case opened"}))
    _finish(c, project, run_id, "context_enrichment",
            {"missing_kinds": [], **decided("ContextAssembled")})
    _finish(c, project, run_id, "historical_pattern",
            {"matched": [], **decided("HistoricalPatternMatched")})
    _finish(c, project, run_id, "compliance_assessment", decided("Assessment"))
    _finish(c, project, run_id, "compliance_orchestration",
            {"finding_families": ["payment_allocation"], **decided("Routed")})
    _finish(c, project, run_id, "lane_confirmation", decided("Confirmed"))
    workflows.advance_run(c, project, run_id)
    note = "Credit periodic payments as of receipt — ready for Loan Servicing Team's approver."
    _finish(c, project, run_id, "remediation_package",
            {"note": note, "produced": {"remediation_package": {
                "statement": "change brief"}}})
    workflows.advance_run(c, project, run_id)
    card = c.execute(text(
        "SELECT d.question, d.recommendation FROM decisions d JOIN workflow_step_runs s ON "
        "s.decision_id = d.id WHERE s.workflow_run_id = :r AND s.step_id = 'human_approval'"),
        {"r": run_id}).mappings().one()
    gate = card["recommendation"]["gate"]
    assert gate["subject"] == ("Repayment-like transaction classification against "
                               "12 CFR 1026.36(c)(1)")
    assert gate["summary"] == note
    said = card["question"] + card["recommendation"]["reason"] + str(gate)
    assert "wfr_" not in said and "kgn_" not in said and "human_approval" not in said


# ── P1: historical_pattern's graph write is an upsert, never a duplicate (criterion 3) ────────

def test_p1_historical_pattern_correlates_without_duplicating_on_a_rerun(connection):
    c, project = connection, connection.project_id
    store = PostgresKnowledgeStore()
    subject_id = store.upsert_node(
        c, project_id=project, kind="rule", node_key="rule:p7b:repayment-order",
        name="Repayment-like transaction classification", body="rule body",
        trust_tier="governed", actor_kind="person")
    code_id = store.upsert_node(
        c, project_id=project, kind="symbol", node_key="symbol:p7b:allocatePayment",
        name="allocatePayment", body="allocates a payment", trust_tier="observed",
        actor_kind="system")
    incident_id = store.upsert_node(
        c, project_id=project, kind="incident", node_key="incident:p7b:INC-2088",
        name="INC-2088", body="payment misapplied", trust_tier="observed", actor_kind="system")
    store.upsert_edge(c, project_id=project, src_id=subject_id, dst_id=code_id,
                      kind="implements", actor_kind="system")
    store.upsert_edge(c, project_id=project, src_id=incident_id, dst_id=code_id,
                      kind="implements", actor_kind="system")

    first = graph_steps.run_historical_pattern(
        c, project_id=project, run_id="run_1", step_id="historical_pattern",
        subject_id=subject_id)
    assert incident_id in first["matched"]

    second = graph_steps.run_historical_pattern(
        c, project_id=project, run_id="run_2", step_id="historical_pattern",
        subject_id=subject_id)
    assert incident_id in second["matched"]

    edges = c.execute(text(
        "SELECT count(*) FROM kg_edges WHERE project_id=:p AND src_id=:s AND dst_id=:d "
        "AND kind='correlates_with'"),
        {"p": project, "s": subject_id, "d": incident_id}).scalar()
    assert edges == 1, "a rerun of historical_pattern must upsert, not duplicate, the edge"


# ── P1: the catalogue declares the ITSM MCP tool historical_pattern is allowed to call ────────

def test_p1_historical_pattern_declares_its_itsm_mcp_call_in_the_catalogue():
    entry = _entry()
    step = next(s for s in entry["steps"] if s["id"] == "historical_pattern")
    calls = ((step.get("contract") or {}).get("calls")) or []
    itsm_tools = {c.get("tool") for c in calls if c.get("mcp") == "itsm"}
    assert itsm_tools & {"get_incident", "list_incidents"}, (
        "historical_pattern must declare the ITSM tool(s) it is allowed to call via "
        f"contract.calls, got {calls!r}")


# ── An outside agent's step is run by the real runner (2026-10-31) ───────────────────────────
# The runner leased an `external_agent` step, skipped it as "somebody else's step kind" and let
# it time out: P6a's dispatch was reachable only from tests that called it directly.

def test_p0_the_runner_runs_an_outside_agents_step(connection, monkeypatch):
    from types import SimpleNamespace

    from boss_engine.services import external_agent_runner
    from boss_engine.services import workflow_capability_runner as runner

    c, project = connection, connection.project_id
    calls = []

    def fake_run_step(conn, ctx, *, project_id, step, context):
        calls.append(step["id"])
        return {"kind": "external_agent", "status": "succeeded", "produced": {}}

    monkeypatch.setattr(external_agent_runner, "run_step", fake_run_step)
    c.execute(text("INSERT INTO external_agents (id, project_id, display_name, endpoint_url, "
                   "secret_ref, sponsor_principal_id, status, created_by) VALUES "
                   "('ext_test', :p, 'Bank agent', 'https://bank.example/agent', 'sref_test', "
                   "'ceo', 'active', 'test')"), {"p": project})
    run_id = _publish_and_start(c, project, [
        {"id": "bank_review", "kind": "external_agent",
         "captain": {"external_agent": "ext_test"}, "timeout_seconds": 60,
         "contract": {"writes": [{"kind": "agent_decision", "required": True,
                                  "decision_type": "bank_review"}]},
         "next": []}], "bank_review")
    runner.tick(c, SimpleNamespace(settings=None, engine=None), project_id=project)
    assert calls == ["bank_review"]
    status = c.execute(text("SELECT status FROM workflow_step_runs WHERE workflow_run_id = :r "
                            "AND step_id = 'bank_review'"), {"r": run_id}).scalar()
    assert status != "leased", status
