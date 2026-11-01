"""The consultant's agent design in BOSS (design §4.1, §4.2, §4.6; conformance P0 2/3/5/6, P1 7/8).

* A contract's numbered `steps` and `design` cards validate exactly as documented: a step reads,
  writes and calls only a subset of the contract's own, and unknown keys are still refused.
* A stage's design goes not reviewed → in review → locked, only a person locks it, Revise bumps
  the reference, and a publish that changes a locked stage's contract is refused.
* The Knowledge Graph version counts approved change sets; the Context Graph version is the
  pack's; both are stamped on every AgentDecision.
* A step's decision is linked by stored edges to what it consulted, cited and is about; the
  backfill is idempotent; pack context kinds are stored in the context layer.
* The last-run overlay counts a step's input subject and the clause of a case it opened as read.
"""
from __future__ import annotations

import copy
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge import (
    context_graph, graph_versions, ontology_packs, pipeline_trace, scopes, stage_designs,
)
from boss_engine.services.knowledge.step_contracts import contract_shape_problems, parse_contract
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
WORKER = "worker-designs"


def _contract(**extra):
    base = {
        "trigger": "Eagle publishes an exception group",
        "reads": [{"kind": "rule"}, {"kind": "clause"}],
        "writes": [{"kind": "agent_decision", "required": True, "decision_type": "Opened"},
                   {"kind": "coverage_gap"}],
        "calls": [{"mcp": "eagle", "tool": "get_exception_group"}, {"mcp": "itsm"}],
        "steps": [
            {"n": 1, "name": "Read the group", "reads": ["rule"],
             "calls": [{"mcp": "eagle", "tool": "get_exception_group"}],
             "explain": "Reads the exception group Eagle graded Error."},
            {"n": 2, "name": "Open or attach", "reads": ["clause"], "writes": ["agent_decision"],
             "calls": [{"mcp": "itsm", "tool": "create_ticket"}]},
        ],
        "design": {
            "status": "locked", "reference": "Agent Reference v0.2", "under_revision": None,
            "decisions": [{"id": "A-D8", "text": "severity only moves up"}],
            "policy": [{"id": "OP-ENR-01", "text": "hop cap 3"}],
            "applied_in": {"kg": "v16", "cg": "v4", "items": ["CG-G1 gradedSeverity"]},
            "open_questions": [{"id": "HP-Q1", "text": "promotionCandidate versus GovernanceFlag"}],
        },
    }
    base.update(extra)
    return base


def _step(contract):
    return {"id": "monitoring", "kind": "capability", "contract": contract}


# ── A. the contract schema ──────────────────────────────────────────────────────────────────

def test_steps_and_design_as_documented_are_accepted_and_parsed():
    assert contract_shape_problems(_step(_contract())) == []
    parsed = parse_contract("monitoring", _contract(steps=list(reversed(_contract()["steps"]))))
    assert [s.n for s in parsed.steps] == [1, 2], "steps are ordered by n"
    assert parsed.steps[0].calls[0].mcp == "eagle" and parsed.steps[0].explain
    assert parsed.design.status == "locked" and parsed.design.reference == "Agent Reference v0.2"
    assert parsed.design.decisions[0].id == "A-D8"
    assert parsed.design.applied_in == {"kg": "v16", "cg": "v4", "items": ["CG-G1 gradedSeverity"]}


@pytest.mark.parametrize("mutate, says", [
    (lambda c: c["steps"][0].update(reads=["cde"]), "reads ['cde'], which the contract's own reads"),
    (lambda c: c["steps"][1].update(writes=["incident"]), "writes ['incident']"),
    (lambda c: c["steps"][0].update(calls=[{"mcp": "olympus"}]), "calls olympus"),
    (lambda c: c["steps"][0].update(calls=[{"mcp": "eagle", "tool": "rerun_check"}]), "calls eagle.rerun_check"),
    (lambda c: c["steps"][0].update(colour="red"), "unknown keys ['colour']"),
    (lambda c: c["steps"][1].update(n=1), "numbers step 1 twice"),
    (lambda c: c["steps"][0].update(n=True), "need a number n"),
    (lambda c: c["steps"][0].pop("name"), "needs a name"),
    (lambda c: c["design"].update(status="approved"), "status must be one of"),
    (lambda c: c["design"].update(owner="x"), "contract.design has unknown keys ['owner']"),
    (lambda c: c["design"]["decisions"].append({"id": "A-D9"}), "need an id and a text"),
    (lambda c: c["design"]["policy"].append({"id": "P", "text": "t", "why": "w"}), "unknown keys ['why']"),
    (lambda c: c["design"]["applied_in"].update(dq="v1"), "applied_in has unknown keys ['dq']"),
    (lambda c: c.update(diagram={}), "contract has unknown keys ['diagram']"),
])
def test_a_step_or_card_outside_the_schema_is_refused(mutate, says):
    contract = copy.deepcopy(_contract())
    mutate(contract)
    problems = contract_shape_problems(_step(contract))
    assert any(says in p for p in problems), problems


