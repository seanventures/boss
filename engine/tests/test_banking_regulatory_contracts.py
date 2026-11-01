"""Every step of the KG investigation template and its regulatory instance declares a contract
(design §4.1, §5.3), and a full run of `banking_regulatory_compliance` — real catalogue spec,
real runtime (claim_step/execute_step/complete_step), a scripted role agent in place of the
model — writes every required kind the contracts declare, each stamped with the run's
provenance, and completes.

Runs against a real, disposable postgres (DATABASE_URL), like
test_banking_regulatory_compliance_real_runtime.py.
"""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge import banking_regulatory_pipeline as pipeline
from boss_engine.services.knowledge import context_graph, ontology_packs
from boss_engine.services.knowledge.documents import add_document
from boss_engine.services.knowledge.step_contracts import (
    contract_assignment_problems, contract_shape_problems, parse_contract)
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

KEY = "banking_regulatory_compliance"
WORKER = "worker-contracts"
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


def _project(conn, label: str) -> str:
    suffix = uuid.uuid4().hex[:8]
    org, ws, project = f"org_rc_{suffix}", f"ws_rc_{suffix}", f"p_rc_{label}_{suffix}"
    conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'RC')"), {"i": org})
    conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'RC')"),
                 {"i": ws, "o": org})
    conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                      "(:i, :w, 'RC', :s)"), {"i": project, "w": ws, "s": project})
    return project


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            project = _project(conn, "pack")
            ontology_packs.set_project_pack(conn, project_id=project, pack_id="banking-regulatory")
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _entry(key: str) -> dict:
    return next(e for e in catalogue.CATALOGUE if e["key"] == key)


# ── every step declares its footprint, and it validates against the project's ontology ───────

@pytest.mark.parametrize("key", ["kg_investigation_pipeline", KEY])
def test_every_step_carries_a_valid_contract(key):
    for step in _entry(key)["steps"]:
        assert isinstance(step.get("contract"), dict), f"{key}.{step['id']} has no contract"
        assert contract_shape_problems(step) == [], (key, step["id"])
        contract = parse_contract(step["id"], step["contract"])
        assert contract.trigger and contract.handoff, (key, step["id"])


def test_regulatory_contract_kinds_are_granted_by_the_pack(connection):
    for step in _entry(KEY)["steps"]:
        assert contract_assignment_problems(connection, connection.project_id, step) == [], step["id"]


