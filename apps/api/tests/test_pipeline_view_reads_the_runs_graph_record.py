"""The Pipeline view's "Show the last run" reads the run's graph record (design §4.7).

The first version overlaid a step's input and output KEYS as its "reads" and "writes" — a key
named `subject_id` is not a read of a Clause — and only for steps whose status was
"completed", which the runner never records (it records "succeeded"), so the overlay never
filled. GET /v1/workflow-runs/{run_id}/context-trace answers per step from the graph itself:
the Context Graph rows written for the run and step, the nodes the step's output names, and the
run's recorded usage. And the capabilities view names every role a stage runs as.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.knowledge import context_graph
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
RUN = "wfr_ctx_trace_1"


def _seed(engine, project_id: str) -> dict[str, str]:
    spec = {"entry_step": "enrich", "steps": [
        {"id": "enrich", "kind": "deterministic", "next": ["assess"]},
        {"id": "assess", "kind": "capability", "binding": {"capability": "compliance_assessment"},
         "next": ["close"]},
        {"id": "close", "kind": "capability", "binding": {"capability": "compliance_closure"}},
    ]}
    with engine.begin() as c:
        rule = STORE.upsert_node(c, project_id=project_id, kind="rule", node_key="rule:ctx-test",
                                 name="Payments are credited on the day received", body="",
                                 trust_tier="observed", actor_kind="agent")
        incident = STORE.upsert_node(c, project_id=project_id, kind="incident",
                                     node_key="incident:INC-ctx", name="INC-2088", body="",
                                     trust_tier="observed", actor_kind="agent")
        precedent = STORE.upsert_node(c, project_id=project_id, kind="precedent",
                                      node_key=f"precedent:{RUN}:close", name="Precedent: allocation",
                                      body="", trust_tier="inferred", actor_kind="agent")
        context_graph.write_agent_decision(
            c, project_id, step_id="enrich", run_id=RUN, decision_type="ContextAssembled",
            statement="assembled", policy_version="", workflow_version="",
            agent_fingerprint="deterministic", actor_ref="test")
        context_graph.write_agent_decision(
            c, project_id, step_id="assess", run_id=RUN, decision_type="Assessment",
            statement="gap", policy_version="", workflow_version="",
            agent_fingerprint="deterministic", actor_ref="test")
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
            "VALUES ('wfd_ctx1', :p, 'ctx_trace_wf', 'Context trace', 'ceo') "
            "ON CONFLICT (id) DO NOTHING"), {"p": project_id})
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            "  status, spec, content_hash, created_by) "
            "VALUES ('wfv_ctx1', :p, 'wfd_ctx1', 1, 'published', CAST(:s AS JSONB), :h, 'ceo') "
            "ON CONFLICT (id) DO NOTHING"),
            {"p": project_id, "s": json.dumps(spec),
             "h": hashlib.sha256(json.dumps(spec).encode()).hexdigest()})
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
            "  actor, status, input) VALUES (:r, :p, 'wfd_ctx1', 'wfv_ctx1', 'ceo', 'waiting', "
            "  CAST('{\"subject_id\": \"x\"}' AS JSONB)) ON CONFLICT (id) DO NOTHING"),
            {"r": RUN, "p": project_id})
        rows = [
            ("wsr_ctx1", "enrich", "deterministic", "succeeded", {
                "collected": [rule, incident],
                # The MCP calls a deterministic step records: one made, one refused.
                "calls": [{"mcp": "service-desk", "tool": "list_incidents", "ok": True},
                          {"mcp": "kyc", "tool": "get_kyc_profile", "ok": False}]}),
            ("wsr_ctx2", "assess", "capability", "succeeded",
             {"citation": rule, "assessment": "gap", "subject_id": "not-a-node"}),
            ("wsr_ctx3", "close", "capability", "pending", {"precedent_id": precedent}),
        ]
        for sid, step, kind, status, output in rows:
            c.execute(text(
                "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
                "  status, input, output, attempt, max_attempts) VALUES (:id, :p, :r, :s, :k, :st, "
                "  CAST('{\"rule\": {}}' AS JSONB), CAST(:o AS JSONB), 1, 3) "
                "ON CONFLICT (id) DO NOTHING"),
                {"id": sid, "p": project_id, "r": RUN, "s": step, "k": kind, "st": status,
                 "o": json.dumps(output)})
    return {"rule": rule, "incident": incident, "precedent": precedent}


def _kinds(entries: list[dict]) -> dict[str, dict]:
    return {e["kind"]: e for e in entries}


def test_each_step_reports_what_it_read_and_wrote_in_the_graph(client, engine):
    project_id = get_settings().default_project_id
    _seed(engine, project_id)

    r = client.get(f"/v1/workflow-runs/{RUN}/context-trace", params={"project_id": project_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "waiting"
    steps = body["steps"]

    enrich = steps["enrich"]
    assert enrich["status"] == "succeeded"  # the runner's word, not "completed"
    assert set(_kinds(enrich["reads"])) == {"rule", "incident"}
    assert _kinds(enrich["reads"])["incident"]["examples"] == ["INC-2088"]
    writes = _kinds(enrich["writes"])
    assert set(writes) == {"agent_decision"}
    assert writes["agent_decision"]["decision_types"] == ["ContextAssembled"]

    # The calls it recorded: made ones counted, a refused one not; the step is marked as
    # recording its calls, so a declared call it never made reads "declared, not done".
    assert enrich["calls"] == [{"tool": "list_incidents", "server": "service-desk", "count": 1}]
    assert enrich["calls_recorded"] is True

    assess = steps["assess"]
    assert assess["calls"] == [] and assess["calls_recorded"] is False
    assert set(_kinds(assess["reads"])) == {"rule"}
    assert _kinds(assess["writes"])["agent_decision"]["decision_types"] == ["Assessment"]

    close = steps["close"]
    assert close["status"] == "pending"
    assert set(_kinds(close["writes"])) == {"precedent"}

    # Never the step's input or output KEYS as kinds.
    every = {e["kind"] for s in steps.values() for e in s["reads"] + s["writes"]}
    assert not every & {"subject_id", "collected", "citation", "assessment", "precedent_id"}
    # And no raw node id as an example name.
    assert "kgn_" not in json.dumps([e["examples"] for s in steps.values()
                                     for e in s["reads"] + s["writes"]])


def test_a_run_of_another_project_is_not_found(client, engine):
    project_id = get_settings().default_project_id
    _seed(engine, project_id)
    r = client.get(f"/v1/workflow-runs/{RUN}/context-trace", params={"project_id": "northpeak-bank-b"})
    assert r.status_code in (403, 404), r.text
    missing = client.get("/v1/workflow-runs/wfr_nope/context-trace", params={"project_id": project_id})
    assert missing.status_code == 404


def test_the_palette_names_every_role_a_stage_runs_as(client):
    project_id = get_settings().default_project_id
    r = client.get("/v1/delivery-workflows/capabilities", params={"project_id": project_id})
    assert r.status_code == 200, r.text
    body = r.json()
    caps = {c["key"]: c for c in body["capabilities"]}
    assert caps["compliance_assessment"]["role_agent"] == "compliance_analyst"
    assert caps["lane_confirmation"]["role_agent"] == "lane_agent"
    assert caps["remediation_package"]["role_agent"] == "remediation_advisor"
    names = body["role_names"]
    # Shipped roles are named even where the project has not installed them.
    assert names["compliance_analyst"] == {"name": "Compliance Analyst", "callsign": "Tally"}
    assert names["lane_agent"]["callsign"] == "Plumb"
    assert names["remediation_advisor"]["callsign"] == "Mend"
    assert body["graph_steps"]["context_enrichment"]["label"] == "Context enrichment"
    assert body["graph_steps"]["historical_pattern"]["summary"]