def test_a_step_call_matches_a_contract_call_that_names_no_tool():
    contract = _contract()
    contract["steps"][1]["calls"] = [{"mcp": "itsm", "tool": "anything"}]
    assert contract_shape_problems(_step(contract)) == []


def test_reference_bumps_keep_the_consultants_numbering():
    assert stage_designs.next_reference("Agent Reference v0.2") == "Agent Reference v0.3"
    assert stage_designs.next_reference("Spec v2") == "Spec v3"
    assert stage_designs.next_reference("") == "Agent Reference v0.1"


def test_the_design_hash_ignores_only_the_review_managed_fields():
    a = _contract()
    b = copy.deepcopy(a)
    b["design"].update(status="in_review", reference="Agent Reference v0.3", under_revision="why")
    assert stage_designs.contract_hash(a) == stage_designs.contract_hash(b)
    b["design"]["decisions"].append({"id": "A-D9", "text": "link facts only"})
    assert stage_designs.contract_hash(a) != stage_designs.contract_hash(b)
    c = copy.deepcopy(a)
    c["steps"][0]["name"] = "Read the exception group"
    assert stage_designs.contract_hash(a) != stage_designs.contract_hash(c)


# ── the database ────────────────────────────────────────────────────────────────────────────

def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "ci", "scratch", "tmp")):
        pytest.skip("DATABASE_URL must name a disposable postgres database")
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
            suffix = uuid.uuid4().hex[:8]
            org, ws, project = f"org_ad_{suffix}", f"ws_ad_{suffix}", f"p_ad_{suffix}"
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'AD')"), {"i": org})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'AD')"),
                         {"i": ws, "o": org})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                              "(:i, :w, 'AD', :s)"), {"i": project, "w": ws, "s": project})
            ontology_packs.set_project_pack(conn, project_id=project, pack_id="banking-regulatory")
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _node(c, kind: str, name: str, **attrs) -> str:
    return STORE.upsert_node(c, project_id=c.project_id, kind=kind, node_key=f"{kind}:{name}",
                             name=name, body=name, trust_tier="observed", actor_kind="system",
                             attrs=attrs or None)


def _remediation_step(contract=None):
    contract = contract or copy.deepcopy(catalogue.REGULATORY_CONTRACTS["remediation_package"])
    return {"id": "remediation_package", "kind": "capability", "title": "Remediation package",
            "binding": {"capability": "remediation_package", "inputs": {}},
            "contract": contract, "next": []}


def _workflow(c, contract=None):
    key = f"ad_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=c.project_id, workflow_key=key, name=key, description=None, actor="test",
        namespace="product")
    workflows.publish_version(c, project_id=c.project_id, definition_id=definition["id"],
                              spec={"entry_step": "remediation_package",
                                    "steps": [_remediation_step(contract)]}, actor="test")
    return key, definition


def _publish(c, definition, contract):
    return workflows.publish_version(
        c, project_id=c.project_id, definition_id=definition["id"],
        spec={"entry_step": "remediation_package", "steps": [_remediation_step(contract)]},
        actor="test")


# ── D. review and lock ──────────────────────────────────────────────────────────────────────

