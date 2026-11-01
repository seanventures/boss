"""Case study 4: the AML alert investigation, built into BOSS as an instance of
kg_investigation_pipeline (`aml_alert_investigation`).

What is proven here, against a real disposable postgres (DATABASE_URL, never the live DB):

* the `banking-aml` ontology pack validates, and includes banking-regulatory's kinds;
* the synthetic bank is deterministic and plants both patterns (benign payroll fan-out, a real
  structuring ring), with ground truth kept in the generator;
* the mock connectors answer from that bank, read-only except the case note;
* loading the sample is the product action (pack, connectors, graph, workflow installed);
* each deterministic step's output on the generated data;
* the real runtime runs ALERT-1 then ALERT-2 end to end with a scripted agent: ALERT-2's analysis
  is handed ALERT-1's precedent and reuses it, and every contract's required write lands;
* a person's determination is required before closure, and an escalation waits for a person to
  file.
"""
from __future__ import annotations

import json
import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import mcp_registry
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge import aml_alert_pipeline as pipeline
from boss_engine.services.knowledge import aml_sample, ontology_packs
from boss_engine.services.knowledge.store import NODE_KINDS
from tools import aml_sample_data as data
from tools import mcp_aml_mock

KEY = "aml_alert_investigation"
WORKER = "worker-aml"


# ── no database ───────────────────────────────────────────────────────────────────────────────

def test_the_pack_validates_and_keeps_the_regulatory_kinds():
    pack = ontology_packs.load_pack("banking-aml")
    assert {"customer", "account", "counterparty", "transaction", "typology", "kyc_profile",
            "alert", "aml_case"} <= pack.kinds
    assert {"transfers_to", "matches_typology", "raised_on", "related_to"} <= pack.edges
    assert {"clause", "document", "compliance_case"} <= pack.kinds, "includes banking-regulatory"
    assert not (pack.kinds & NODE_KINDS)
    assert "banking-aml" in ontology_packs.shipped_packs()
    assert pack.kind_meta["alert"]["color"] and pack.kind_meta["alert"]["label"] == "Alert"


def test_the_generator_is_deterministic_and_plants_both_patterns():
    first, second = data.generate.__wrapped__(), data.generate.__wrapped__()
    assert json.dumps(first.transactions) == json.dumps(second.transactions)
    assert first.alerts == second.alerts
    ds = data.generate()
    assert len(ds.customers) == 40 and len(ds.accounts) == 60
    assert len(ds.transactions) >= 2000
    days = {t["booked_at"] for t in ds.transactions}
    assert min(days) >= data.START.isoformat() and max(days) <= data.END.isoformat()

    # (a) payroll fan-out: ALERT-1 flags >= 10 payments to distinct parties in one day, and the
    # KYC file documents the payroll.
    alert1 = ds.alerts["ALERT-1"]
    flagged = [t for t in ds.transactions if t["transaction_id"] in alert1["flagged_transactions"]]
    assert len({t["party"] for t in flagged}) >= 10 and len({t["booked_at"] for t in flagged}) == 1
    assert all(t["memo"] == "PAYROLL" for t in flagged)
    assert any("Payroll register" in d for d in ds.kyc_profiles[data.CAFE]["documents"])
    # ALERT-2 is on the related entity: common owner, funded from the cafe's operating account.
    assert ds.alerts["ALERT-2"]["customer_id"] == data.CATERING
    assert any({a, b} == {data.CATERING, data.CAFE} for a, b, _ in ds.relationships)
    assert any(t["account_id"] == data.CAFE_OPERATING and t["party"] == data.CATERING_PAYROLL
               for t in ds.transactions)

    # (b) the ring: sub-threshold cash, wires to three companies at one address, money back.
    ring = ds.account_transactions(data.MARLOW_ACCOUNT)
    near = [t for t in ring if t["channel"] == "cash" and 9_000 <= t["amount"] < 10_000]
    assert len(near) >= 20
    wired = {t["party"] for t in ring if t["channel"] == "wire" and t["direction"] == "out"}
    assert wired == {"CP-0101", "CP-0102", "CP-0103"}
    assert len({ds.counterparties[c]["registered_address"] for c in wired}) == 1
    assert any(t["account_id"] == data.RHEA_ACCOUNT and t["party"] == "CP-0103"
               for t in ds.transactions)
    assert {k: v["label"] for k, v in data.GROUND_TRUTH.items()} == {
        "ALERT-1": "false_positive", "ALERT-2": "false_positive", "ALERT-3": "true_positive"}


