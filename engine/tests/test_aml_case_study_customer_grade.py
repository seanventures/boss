"""Customer-grade gaps in the AML alert investigation case study (BOSS task "AML case study:
customer-grade"): the network mapper's self-reference bug (A3), a policy document chapter (A1),
separation of duties on filing a SAR (A5), a readable problem screen with no raw ids (A4), and a
clean reload (A6). Run against a real disposable postgres (DATABASE_URL), never the live DB.
"""
from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import mcp_registry, workflows
from boss_engine.services.knowledge import aml_alert_pipeline as pipeline
from boss_engine.services.knowledge import aml_sample, ontology_packs


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
def connection(pg_engine, monkeypatch):
    def call(settings, server, tool, args):
        from tools import mcp_aml_mock

        return mcp_aml_mock.TOOL_FNS[server["name"]][tool](args)

    monkeypatch.setattr(mcp_registry, "call_tool_unattended", call)
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_amlcg_{suffix}", f"ws_amlcg_{suffix}", f"p_amlcg_{suffix}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'AML')"), {"i": org})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'AML')"),
                         {"i": ws, "o": org})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                              "(:i, :w, 'NorthPeak AML', :s)"), {"i": project, "w": ws, "s": project})
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()
            ontology_packs._PROJECT_PACK_CACHE.pop(project, None)


@pytest.fixture()
def loaded(connection):
    result = aml_sample.load_sample(connection, project_id=connection.project_id, actor="ceo")
    return connection, connection.project_id, result


def _run_network_mapping_scripted(c, project, alert_id, run_id):
    """Drives alert_intake -> evidence_collection -> network_mapping with a scripted agent that
    answers from whatever related_to edges the prompt actually shows it (never ground truth)."""
    import re

    class Scripted:
        def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
            seeds = set(re.findall(r"ALERTED (?:CUSTOMER|ACCOUNT): .*\[(kgn_[0-9a-f]+)\]", prompt))
            parties = []
            for ref, src, dst in re.findall(
                    r"^(E\d+): .*?\[(kgn_[0-9a-f]+)\] —related_to→ .*?\[(kgn_[0-9a-f]+)\]",
                    prompt, re.M):
                other = dst if src in seeds else src
                if other not in seeds:
                    parties.append({"node_id": other, "relationship": "on record", "edges": [ref]})
            import json

            answer = {"related_parties": parties, "summary": "scripted"}
            return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(answer)}\n"
                               f"{pipeline._shared._MARK_CLOSE}"}

    orig = pipeline._role_agent
    pipeline._role_agent = lambda *a, **k: (Scripted(), "scripted")
    try:
        intake = pipeline.run_alert_intake(c, project_id=project, run_id=run_id,
                                           step_id="alert_intake", alert_id=alert_id)
        pipeline.run_evidence_collection(c, project_id=project, run_id=run_id,
                                         step_id="evidence_collection", alert_id=alert_id,
                                         case_id=intake["case_id"])
        mapped = pipeline.run_aml_network_mapping(
            c, project_id=project, run_id=run_id, step_id="network_mapping", alert_id=alert_id,
            case_id=intake["case_id"])
    finally:
        pipeline._role_agent = orig
    return intake, mapped


def test_p0_network_mapper_never_names_boss_own_case_nodes_and_is_idempotent(loaded):
    """A3: the mapper must exclude case/alert/agent-written edges it created on a prior run, and
    never return a party with no name ('None' on screen). A second run on the same alert maps the
    same parties as the first."""
    c, project, _ = loaded
    first_intake, first = _run_network_mapping_scripted(c, project, "ALERT-3", "run-first")

    party_kinds = set()
    for party in first["related_parties"]:
        node_id = party["node_id"]
        kind = c.execute(text("SELECT kind FROM kg_nodes WHERE id = :i"), {"i": node_id}).scalar()
        party_kinds.add(kind)
        assert kind not in ("aml_case", "alert"), (
            f"network mapper returned BOSS's own {kind!r} node as a network party")
        name = c.execute(text("SELECT attrs->>'name' FROM kg_nodes WHERE id = :i"),
                         {"i": node_id}).scalar()
        assert name not in (None, "None", "UNKNOWN"), "party rendered with no name"

    second_intake, second = _run_network_mapping_scripted(c, project, "ALERT-3", "run-second")
    first_ids = {p["node_id"] for p in first["related_parties"]}
    second_ids = {p["node_id"] for p in second["related_parties"]}
    assert first_ids == second_ids, "a second run on the same alert must map the same parties"