def test_a_design_goes_from_not_reviewed_to_locked_and_only_a_person_locks_it(connection):
    c = connection
    key, _definition = _workflow(c)
    stage = stage_designs.designs(c, c.project_id, key)["stages"]["remediation_package"]
    assert stage["status"] == "not_reviewed" and stage["can"]["send_for_review"]
    with pytest.raises(stage_designs.DesignRefused, match="for review before locking"):
        stage_designs.lock(c, c.project_id, workflow_key=key, stage_id="remediation_package",
                           actor="u1", actor_kind="person")
    sent = stage_designs.send_for_review(c, c.project_id, workflow_key=key,
                                         stage_id="remediation_package", actor="u1")
    assert sent["status"] == "in_review" and sent["reference"] == "Agent Reference v0.1"
    with pytest.raises(PermissionError, match="Only a person"):
        stage_designs.lock(c, c.project_id, workflow_key=key, stage_id="remediation_package",
                           actor="agent:x", actor_kind="agent")
    locked = stage_designs.lock(c, c.project_id, workflow_key=key, stage_id="remediation_package",
                                actor="u1", actor_kind="person", actor_name="Dana Reviewer")
    assert locked["status"] == "locked" and locked["locked"]["locked_by"] == "Dana Reviewer"
    assert locked["can"] == {"send_for_review": False, "lock": False, "revise": True}
    events = c.execute(text("SELECT event_type FROM events WHERE project_id = :p "
                            "AND event_type LIKE 'workflow.design_%' ORDER BY created_at"),
                       {"p": c.project_id}).scalars().all()
    assert events == ["workflow.design_sent_for_review", "workflow.design_locked"]


def test_a_locked_contract_is_refused_at_publish_until_revise_opens_the_next_version(connection):
    c = connection
    key, definition = _workflow(c)
    stage_designs.send_for_review(c, c.project_id, workflow_key=key, stage_id="remediation_package", actor="u1")
    stage_designs.lock(c, c.project_id, workflow_key=key, stage_id="remediation_package",
                       actor="u1", actor_kind="person")
    changed = copy.deepcopy(catalogue.REGULATORY_CONTRACTS["remediation_package"])
    changed["human_review"] = "two people approve it"
    with pytest.raises(workflows.WorkflowValidationError) as refused:
        _publish(c, definition, changed)
    message = str(refused.value)
    assert "locked at Agent Reference v0.1" in message and "Revise" in message and "v0.2" in message
    assert refused.value.problems[0]["step_id"] == "remediation_package"
    # A managed field alone is not a change of design.
    same = copy.deepcopy(catalogue.REGULATORY_CONTRACTS["remediation_package"])
    same["design"] = {"status": "locked", "reference": "Agent Reference v0.1"}
    _publish(c, definition, same)
    revised = stage_designs.revise(c, c.project_id, workflow_key=key, stage_id="remediation_package",
                                   actor="u1", note="Two approvers now")
    assert revised["status"] == "in_review" and revised["reference"] == "Agent Reference v0.2"
    assert revised["locked"]["reference"] == "Agent Reference v0.1", "the locked one stays on record"
    assert revised["under_revision"] == "Two approvers now"
    _publish(c, definition, changed)
    relocked = stage_designs.lock(c, c.project_id, workflow_key=key, stage_id="remediation_package",
                                  actor="u1", actor_kind="person")
    assert relocked["status"] == "locked" and relocked["reference"] == "Agent Reference v0.2"


def test_a_contract_that_declares_locked_is_enforced_like_a_locked_version(connection):
    c = connection
    declared = copy.deepcopy(catalogue.REGULATORY_CONTRACTS["remediation_package"])
    declared["design"] = {"status": "locked", "reference": "Agent Reference v0.3"}
    key, definition = _workflow(c, declared)
    stage = stage_designs.designs(c, c.project_id, key)["stages"]["remediation_package"]
    assert (stage["status"], stage["reference"], stage["source"]) == ("locked", "Agent Reference v0.3", "contract")
    changed = copy.deepcopy(declared)
    changed["trigger"] = "after the lanes, and on demand"
    with pytest.raises(workflows.WorkflowValidationError, match="locked at Agent Reference v0.3"):
        _publish(c, definition, changed)
    revised = stage_designs.revise(c, c.project_id, workflow_key=key, stage_id="remediation_package", actor="u1")
    assert revised["reference"] == "Agent Reference v0.4"
    _publish(c, definition, changed)


def test_a_person_step_is_a_human_step_and_is_not_reviewed_as_an_agent(connection):
    step = {"id": "approve", "kind": "decision", "contract": {"reads": [], "writes": []}}
    assert stage_designs.stage_design(step, [])["status"] == "human_step"
    shared = {"id": "curator", "kind": "capability",
              "contract": {"design": {"status": "shared_service", "reference": "Chapter to write"}}}
    state = stage_designs.stage_design(shared, [])
    assert state["status"] == "shared_service" and not any(state["can"].values())


# ── E. versions ─────────────────────────────────────────────────────────────────────────────