def test_the_mock_connectors_answer_from_the_sample_bank():
    fns = mcp_aml_mock.TOOL_FNS
    alert = fns["case-management"]["get_alert"]({"alert_id": "ALERT-1"})
    assert alert["account_id"] == data.CAFE_PAYROLL and len(alert["flagged"]) == 14
    assert fns["case-management"]["get_alert"]({"alert_id": "NOPE"}).get("error")
    listed = fns["case-management"]["list_alerts"]({"customer_id": data.MARLOW})["alerts"]
    assert [a["alert_id"] for a in listed] == ["ALERT-3"]
    kyc = fns["kyc"]["get_kyc_profile"]({"customer_id": data.CATERING})
    assert kyc["beneficial_owner"] == "Dana Okafor" and kyc["related_parties"]
    txns = fns["transactions"]["query_transactions"]({"account_id": data.MARLOW_ACCOUNT,
                                                      "limit": 5})
    assert len(txns["transactions"]) == 5 and txns["truncated"]
    assert txns["summary"]["cash_deposits_9000_to_9999"] >= 20
    note = fns["case-management"]["add_case_note"]({"alert_id": "ALERT-1", "note": "checked"})
    assert note["ok"] and note["note_id"].startswith("NOTE-ALERT-1")
    # Only the case note writes; nothing can close an alert or file a report.
    tools = {t["name"] for spec in mcp_aml_mock.SERVERS.values() for t in spec["tools"]}
    assert tools == {"get_alert", "list_alerts", "add_case_note", "get_kyc_profile",
                     "query_transactions"}


def test_the_mux_serves_every_connector_the_sample_registers():
    from urllib.parse import urlparse

    from tools.mcp_tools_mux import SERVERS

    served = {port for _name, module, port in SERVERS if module == "tools.mcp_aml_mock"}
    for server in aml_sample.aml_mcp_servers():
        url = urlparse(server["url"])
        assert url.port in served and url.hostname not in ("127.0.0.1", "localhost")
        assert url.path.strip("/").split("/")[0] in mcp_aml_mock.SERVERS


def test_every_step_declares_a_contract_and_the_gates_are_a_persons():
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    steps = {s["id"]: s for s in entry["steps"]}
    for step_id in ("alert_intake", "evidence_collection", "network_mapping",
                    "historical_pattern", "aml_analysis", "narrative", "aml_closure"):
        contract = steps[step_id]["contract"]
        assert {"trigger", "reads", "writes", "llm", "decisions"} <= set(contract), step_id
    for gate in ("analyst_decision", "sar_filing"):
        assert steps[gate]["kind"] == "decision"
        assert steps[gate]["approval"]["mode"] == "manual"
    assert entry["requires"]["ontology_pack"] == "banking-aml"


# ── a real project ───────────────────────────────────────────────────────────────────────────

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
    # The connectors, in-process: the registry's unattended road, answered by the mock itself.
    def call(settings, server, tool, args):
        return mcp_aml_mock.TOOL_FNS[server["name"]][tool](args)

    monkeypatch.setattr(mcp_registry, "call_tool_unattended", call)
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_aml_{suffix}", f"ws_aml_{suffix}", f"p_aml_{suffix}"
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


def test_loading_the_sample_sets_the_pack_connectors_graph_and_workflow(loaded):
    c, project, result = loaded
    assert result["ok"] and result["pack_id"] == "banking-aml"
    assert ontology_packs.get_project_pack(c, project_id=project).pack_id == "banking-aml"
    assert {s["name"] for s in result["connectors"]} == {"case-management", "kyc", "transactions"}
    assert result["counts"]["customer"] == 40 and result["counts"]["account"] == 60
    assert result["counts"]["alert"] == 3
    assert result["workflow"].get("version_id") or result["workflow"].get("version") is None, \
        result["workflow"]
    assert not result["workflow"].get("skipped"), result["workflow"]
    assert c.execute(text("SELECT count(*) FROM workflow_definitions WHERE project_id = :p "
                          "AND workflow_key = :k"), {"p": project, "k": KEY}).scalar() == 1
    again = aml_sample.load_sample(c, project_id=project, actor="ceo")
    nodes = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND "
                           "node_key LIKE 'aml:customer:%' AND valid_to IS NULL"),
                      {"p": project}).scalar()
    assert again["ok"] and nodes == 40, "loading twice changes nothing"