def test_a_project_without_the_pack_still_seeds_the_rest_of_the_catalogue(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            project = _project(conn, "bare")
            result = catalogue.seed(conn, project, actor="test")
            by_key = {row["key"]: row for row in result["installed"]}
            assert by_key[KEY].get("skipped"), "a project without the pack must skip it, not fail"
            assert by_key["kg_investigation_pipeline"]["version_id"], (
                "the template's contracts use core kinds only and publish anywhere")
            # Setting the pack installs what it unlocks, and only that.
            ontology_packs.set_project_pack(conn, project_id=project, pack_id="banking-regulatory")
            unlocked = catalogue.seed_unlocked_by_pack(conn, project, "banking-regulatory")
            assert [r["key"] for r in unlocked["installed"]] == [KEY]
            assert unlocked["installed"][0]["version_id"], unlocked
        finally:
            transaction.rollback()


# ── a full run writes every required kind, with provenance, and completes ────────────────────

class ScriptedRoles:
    """Answers every role prompt with one judgement carrying each role's fields."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        assert read_only is True
        self.calls += 1
        body = json.dumps(self.payload)
        return {"summary": f"{pipeline._MARK_OPEN}\n{body}\n{pipeline._MARK_CLOSE}\n"}


def _seed_graph(c, project):
    doc = add_document(
        c, project_id=project, title="Reg Z repayment rule",
        source_url="https://www.ecfr.gov/x/1026.36", citation="12 CFR 1026.36(c)",
        clauses=[{"section": "1026.36(c)(1)(i)",
                  "text": "A periodic payment must be credited as of the date of receipt.",
                  "citation": "12 CFR 1026.36(c)(1)(i)", "url": "https://www.ecfr.gov/x/1026.36"}],
        actor_kind="person", actor_ref="compliance.lead@example.com")
    clause_id = doc["clause_ids"][0]
    rule_id = STORE.upsert_node(
        c, project_id=project, kind="rule", node_key="rule:rc:repayment-order",
        name="Repayment allocation order", body="Apply interest before principal.",
        trust_tier="governed", actor_kind="person", actor_ref="test")
    symbol_id = STORE.upsert_node(
        c, project_id=project, kind="symbol",
        node_key="symbol:fineract:LoanPaymentAllocationRule.java:LoanPaymentAllocationRule",
        name="LoanPaymentAllocationRule", body="allocates a payment", trust_tier="observed",
        actor_kind="system")
    incident_id = STORE.upsert_node(
        c, project_id=project, kind="incident", node_key="incident:INC-2088", name="INC-2088",
        body="250.00 paid down principal in full; 218.60 was due", trust_tier="observed",
        actor_kind="system", attrs={"incident_id": "INC-2088"})
    STORE.upsert_edge(c, project_id=project, src_id=clause_id, dst_id=rule_id,
                      kind="implemented_by", actor_kind="system")
    STORE.upsert_edge(c, project_id=project, src_id=rule_id, dst_id=symbol_id,
                      kind="evidenced_by", actor_kind="system")
    STORE.upsert_edge(c, project_id=project, src_id=incident_id, dst_id=rule_id,
                      kind="about", actor_kind="system")
    STORE.upsert_edge(c, project_id=project, src_id=incident_id, dst_id=symbol_id,
                      kind="found_in", actor_kind="system")
    return doc["document_id"], clause_id, rule_id


def _drive(c, project, run_id, spec_steps):
    ctx = SimpleNamespace(settings=SimpleNamespace(), engine=None)
    for _ in range(80):
        claimed = workflows.claim_step(c, project_id=project, run_id=run_id, worker_id=WORKER,
                                       lease_seconds=600)
        if claimed is None:
            waiting = c.execute(text(
                "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r "
                "AND status = 'waiting' AND decision_id IS NOT NULL"), {"r": run_id}).scalar()
            if not waiting:
                return
            c.execute(text("UPDATE decisions SET status = 'approved', "
                           "approved_by = 'compliance.officer@example.com', "
                           "resolution_note = 'fix the allocation order', approved_at = now() "
                           "WHERE id = :d"), {"d": waiting})
            workflows.advance_run(c, project, run_id)
            continue
        step = spec_steps[claimed["step_id"]]
        if claimed["step_kind"] in ("capability", "deterministic"):
            output = runner.execute_step(
                c, ctx, project_id=project, actor="test", step=step, spec_steps=spec_steps,
                context={**runner._run_context(c, run_id), **runner.item_context(claimed)})
        else:
            output = {"note": f"{claimed['step_id']} done"}
        workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                worker_id=WORKER, output=output, evidence=[])
    raise AssertionError("the run did not settle in 80 claims")


def test_a_full_run_writes_every_required_kind_with_provenance(connection, monkeypatch):
    c, project = connection, connection.project_id
    document_id, clause_id, rule_id = _seed_graph(c, project)

    roles = ScriptedRoles({
        "status": "implemented", "confidence": 0.9, "citation": rule_id,
        "rationale": "the allocation rule covers the clause but INC-2088 shows it misapplied",
        "family": "payment_allocation", "verdict": "confirmed",
        "reason": "the source applies the whole payment to principal",
        "change_brief": "Apply interest first in LoanPaymentAllocationRule.",
        "test_ref": "LoanPaymentAllocationRuleTest", "title": "Interest-first allocation"})
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (roles, "scripted"))
    monkeypatch.setattr(pipeline, "_repo_reader", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "_read_symbol_source", lambda reader, symbol: {
        "path": "LoanPaymentAllocationRule.java", "start": 1, "end": 3,
        "content": "principal -= payment;"})

    result = catalogue.seed(c, project, actor="test")
    version_id = next(r["version_id"] for r in result["installed"] if r["key"] == KEY)
    assert version_id, result
    spec = c.execute(text("SELECT spec FROM workflow_versions WHERE id = :v"),
                     {"v": version_id}).scalar()
    spec_steps = {s["id"]: s for s in spec["steps"]}
    assert all(s.get("contract") for s in spec["steps"]), "the published spec keeps the contracts"

    run = workflows.start_run(
        c, project_id=project, actor="test",
        input_data={"document_id": document_id, "subject_id": clause_id,
                    "directive": "check the repayment order"},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version_id)
    run_id = run["id"]
    _drive(c, project, run_id, spec_steps)

    status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                       {"r": run_id}).scalar()
    failed = c.execute(text("SELECT step_id, last_error FROM workflow_step_runs "
                            "WHERE workflow_run_id = :r AND status <> 'succeeded'"),
                       {"r": run_id}).mappings().all()
    assert status == "completed", (status, [dict(f) for f in failed])
    assert roles.calls >= 4, "the four role stages asked the (scripted) agent"

    written = context_graph.step_writes(c, project, run_id=run_id)
    by_step: dict[str, list[dict]] = {}
    for node in written:
        by_step.setdefault(node["step_id"], []).append(node)

    for step in spec["steps"]:
        contract = parse_contract(step["id"], step["contract"])
        for spec_write in contract.writes:
            if not spec_write.required:
                continue
            nodes = [n for n in by_step.get(step["id"], []) if n["kind"] == spec_write.kind]
            assert nodes, f"step {step['id']} never wrote required {spec_write.kind}"
            if spec_write.decision_type:
                allowed = spec_write.decision_type.split("|")
                assert any(n["decision_type"] in allowed for n in nodes), (step["id"], nodes)

    decisions = [n for n in written if n["kind"] == "agent_decision"]
    assert {d["decision_type"] for d in decisions} >= {
        "ClauseIntake", "ContextAssembled", "HistoricalPatternMatched", "Assessment", "Routed",
        "Confirmed", "Approved", "PrecedentRecorded"}
    for node in decisions:
        assert node["run_id"] == run_id and node["step_id"], node
        assert node["workflow_version"] == version_id, node
        assert node["agent_fingerprint"], node
    approved = next(d for d in decisions if d["decision_type"] == "Approved")
    assert "compliance.officer@example.com" in approved["statement"]
    deterministic = {d["step_id"]: d["agent_fingerprint"] for d in decisions}
    assert deterministic["context_enrichment"] == "deterministic"


def test_a_required_write_the_step_only_claims_fails_after_one_repair_round(connection):
    """A step that names a node id it never wrote has not satisfied its contract."""
    c, project = connection, connection.project_id
    steps = [{"id": "remediation_package", "kind": "capability",
              "binding": {"capability": "remediation_package", "inputs": {}},
              "contract": catalogue.REGULATORY_CONTRACTS["remediation_package"], "next": []}]
    definition = workflows.create_definition(
        c, project_id=project, workflow_key=f"rc_{uuid.uuid4().hex[:6]}", name="rc",
        description=None, actor="test", namespace="product")
    version = workflows.publish_version(
        c, project_id=project, definition_id=definition["id"],
        spec={"entry_step": "remediation_package", "steps": steps}, actor="test")
    run = workflows.start_run(c, project_id=project, actor="test", input_data={},
                              dedupe_key=f"dk_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=version["id"])
    claimed = workflows.claim_step(c, project_id=project, run_id=run["id"], worker_id=WORKER,
                                   lease_seconds=600)
    lie = {"produced": {"remediation_package": {"node_id": "kgn_never_written"}}}
    first = workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                    worker_id=WORKER, output=lie, evidence=[])
    assert first["status"] == "ready", "one repair round first"
    again = workflows.claim_step(c, project_id=project, run_id=run["id"], worker_id=WORKER,
                                 lease_seconds=600)
    with pytest.raises(workflows.WorkflowValidationError):
        workflows.complete_step(c, project_id=project, step_run_id=again["id"],
                                worker_id=WORKER, output=lie, evidence=[])