def test_p1_aml_policy_document_grounds_rules_with_a_citation(loaded):
    """A1: an AML monitoring policy is ingested as a document (same path as the DQ rulebook),
    with clauses a person can cite, and the active pack grants it before the document is added."""
    c, project, _ = loaded
    from boss_engine.services.knowledge import documents

    result = documents.add_document(
        c, project_id=project, title="NorthPeak AML Transaction Monitoring Standard",
        source_url="internal://aml-policy", citation="NorthPeak AML Standard v1",
        clauses=[
            {"section": "FANOUT-01", "text": "Multiple payments to distinct payees in one day "
                                              "above the fan-out threshold are flagged.",
             "citation": "NorthPeak AML Standard v1 p.4", "page": 4},
            {"section": "STRUCT-02", "text": "Repeated cash deposits just under the CTR "
                                              "threshold followed by outbound wires are flagged.",
             "citation": "NorthPeak AML Standard v1 p.7", "page": 7},
        ],
        actor_kind="person", actor_ref="dana.analyst")
    assert result["document_id"] and len(result["clause_ids"]) == 2

    listed = documents.list_documents(c, project_id=project)
    doc = next(d for d in listed if d["id"] == result["document_id"])
    sections = {clause["section"]: clause for clause in doc["clauses"]}
    assert sections["STRUCT-02"]["citation"] == "NorthPeak AML Standard v1 p.7"
    assert sections["STRUCT-02"]["page"] == 7


def test_p1_filing_the_sar_refuses_the_person_who_made_the_analyst_decision(loaded, monkeypatch):
    """A5: separation of duties — the person who decided to escalate cannot also be the one who
    files the SAR; a different person can."""
    c, project, _ = loaded
    import json
    import re

    class Scripted:
        def run_role(self, prompt, cwd, **_kw):
            if prompt.startswith("# Network Mapper"):
                answer = {"related_parties": [], "summary": "scripted"}
            elif prompt.startswith("# AML Analyst"):
                answer = {"recommendation": "escalate", "confidence": 0.9, "rationale": "scripted",
                          "cited": [], "precedents_applied": []}
            else:
                answer = {"narrative": "WHO: scripted.\n\nWHY: scripted.", "cited": []}
            return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(answer)}\n"
                               f"{pipeline._shared._MARK_CLOSE}"}

    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (Scripted(), "scripted"))

    from boss_engine.services import workflow_catalogue as catalogue
    from boss_engine.services import workflow_capability_runner as runner
    from types import SimpleNamespace

    entry = next(e for e in catalogue.CATALOGUE if e["key"] == "aml_alert_investigation")
    steps = {s["id"]: s for s in entry["steps"]}
    assert steps["sar_filing"]["approval"].get("distinct_from") == ["analyst_decision"], (
        "the SAR filing gate must declare distinct_from: [analyst_decision]")

    definition_id = c.execute(text("SELECT id FROM workflow_definitions WHERE project_id = :p "
                                   "AND workflow_key = :k"),
                              {"p": project, "k": "aml_alert_investigation"}).scalar()
    version = c.execute(text("SELECT id FROM workflow_versions WHERE workflow_definition_id = :d "
                             "ORDER BY version DESC LIMIT 1"), {"d": definition_id}).scalar()
    run = workflows.start_run(c, project_id=project, actor="ceo",
                              input_data={"alert_id": "ALERT-3"},
                              dedupe_key=f"aml_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=version)
    run_id = run["id"]
    spec_steps = {s["id"]: s for s in catalogue.entry_spec(entry)["steps"]}
    ctx = SimpleNamespace(settings=None, engine=None)

    def drive():
        for _ in range(40):
            claimed = workflows.claim_step(c, project_id=project, run_id=run_id,
                                           worker_id="worker-amlcg", lease_seconds=600)
            if claimed is not None:
                step = spec_steps[claimed["step_id"]]
                output = runner.execute_step(
                    c, ctx, project_id=project, actor="ceo", step=step, spec_steps=spec_steps,
                    context={**runner._run_context(c, run_id), "run_id": run_id})
                workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                        worker_id="worker-amlcg", output=output, evidence=[])
                continue
            workflows.advance_run(c, project, run_id)
            waiting = c.execute(text(
                "SELECT step_id FROM workflow_step_runs WHERE workflow_run_id = :r AND "
                "status = 'waiting' AND decision_id IS NOT NULL"), {"r": run_id}).scalars().all()
            if waiting:
                return waiting[0]
        raise AssertionError("did not reach a gate")

    assert drive() == "analyst_decision"
    analyst_decision_id = c.execute(text(
        "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r AND "
        "step_id = 'analyst_decision'"), {"r": run_id}).scalar()
    c.execute(text("UPDATE decisions SET status = 'approved', approved_by = :b, "
                   "approved_at = now(), resolution_note = :n WHERE id = :i"),
              {"b": "dana.analyst", "n": "Escalate: structuring.", "i": analyst_decision_id})
    assert drive() == "sar_filing"
    sar_decision_id = c.execute(text(
        "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r AND "
        "step_id = 'sar_filing'"), {"r": run_id}).scalar()

    refusal = workflows.separation_of_duties_refusal(c, sar_decision_id, "dana.analyst")
    assert refusal and "dana.analyst" in refusal

    assert workflows.separation_of_duties_refusal(c, sar_decision_id, "bsa.officer") is None