def test_the_catalogue_skips_the_aml_workflow_where_the_sample_is_not_loaded(connection):
    seeded = catalogue.seed(connection, connection.project_id, actor="ceo")
    aml = next(i for i in seeded["installed"] if i["key"] == KEY)
    assert "banking-aml" in aml["skipped"]


def test_the_deterministic_steps_on_the_generated_bank(loaded):
    c, project, _ = loaded
    intake = pipeline.run_alert_intake(c, project_id=project, run_id="r1",
                                       step_id="alert_intake", alert_id="alert-1")
    assert intake["alert_id"] == "ALERT-1" and intake["account_id"] == data.CAFE_PAYROLL
    assert intake["case_id"] and intake["produced"]["agent_decision"]["decision_type"] == \
        "AlertIntake"
    assert intake["calls"] == [{"mcp": "case-management", "tool": "get_alert", "ok": True}]
    evidence = pipeline.run_evidence_collection(
        c, project_id=project, run_id="r1", step_id="evidence_collection",
        alert_id="ALERT-1", case_id=intake["case_id"])
    assert evidence["kyc"]["occupation"] == "restaurant"
    assert evidence["transactions"]["distinct_payees"] == 14
    assert evidence["past_alerts"] == [] and not evidence["gaps"]
    assert "coverage_gap" not in evidence["produced"]
    history = pipeline.run_aml_historical_pattern(
        c, project_id=project, run_id="r1", step_id="historical_pattern", alert_id="ALERT-1",
        case_id=intake["case_id"], related_party_ids=[])
    assert history["matched"] == [], "nothing has been decided yet"
    with pytest.raises(LookupError):
        pipeline.run_alert_intake(c, project_id=project, run_id="r1", step_id="alert_intake",
                                  alert_id="ALERT-99")


def test_a_connector_that_is_missing_is_a_coverage_gap_not_a_failure(loaded, monkeypatch):
    c, project, _ = loaded
    monkeypatch.setattr(mcp_registry, "call_tool_unattended",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    evidence = pipeline.run_evidence_collection(
        c, project_id=project, run_id="r2", step_id="evidence_collection", alert_id="ALERT-3")
    assert evidence["status"] == "succeeded" and len(evidence["gaps"]) == 3
    assert "down" in evidence["produced"]["coverage_gap"]["statement"]


# ── the full pipeline through the real runtime ───────────────────────────────────────────────

class ScriptedAgents:
    """Answers each role's prompt the way the role prompt asks — from what the prompt shows
    (edge numbers, evidence ids, prior decisions) — never from the ground truth."""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {"network": [], "analyst": [], "narrator": []}

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        assert read_only is True
        if prompt.startswith("# Network Mapper"):
            self.prompts["network"].append(prompt)
            seeds = set(re.findall(r"ALERTED (?:CUSTOMER|ACCOUNT): .*\[(kgn_[0-9a-f]+)\]", prompt))
            parties = []
            for ref, src, dst in re.findall(
                    r"^(E\d+): .*?\[(kgn_[0-9a-f]+)\] —related_to→ .*?\[(kgn_[0-9a-f]+)\]",
                    prompt, re.M):
                other = dst if src in seeds else src
                if other not in seeds:
                    parties.append({"node_id": other, "relationship": "on record", "edges": [ref]})
            answer = {"related_parties": parties, "summary": "scripted"}
        elif prompt.startswith("# AML Analyst"):
            self.prompts["analyst"].append(prompt)
            evidence = prompt.rsplit("EVIDENCE YOU MAY CITE:", 1)[1]
            cited = re.findall(r"- (kgn_[0-9a-f]+):", evidence)[:2]
            marker = "PRIOR DECISIONS ON RELATED ALERTS"
            prior = (prompt.split(marker, 1)[1].split("EVIDENCE YOU MAY CITE:", 1)[0]
                     if marker in prompt else "")
            applied = re.findall(r"- \[(kgn_[0-9a-f]+)\]", prior)
            escalate = "STRUCT-02" in prompt
            answer = {"recommendation": "escalate" if escalate else "close_false_positive",
                      "confidence": 0.84, "rationale": "scripted", "cited": cited,
                      "precedents_applied": applied}
        else:
            self.prompts["narrator"].append(prompt)
            answer = {"narrative": "WHO: Q. Marlow.\n\nWHY: scripted.", "cited": []}
        return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(answer)}\n"
                           f"{pipeline._shared._MARK_CLOSE}"}