def test_the_kg_version_counts_approved_change_sets_and_holds_as_of(connection):
    c = connection
    assert graph_versions.kg_version(c, c.project_id) == 1
    rule = _node(c, "rule", "credit on receipt")
    earlier = datetime.now(timezone.utc) - timedelta(days=2)
    for action, at in (("knowledge.rule.approve", earlier), ("knowledge.rule.edit", None),
                       ("knowledge.rule.propose", None), ("knowledge.promote", None)):
        STORE.audit(c, project_id=c.project_id, action=action, entity_id=rule, actor_kind="person")
        if at:
            c.execute(text("UPDATE kg_audit SET created_at = :t WHERE project_id = :p AND action = :a"),
                      {"t": at, "p": c.project_id, "a": action})
    clause = _node(c, "clause", "1026.36(c)(1)(i)")
    c.execute(text(
        "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, trust_tier, attrs) VALUES "
        "(:i, :p, :s, :d, 'implemented_by', 'approved', CAST(:a AS JSONB))"),
        {"i": f"kge_{uuid.uuid4().hex[:12]}", "p": c.project_id, "s": clause, "d": rule,
         "a": f'{{"reviewed_at": "{datetime.now(timezone.utc).isoformat()}"}}'})
    # approve + edit + promote + one approved trace link; a proposal is not a change set.
    assert graph_versions.kg_version(c, c.project_id) == 5
    assert graph_versions.kg_version(c, c.project_id, as_of=earlier + timedelta(hours=1)) == 2


def test_the_cg_version_is_the_packs_and_every_decision_records_both(connection):
    c = connection
    assert graph_versions.cg_version(c, c.project_id) == 1
    pack = ontology_packs.get_project_pack(c, project_id=c.project_id)
    # The shipped packs declare no context_version yet: one that does is read as the CG version.
    object.__setattr__(pack, "context_version", "4")
    try:
        assert graph_versions.cg_version(c, c.project_id) == 4
        written = context_graph.write_agent_decision(
            c, c.project_id, step_id="monitoring", run_id=f"wfr_{uuid.uuid4().hex[:12]}",
            decision_type="Opened", statement="Opened an incident", policy_version="v0.2.0",
            workflow_version="3", agent_fingerprint="fp", actor_ref="agent:monitoring")
    finally:
        object.__setattr__(pack, "context_version", None)
    attrs = c.execute(text("SELECT attrs FROM kg_nodes WHERE id = :i"), {"i": written["id"]}).scalar_one()
    assert (attrs["kg_version"], attrs["cg_version"]) == (1, 4)


# ── F. layers and stored edges ──────────────────────────────────────────────────────────────

def test_pack_context_kinds_are_stored_in_the_context_layer_and_the_backfill_is_idempotent(connection):
    c = connection
    case = _node(c, "compliance_case", "case 1")
    clause = _node(c, "clause", "a clause")
    layer = lambda i: c.execute(text("SELECT knowledge_layer FROM kg_nodes WHERE id = :i"), {"i": i}).scalar_one()  # noqa: E731
    assert layer(case) == "context" and layer(clause) == "knowledge"
    assert scopes.layer_of("compliance_case") == "context" and scopes.layer_of("aml_case") == "context"
    # A row stored before the fix: the sync puts it right, once.
    c.execute(text("UPDATE kg_nodes SET knowledge_layer = 'knowledge' WHERE id = :i"), {"i": case})
    first = ontology_packs.sync_kind_layers(c, force=True)
    assert first["nodes"] >= 1 and layer(case) == "context"
    assert ontology_packs.sync_kind_layers(c, force=True) == {"kinds": 0, "nodes": 0}
    # A contract reading a pack context kind reaches it under its policy.
    policy = scopes.policy_for(step={"id": "s", "kind": "capability", "contract": {
        "reads": [{"kind": "compliance_case"}]}})
    assert "context" in policy.layers


def _run_one_step(c, step, run_input, output):
    key = f"ad_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(c, project_id=c.project_id, workflow_key=key, name=key,
                                             description=None, actor="test", namespace="product")
    version = workflows.publish_version(c, project_id=c.project_id, definition_id=definition["id"],
                                        spec={"entry_step": step["id"], "steps": [step]}, actor="test")
    run = workflows.start_run(c, project_id=c.project_id, actor="test", input_data=run_input,
                              dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version["id"])
    claimed = workflows.claim_step(c, project_id=c.project_id, run_id=run["id"], worker_id=WORKER,
                                   lease_seconds=600)
    produced = output(run["id"], step["id"])
    workflows.complete_step(c, project_id=c.project_id, step_run_id=claimed["id"], worker_id=WORKER,
                            output=produced, evidence=[])
    return run["id"]