def test_p1_problem_screen_names_customers_shows_the_rule_and_no_raw_ids(loaded):
    """A4: the problem screen shows the customer's name and the rule, not raw ids like
    CUST-0007, and its open-alert count reflects alerts that are actually still open."""
    c, project, _ = loaded
    screen = aml_sample.build_problem_screen(c, project_id=project)
    assert screen["alerts"], "the problem screen must list the open alerts"
    for row in screen["alerts"]:
        rendered = " ".join(str(v) for v in row.values())
        assert not re.search(r"\bCUST-\d+\b", rendered), rendered
        assert not re.search(r"\bkgn_[0-9a-f]+\b", rendered), rendered
        assert "None" not in rendered and "UNKNOWN" not in rendered
        assert row.get("customer_name"), "each row must carry a readable customer name"
        assert row.get("rule_text") or row.get("rule"), "each row must carry the rule"
    assert screen["open_count"] == len([a for a in screen["alerts"] if a.get("is_open", True)])


def test_p1_reloading_the_sample_clears_prior_runs_cases_and_inferred_edges(loaded):
    """A6: reloading the sample, like the DQ platform reload, removes the leftovers of earlier
    runs — the case nodes and the agent-inferred related_to edges — not just the sample data."""
    c, project, _ = loaded
    _run_network_mapping_scripted(c, project, "ALERT-1", "run-before-reload")
    cases_before = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND "
                                  "kind = 'aml_case' AND valid_to IS NULL"),
                             {"p": project}).scalar()
    assert cases_before > 0, "setup should have created a case node"

    aml_sample.reload_sample(c, project_id=project, actor="ceo")

    cases_after = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND "
                                 "kind = 'aml_case' AND valid_to IS NULL"),
                            {"p": project}).scalar()
    inferred_edges_after = c.execute(text(
        "SELECT count(*) FROM kg_edges WHERE project_id = :p AND kind = 'related_to' AND "
        "attrs->>'run_id' IS NOT NULL AND valid_to IS NULL"), {"p": project}).scalar()
    assert cases_after == 0, "reloading the sample must clear leftover case nodes"
    assert inferred_edges_after == 0, "reloading the sample must clear leftover inferred edges"

    customers_after = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND "
                                     "node_key LIKE 'aml:customer:%' AND valid_to IS NULL"),
                                {"p": project}).scalar()
    assert customers_after == 40, "the sample data itself must still be fully loaded"