def _start(c, project, alert_id):
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    definition_id = c.execute(text("SELECT id FROM workflow_definitions WHERE project_id = :p "
                                   "AND workflow_key = :k"), {"p": project, "k": KEY}).scalar()
    version = c.execute(text("SELECT id FROM workflow_versions WHERE workflow_definition_id = :d "
                             "ORDER BY version DESC LIMIT 1"), {"d": definition_id}).scalar()
    assert version, "load_sample installs the workflow"
    run = workflows.start_run(c, project_id=project, actor="ceo", input_data={"alert_id": alert_id},
                              dedupe_key=f"aml_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=version)
    return run["id"], {s["id"]: s for s in catalogue.entry_spec(entry)["steps"]}


def _drive(c, project, run_id, spec_steps, *, until_gate=None, max_rounds=40):
    """Work the run like the runner does: claim, execute, complete; release timers/conditions.
    Stops at a waiting decision gate (returns its step id) or when the run ends (None)."""
    from types import SimpleNamespace

    ctx = SimpleNamespace(settings=None, engine=None)
    for _ in range(max_rounds):
        claimed = workflows.claim_step(c, project_id=project, run_id=run_id, worker_id=WORKER,
                                       lease_seconds=600)
        if claimed is not None:
            step = spec_steps[claimed["step_id"]]
            output = runner.execute_step(
                c, ctx, project_id=project, actor="ceo", step=step, spec_steps=spec_steps,
                context={**runner._run_context(c, run_id), "run_id": run_id})
            workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                    worker_id=WORKER, output=output, evidence=[])
            continue
        workflows.advance_run(c, project, run_id)
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        if status in workflows.TERMINAL_RUN_STATES:
            return None
        waiting = c.execute(text(
            "SELECT step_id FROM workflow_step_runs WHERE workflow_run_id = :r AND "
            "status = 'waiting' AND decision_id IS NOT NULL"), {"r": run_id}).scalars().all()
        if waiting:
            return waiting[0]
    raise AssertionError("the run did not reach a gate or an end")


def _answer(c, run_id, step_id, *, by, note):
    decision_id = c.execute(text("SELECT decision_id FROM workflow_step_runs WHERE "
                                 "workflow_run_id = :r AND step_id = :s"),
                            {"r": run_id, "s": step_id}).scalar()
    c.execute(text("UPDATE decisions SET status = 'approved', approved_by = :b, "
                   "approved_at = now(), resolution_note = :n WHERE id = :i"),
              {"b": by, "n": note, "i": decision_id})


def _output(c, run_id, step_id):
    return c.execute(text("SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r "
                          "AND step_id = :s ORDER BY iteration DESC LIMIT 1"),
                     {"r": run_id, "s": step_id}).scalar() or {}


def _decision_types(c, project, run_id):
    return set(c.execute(text("SELECT attrs->>'decision_type' FROM kg_nodes WHERE project_id = :p "
                              "AND kind = 'agent_decision' AND attrs->>'run_id' = :r"),
                         {"p": project, "r": run_id}).scalars().all())


REQUIRED = {"AlertIntake", "EvidenceAssembled", "NetworkMapped", "HistoricalPatternMatched",
            "Recommendation", "PrecedentRecorded"}