def _edges(c, src):
    return {(r[0], r[1], r[2] or "") for r in c.execute(text(
        "SELECT dst_id, kind, attrs->>'how' FROM kg_edges WHERE src_id = :s"), {"s": src}).all()}


def test_a_steps_decision_is_linked_to_what_it_consulted_cited_and_is_about(connection):
    c = connection
    rule, symbol = _node(c, "rule", "classification"), _node(c, "symbol", "Allocator.apply")
    clause = _node(c, "clause", "1026.36(c)(1)(ii)")
    case = _node(c, "compliance_case", "case for (ii)", clause_id=clause)
    contract = {"reads": [{"kind": "rule"}, {"kind": "symbol"}, {"kind": "clause"}],
                "writes": [{"kind": "agent_decision", "required": True, "decision_type": "Assessed"}]}
    step = {"id": "compliance_assessment", "kind": "capability",
            "binding": {"capability": "clause_intake",
                        "inputs": {"subject_id": {"from_input": "subject_id"}}},
            "contract": contract, "next": []}
    decision_ids = []

    def output(run_id, step_id):
        decision_ids.append(context_graph.write_agent_decision(
            c, c.project_id, step_id=step_id, run_id=run_id, decision_type="Assessed",
            statement="Assessed as gap", policy_version="", workflow_version="", agent_fingerprint="",
            actor_ref="agent:x")["id"])
        return {"produced": {"agent_decision": {"node_id": decision_ids[0], "decision_type": "Assessed",
                                                "cited_fact_ids": [rule]}},
                "collected": [symbol], "compliance_case_ids": [case]}

    run_id = _run_one_step(c, step, {"subject_id": clause}, output)
    edges = _edges(c, decision_ids[0])
    assert (rule, "consulted", "cited") in edges
    assert (symbol, "consulted", "read") in edges
    assert (clause, "about", "") in edges, "the subject the run bound it to"
    assert (case, "about", "") in edges, "the case it worked on"
    # The canvas's Context layer draws them as edges.
    from boss_engine.services.knowledge import view_context

    drawn = {(s, d, k) for s, d, k, _w in view_context.load_trace(c, c.project_id).edges}
    assert (decision_ids[0], rule, "cites") in drawn and (decision_ids[0], symbol, "consulted") in drawn
    # Idempotent: the backfill finds nothing left to link, and writes nothing.
    assert context_graph.backfill_decision_edges(c, c.project_id)["edges_added"] == 0
    # The backfill writes the same edges for a step that ran before they existed.
    c.execute(text("DELETE FROM kg_edges WHERE src_id = :s"), {"s": decision_ids[0]})
    done = context_graph.backfill_decision_edges(c, c.project_id)
    assert done["edges_added"] == 4 and _edges(c, decision_ids[0]) == edges
    assert context_graph.backfill_decision_edges(c, c.project_id)["edges_added"] == 0
    # G. the overlay: the subject clause is READ by the step, never "declared, not done".
    trace = pipeline_trace.run_trace(c, c.project_id, run_id)
    reads = {k["kind"] for k in trace["steps"]["compliance_assessment"]["reads"]}
    assert {"clause", "rule", "symbol"} <= reads


def test_the_clause_of_a_case_a_step_opened_counts_as_read(connection):
    c = connection
    document = _node(c, "document", "Regulation Z 1026.36(c)(1)")
    clause = _node(c, "clause", "1026.36(c)(1)(i)")
    step = {"id": "clause_intake", "kind": "capability",
            "binding": {"capability": "clause_intake",
                        "inputs": {"document_id": {"from_input": "document_id"}}},
            "contract": {"reads": [{"kind": "document"}, {"kind": "clause"}],
                         "writes": [{"kind": "compliance_case", "required": True}]},
            "next": []}

    def output(run_id, step_id):
        case = STORE.upsert_node(c, project_id=c.project_id, kind="compliance_case",
                                 node_key=f"case:{clause}", name="Case", body="", trust_tier="inferred",
                                 actor_kind="agent", attrs={"clause_id": clause, "run_id": run_id,
                                                            "step_id": step_id})
        return {"produced": {"compliance_case": {"node_ids": [case]}}, "compliance_case_ids": [case]}

    run_id = _run_one_step(c, step, {"document_id": document}, output)
    trace = pipeline_trace.run_trace(c, c.project_id, run_id)["steps"]["clause_intake"]
    assert {k["kind"] for k in trace["reads"]} == {"document", "clause"}
    assert {k["kind"] for k in trace["writes"]} == {"compliance_case"}