def test_alert_1_then_alert_2_reuses_the_first_determination(loaded, monkeypatch):
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))

    run1, spec = _start(c, project, "ALERT-1")
    assert _drive(c, project, run1, spec) == "analyst_decision"
    assert _output(c, run1, "aml_analysis")["recommendation"] == "close_false_positive"
    assert "PRIOR DECISIONS: none" in agents.prompts["analyst"][0]
    # No person, no closure: the closure step refuses to run on its own.
    with pytest.raises(pipeline.DeterminationMissing):
        pipeline.run_aml_closure(c, project_id=project, run_id=run1, step_id="aml_closure",
                                 alert_id="ALERT-1")
    _answer(c, run1, "analyst_decision", by="dana.analyst",
            note="Documented biweekly payroll; payroll register Q3 on file.")
    assert _drive(c, project, run1, spec) is None
    closure1 = _output(c, run1, "aml_closure")
    assert closure1["case_status"] == "closed_false_positive"
    assert closure1["decided_by"] == "dana.analyst"
    assert _output(c, run1, "narrative")["drafted"] is False
    precedent = c.execute(text("SELECT trust_tier, attrs FROM kg_nodes WHERE id = :i"),
                          {"i": closure1["precedent_id"]}).mappings().one()
    assert precedent["trust_tier"] == "governed"
    assert precedent["attrs"]["reason"].startswith("Documented biweekly payroll")
    assert REQUIRED <= _decision_types(c, project, run1)
    status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"), {"r": run1}).scalar()
    assert status == "completed"

    run2, _ = _start(c, project, "ALERT-2")
    assert _drive(c, project, run2, spec) == "analyst_decision"
    history = _output(c, run2, "historical_pattern")
    assert closure1["precedent_id"] in history["matched"], history
    prompt = agents.prompts["analyst"][-1]
    assert closure1["precedent_id"] in prompt and "Documented biweekly payroll" in prompt
    analysis = _output(c, run2, "aml_analysis")
    assert analysis["precedents_applied"] == [closure1["precedent_id"]]
    assert "ALERT-1's determination" in analysis["note"]
    _answer(c, run2, "analyst_decision", by="lee.analyst",
            note="Same owner and staff as ALERT-1; documented catering payroll.")
    assert _drive(c, project, run2, spec) is None
    closure2 = _output(c, run2, "aml_closure")
    assert closure2["reused_precedents"] == [closure1["precedent_id"]]
    reused = c.execute(text("SELECT count(*) FROM kg_edges WHERE project_id = :p AND src_id = :s "
                            "AND dst_id = :d AND kind = 'reused'"),
                       {"p": project, "s": closure2["case_id"],
                        "d": closure1["precedent_id"]}).scalar()
    assert reused == 1
    assert REQUIRED <= _decision_types(c, project, run2)


def test_an_escalation_waits_for_a_person_to_file(loaded, monkeypatch):
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))
    run3, spec = _start(c, project, "ALERT-3")
    assert _drive(c, project, run3, spec) == "analyst_decision"
    assert _output(c, run3, "aml_analysis")["recommendation"] == "escalate"
    history = _output(c, run3, "historical_pattern")
    assert history["matched"] == []
    _answer(c, run3, "analyst_decision", by="dana.analyst",
            note="Structuring under the CTR threshold, wired on to related offshore companies.")
    assert _drive(c, project, run3, spec) == "sar_filing"
    narrative = _output(c, run3, "narrative")
    assert narrative["drafted"] and narrative["determination"] == "escalate"
    with pytest.raises(pipeline.DeterminationMissing):
        pipeline.run_aml_closure(c, project_id=project, run_id=run3, step_id="aml_closure",
                                 alert_id="ALERT-3")
    _answer(c, run3, "sar_filing", by="bsa.officer", note="Filed, reference SAR-2026-1031.")
    assert _drive(c, project, run3, spec) is None
    closure = _output(c, run3, "aml_closure")
    assert closure["case_status"] == "escalated_filed" and closure["filed_by"] == "bsa.officer"
    assert "Narrative" in _decision_types(c, project, run3)


def test_a_person_can_decide_against_the_recommendation(loaded, monkeypatch):
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))
    run1, spec = _start(c, project, "ALERT-1")
    assert _drive(c, project, run1, spec) == "analyst_decision"
    _answer(c, run1, "analyst_decision", by="dana.analyst",
            note="Escalate: two payees are not on the payroll register.")
    assert _drive(c, project, run1, spec) == "sar_filing"
    assert _output(c, run1, "narrative")["determination"] == "escalate"


def test_an_unreadable_analyst_answer_is_no_recommendation(loaded, monkeypatch):
    c, project, _ = loaded

    class Mumbles:
        def run_role(self, prompt, cwd, **_kw):
            return {"summary": "I think it is probably fine."}

    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (Mumbles(), "scripted"))
    run1, spec = _start(c, project, "ALERT-1")
    assert _drive(c, project, run1, spec) == "analyst_decision"
    analysis = _output(c, run1, "aml_analysis")
    assert analysis["recommendation"] is None and analysis["confidence"] == 0.0
    assert _output(c, run1, "network_mapping")["related_parties"] == []


def test_loading_the_sample_is_offered_to_the_agent_but_pressed_by_a_person():
    from boss_engine.tools import build_default_registry

    reg = build_default_registry()
    load = reg.get("load_aml_sample")
    assert load.operator_only is True and load.self_executable is False
    assert reg.get("aml_sample_status").kind == "read"


def test_starting_it_needs_only_a_model_and_its_gates_are_a_persons():
    """A gate's approver must be somebody the start door can find: `human`, not a role key no
    project has (which refused the start before the first step)."""
    from boss_engine.services.workflow_readiness import needs_of

    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    assert needs_of(catalogue.entry_spec(entry)) == ["model"]
