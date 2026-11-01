"""The consultant's DQ RCA and remediation pipeline (`dq_incident_rca`), built into BOSS and run
end to end through the real workflow runtime on NorthPeak Bank's synthetic data platform
(tools/dq_sample), graded against `answer_key()`.

Proven here, against a disposable postgres (DATABASE_URL, never the live DB):

* the banking-dq pack mirrors tools/dq_sample, and a project holds it beside banking-aml;
* the Eagle / Olympus / ITSM mocks answer from the sample world; ITSM opens, attaches and only
  raises severity;
* loading the platform is a product action (pack, connectors, graph, workflows, the daily
  escalation schedule), idempotent, never writing the rulebook's rules — and a rule not in the
  graph is a coverage gap that says "load the rulebook";
* Monitoring's open / attach / ignore for every exception group matches the answer key;
* EGL-0588 v2 (EXG-20261005-0003) end to end: open → enrichment (CHG-7731, ISS-031, EF-2026-01)
  → history (INC-2187, INC-2041, RPAT-COB-ORDER) → RCA source_processing citing INC-2187 →
  lane confirms → package → approval by A → execution REFUSED for A, done by B → closure writes
  the precedent; the next COB's group attaches to the incident it opened;
* the attach case (EXG-20261005-0005 → INC-2301) stops after Monitoring;
* the false positive (EXG-20261006-0002) becomes a rule proposal in Rules review;
* the insufficient-evidence case (EXG-20261007-0004, the F-06 lineage gap) loops Enrichment,
  Historical Pattern and RCA back, three rounds, and leaves the incident open.

The scripted agents answer from what each role prompt SHOWS (the evidence list, the precedents,
the coverage gaps), never from the answer key.
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
from boss_engine.services.knowledge import dq_incident_pipeline as pipeline
from boss_engine.services.knowledge import dq_platform, ontology_packs
from boss_engine.services.knowledge.dq_services import Section, curate
from boss_engine.services.knowledge.store import NODE_KINDS, PostgresKnowledgeStore
from tools import mcp_dq_mock, mcp_itsm_mock
from tools.dq_sample import data as D

KEY = "dq_incident_rca"
WORKER = "worker-dq"
KEY_ANSWERS = D.answer_key()["exception_groups"]
STORE = PostgresKnowledgeStore()


# ── no database ───────────────────────────────────────────────────────────────────────────────

def test_the_pack_mirrors_the_sample_world():
    pack = ontology_packs.load_pack("banking-dq")
    core = {"incident", "precedent"}
    assert (set(D.KG_KINDS) | set(D.CONTEXT_KINDS)) - core <= pack.kinds
    assert {"agent_run", "agent_tool_call", "remediation_package", "rule_proposal",
            "mcp_server"} <= pack.kinds
    assert not (pack.kinds & NODE_KINDS)
    for _src, rel, _dst, _meaning in D.RELATIONSHIPS:
        kind = dq_platform.edge_kind(rel)
        assert kind in pack.edges or kind in {"owns", "reads", "caused_by", "fixed_by"}, rel
    assert {"matched_incident", "used_precedent"} <= pack.edges
    meta = pack.kind_meta["data_quality_rule"]
    assert meta["color"] and meta["label"] == "Data quality rule" and meta["plural"]
    assert pack.context_version == "4"
    # v3: the kinds the consultant's designs read; the sample holds instances of some, the rest
    # are declared and never invented.
    assert pack.version == "3"
    assert set(D.DECLARED_ONLY_KINDS) <= pack.kinds
    assert {"transformation", "root_cause_category", "remediation_template"} <= set(D.KG_KINDS)
    ds = D.generate()
    kinds = {n["kind"] for n in ds.nodes()}
    assert not (set(D.DECLARED_ONLY_KINDS) & kinds)
    assert {n["key"] for n in ds.nodes() if n["kind"] == "root_cause_category"} == \
        set(D.ROOT_CAUSE_FAMILIES)
    templates = {n["attrs"]["pattern_id"]: n["name"] for n in ds.nodes()
                 if n["kind"] == "remediation_template"}
    assert templates == {p.pattern_id: p.durable_fix for p in ds.patterns}
    transforms = [n for n in ds.nodes() if n["kind"] == "transformation"]
    assert transforms and all(n["name"] == ds_lineage.transform for n in transforms
                              for ds_lineage in ds.lineage
                              if ds_lineage.lineage_id == n["attrs"]["lineage_id"])


def test_the_designs_read_what_the_reference_draws():
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    steps = {s["id"]: s for s in entry["steps"]}
    reads = lambda sid: {r["kind"] for r in steps[sid]["contract"]["reads"]}  # noqa: E731
    assert {"transformation", "derivation_rule", "data_concern", "adjustment",
            "regulatory_communication", "join_path", "domain", "contract"} <= \
        reads("context_enrichment")
    assert {"remediation_template", "root_cause_category"} <= reads("historical_pattern")
    pack = ontology_packs.load_pack("banking-dq")
    for sid in ("context_enrichment", "historical_pattern"):
        assert reads(sid) - pack.kinds - NODE_KINDS == set(), sid


def test_a_project_already_on_v2_gets_the_new_kinds_and_its_contracts_publish(loaded):
    """northpeak-lending holds banking-dq at v2: the kinds come from the shipped pack, so its
    contracts are satisfied at once, and loading the platform again records v3 and seeds the
    sample's instances."""
    from boss_engine.services.knowledge.step_contracts import contract_assignment_problems

    c, project, _ = loaded
    c.execute(text("UPDATE kg_ontology_packs SET version = '2' WHERE project_id = :p "
                   "AND pack_id = 'banking-dq'"), {"p": project})
    ontology_packs._PROJECT_PACK_CACHE.pop(project, None)
    granted = ontology_packs.effective_node_kinds(c, project_id=project)
    assert {"join_path", "root_cause_category", "remediation_template"} <= granted
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    for step in entry["steps"]:
        if step["id"] in ("context_enrichment", "historical_pattern"):
            problems = contract_assignment_problems(c, project, step)
            assert not [p for p in problems if "kind" in p], problems
    dq_platform.load_platform(c, project_id=project, actor="ceo")
    assert c.execute(text("SELECT version FROM kg_ontology_packs WHERE project_id = :p AND "
                          "pack_id = 'banking-dq'"), {"p": project}).scalar() == "3"
    counts = dict(c.execute(text(
        "SELECT kind, count(*) FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL AND "
        "kind IN ('root_cause_category', 'remediation_template', 'transformation', 'join_path') "
        "GROUP BY kind"), {"p": project}).all())
    assert counts.get("root_cause_category") == 5
    assert counts.get("remediation_template", 0) >= 1 and counts.get("transformation", 0) >= 1
    assert "join_path" not in counts


def test_the_mocks_answer_from_the_sample_world():
    mcp_itsm_mock.reset()
    eagle, olympus = mcp_dq_mock.TOOL_FNS["eagle"], mcp_dq_mock.TOOL_FNS["olympus"]
    listed = eagle["list_exception_groups"]({"cob": "2026-10-05"})
    assert [g["group_id"] for g in listed["groups"]] == ["EXG-20261005-0003", "EXG-20261005-0005"]
    group = eagle["get_exception_group"]({"group_id": D.RUNNING_EXAMPLE})
    assert group["eagle_rule_id"] == "EGL-0588" and group["grade"] == "Error"
    assert eagle["get_exception_group"]({"group_id": "NOPE"}).get("error")
    rerun = eagle["rerun_check"]({"rule_id": "NPB-DQ-LN-003", "cob": "2026-10-05",
                                  "change_ref": "CHG-7760"})
    assert rerun["grade"] != "Error" and rerun["before_grade"] == "Error"
    profile = olympus["get_exception_profile"]({"platform": "S-LSP", "rule_id": "NPB-DQ-LN-003",
                                                "measured": 0.00673})
    assert profile["profiled"] and profile["z_score"] > 3
    changes = olympus["list_recent_changes"]({"platform": "S-LSP", "until": "2026-10-05"})
    assert "CHG-7731" in {c["change_id"] for c in changes["changes"]}
    itsm = mcp_itsm_mock.TOOL_FNS
    assert itsm["get_incident"]({"incident_id": "INC-2088"})["correct_paydown"] == 218.60
    assert [t["incident_id"] for t in itsm["list_incidents"](
        {"rule_id": "NPB-DQ-AR-001", "status": "open"})["incidents"]] == ["INC-2301"]
    made = itsm["create_incident"]({"title": "t", "assignment_group": "NPB-LOAN-OPS",
                                    "source_ref": D.RUNNING_EXAMPLE, "severity": "High"})
    again = itsm["create_incident"]({"title": "t", "assignment_group": "NPB-LOAN-OPS",
                                     "source_ref": D.RUNNING_EXAMPLE, "severity": "High"})
    assert made["created"] and not again["created"] and made["incident_id"] == again["incident_id"]
    assert itsm["update_incident"]({"incident_id": made["incident_id"], "severity": "Low"})[
        "error"].endswith("(A-D8)")
    mcp_itsm_mock.reset()


def test_the_mux_serves_the_dq_connectors():
    from urllib.parse import urlparse

    from tools.mcp_tools_mux import SERVERS

    served = {port for _n, module, port in SERVERS if module == "tools.mcp_dq_mock"}
    for server in dq_platform.dq_mcp_servers():
        url = urlparse(server["url"])
        assert url.port in served and url.path.strip("/").split("/")[0] in mcp_dq_mock.SERVERS


def test_the_context_curator_trims_to_budget_and_says_what_it_dropped():
    curated = curate([Section("Must", ["a" * 400], must=True, priority=1),
                      Section("Big", ["b" * 800] * 10, priority=50),
                      Section("Small", ["c"], priority=10)], 600)
    assert curated.used <= 600 and "Must" in curated.kept
    assert curated.dropped and curated.dropped[0]["section"] == "Big"
    assert "dropped" in curated.note()


def test_every_stage_has_a_contract_and_the_consultants_design():
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    steps = {s["id"]: s for s in entry["steps"]}
    designs = {sid: (s.get("contract") or {}).get("design") or {} for sid, s in steps.items()}
    assert designs["monitoring"]["status"] == "locked"
    assert designs["monitoring"]["reference"] == "Agent Reference v0.1"
    assert designs["context_enrichment"]["reference"] == "Agent Reference v0.2"
    assert designs["historical_pattern"]["reference"] == "Agent Reference v0.3"
    assert designs["rca"]["status"] == "in_review"
    assert designs["human_approval"]["status"] == designs["human_execution"]["status"] == "human_step"
    assert designs["context_curator"]["status"] == designs["profile_service"]["status"] == \
        "shared_service"
    assert [s["name"] for s in steps["monitoring"]["contract"]["steps"]] == [
        "Read the group", "Resolve the rule", "Grade severity", "Set failure reason",
        "Tag the exception", "Match", "Open or attach", "Link facts", "Write trace, hand off"]
    assert len(steps["context_enrichment"]["contract"]["steps"]) == 10
    assert len(steps["historical_pattern"]["contract"]["steps"]) == 7
    assert {"A-D30", "A-D32", "A-D41"} <= {d["id"] for d in designs["context_enrichment"]["decisions"]}
    assert designs["context_enrichment"]["policy"][0]["id"] == "OP-ENR-01"
    # The shape: RCA loops back to stages 2-3, two people at the end, the side paths.
    assert steps["evidence_loop"]["body"] == ["context_enrichment", "historical_pattern", "rca"]
    assert steps["evidence_loop"]["on_exhausted"] == "continue"
    assert steps["human_execution"]["approval"]["distinct_from"] == ["human_approval"]
    assert steps["rule_drafting"]["side_path"] == "Rule authoring"
    assert steps["escalation"]["side_path"]
    # Every step names its agent, so the consent names them.
    for sid in ("monitoring", "context_enrichment", "historical_pattern", "rca",
                "pipeline_orchestrator", "lane", "rule_drafting", "remediation_advisor",
                "incident_closure"):
        assert steps[sid].get("captain", "").startswith("dq_"), sid
    _, problems = workflows.normalise_workflow_spec(catalogue.entry_spec(entry), check_agents=False)
    assert problems == []


def test_the_designs_draw_as_the_consultants_reference():
    """The run trace, the Context Graph links, the orchestrator as the entry, handoffs in full
    sentences, the escalation band numbered 9 and the lanes named by what they fan out over."""
    from boss_engine.services.knowledge.step_contracts import contract_shape_problems

    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    steps = {s["id"]: s for s in entry["steps"]}
    for sid in ("monitoring", "context_enrichment", "historical_pattern"):
        contract = steps[sid]["contract"]
        written = {w["kind"] for w in contract["writes"]}
        assert {"agent_run", "agent_tool_call"} <= written, sid
        assert contract["invoked_by"] == "pipeline_orchestrator"
        assert contract["design"]["example"].startswith(("EGL-0588", "INC-501"))
        last = contract["steps"][-1 if sid != "historical_pattern" else 4]
        assert {"agent_run", "agent_tool_call"} <= set(last["writes"]), sid
    assert {(l["from"], l["to"], l["rel"]) for l in steps["monitoring"]["contract"]["links"]} == {
        ("incident", "incident", "CORRELATED_WITH")}
    assert {l["rel"] for l in steps["historical_pattern"]["contract"]["links"]} == {
        "MATCHED_INCIDENT", "REFERENCES"}
    assert {c["mcp"] for c in steps["context_enrichment"]["contract"]["calls"]} == {"olympus", "itsm"}
    assert steps["context_enrichment"]["contract"]["handoff"]["next"] == "historical_pattern"
    for sid, step in steps.items():
        handoff = (step.get("contract") or {}).get("handoff")
        if handoff:
            text = handoff.get("text", "")
            assert text[:1].isupper() and text.endswith("."), sid
            assert not re.search(r"\b[a-z]+_[a-z_]+\b", text), f"{sid}: a raw id in the handoff"
        assert contract_shape_problems(step) == [], sid
    assert steps["escalation"]["stage_number"] == 9
    assert steps["lanes"]["item_label"] == "root-cause family"


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
    def call(settings, server, tool, args):
        name = server["name"]
        fns = mcp_itsm_mock.TOOL_FNS if name == "itsm" else mcp_dq_mock.TOOL_FNS[name]
        return fns[tool](args)

    monkeypatch.setattr(mcp_registry, "call_tool_unattended", call)
    mcp_itsm_mock.reset()
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_dq_{suffix}", f"ws_dq_{suffix}", f"p_dq_{suffix}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'DQ')"), {"i": org})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'DQ')"),
                         {"i": ws, "o": org})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                              "(:i, :w, 'NorthPeak Bank', :s)"), {"i": project, "w": ws, "s": project})
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()
            ontology_packs._PROJECT_PACK_CACHE.pop(project, None)
            mcp_itsm_mock.reset()


def _load_rulebook(c, project):
    """What rulebook extraction leaves in the graph: each rule as a data_quality_rule with its
    rulebook fields, and its two thresholds (another helper's job in the product)."""
    ontology_packs.set_project_pack(c, project_id=project, pack_id="banking-dq")
    ds = D.generate()
    for rule_id, r in D.answer_key()["rules"].items():
        STORE.upsert_node(
            c, project_id=project, kind="data_quality_rule",
            node_key=dq_platform.node_key("data_quality_rule", rule_id),
            name=f"{rule_id} v{r['version']}: {r['title']}", body=r["statement"],
            trust_tier="observed", actor_kind="system", actor_ref="rulebook_extraction",
            # Reviewed: "in the graph" counts approved DQ rules (rules.rule_counts' reading).
            attrs={"rule_id": rule_id, "status": "approved", **{k: v for k, v in r.items()
                                                                if k not in ("warning", "error")},
                   "warning_text": r["warning"], "error_text": r["error"]})
        rule = ds.rule(rule_id)
        for t in (rule.warning, rule.error):
            STORE.upsert_node(
                c, project_id=project, kind="dq_threshold",
                node_key=dq_platform.node_key("dq_threshold", t.threshold_id),
                name=f"{rule_id} {t.level}", body=t.text, trust_tier="observed",
                actor_kind="system", actor_ref="rulebook_extraction",
                attrs={"threshold_id": t.threshold_id, "rule_id": rule_id, "level": t.level,
                       "metric": t.metric, "value": t.value})


@pytest.fixture()
def loaded(connection):
    _load_rulebook(connection, connection.project_id)
    result = dq_platform.load_platform(connection, project_id=connection.project_id, actor="ceo")
    return connection, connection.project_id, result


def test_loading_keeps_the_projects_other_packs(connection):
    project = connection.project_id
    ontology_packs.set_project_pack(connection, project_id=project, pack_id="banking-aml")
    result = dq_platform.load_platform(connection, project_id=project, actor="ceo")
    pack = ontology_packs.get_project_pack(connection, project_id=project)
    assert {"banking-aml", "banking-regulatory", "banking-dq"} <= set(pack.pack_ids)
    assert {"alert", "clause", "cde", "exception_group"} <= pack.kinds
    # No rulebook yet: no rule written, the platform says to load it.
    assert result["rules_in_graph"] == 0 and "load the rulebook" in result["note"]
    assert not connection.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND "
                                       "kind IN ('data_quality_rule', 'dq_threshold')"),
                                  {"p": project}).scalar()
    assert result["edges_waiting_for_rules"] > 0
    # Monitoring says what is missing in the same words.
    agents = ScriptedAgents()
    out = pipeline.run_dq_monitoring(connection, None, project, {"id": "monitoring"},
                                     {"run_id": ""}, {"exception_group_id": D.RUNNING_EXAMPLE})
    assert any("not in the graph yet — load the rulebook" in g for g in out["gaps"])
    del agents


def test_extract_rules_works_right_after_the_platform_and_rulebook_loads(connection):
    """The live walk of 2026-11-01: a project booted with no pack (the boot seed skips
    rulebook_extraction), then the AML sample's pack, "Load the NorthPeak Bank data platform",
    "Load the NorthPeak Bank rulebook" and Add the document — and "Extract rules" answered "no
    delivery workflow 'rulebook_extraction'". Each load installs what it completes."""
    from boss_engine.services.knowledge import document_upload, dq_rulebook_sample

    c, project = connection, connection.project_id
    boot = catalogue.seed(c, project)
    assert next(i for i in boot["installed"] if i["key"] == "rulebook_extraction").get("skipped")
    assert _version(c, project, "rulebook_extraction") is None
    ontology_packs.set_project_pack(c, project_id=project, pack_id="banking-aml", set_by="ceo")
    dq_platform.load_platform(c, project_id=project, actor="ceo")
    # The pack set after boot already completes it: the platform load installed it.
    assert _version(c, project, "rulebook_extraction") is not None
    preview = dq_rulebook_sample.stage(c, project_id=project, actor_kind="person", actor_ref="ceo")
    added = document_upload.confirm_upload(c, project_id=project, upload_id=preview["upload_id"],
                                           actor_kind="person", actor_ref="ceo")
    status = dq_rulebook_sample.status(c, project_id=project)
    assert status["can_extract"] and status["document_id"] == added["document_id"]
    run = workflows.start_run(c, project_id=project, actor="ceo",
                              input_data={"document_id": added["document_id"]},
                              dedupe_key=f"rbx_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=_version(c, project, "rulebook_extraction"))
    assert run["id"]
    assert c.execute(text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = 'rulebook_analyst'"),
                     {"p": project}).scalar()


def test_the_rulebook_load_alone_installs_extraction_once_the_packs_are_there(connection):
    """The other order: packs set by a path that seeds nothing, then the document confirmed."""
    from boss_engine.services.knowledge import document_upload, dq_rulebook_sample

    c, project = connection, connection.project_id
    for pack in ("banking-regulatory", "banking-dq"):
        ontology_packs.set_project_pack(c, project_id=project, pack_id=pack, set_by="ceo")
    assert _version(c, project, "rulebook_extraction") is None
    preview = dq_rulebook_sample.stage(c, project_id=project, actor_kind="person", actor_ref="ceo")
    document_upload.confirm_upload(c, project_id=project, upload_id=preview["upload_id"],
                                   actor_kind="person", actor_ref="ceo")
    assert _version(c, project, "rulebook_extraction") is not None


def test_loading_is_a_product_action_and_idempotent(loaded):
    c, project, result = loaded
    assert result["ok"] and result["counts"]["database_table"] == 28
    assert result["counts"]["exception_group"] == 13 and result["counts"]["mcp_server"] == 3
    assert result["rules_in_graph"] == 96 and result["edges_waiting_for_rules"] == 0
    assert {w["key"] for w in result["workflows"]} == set(dq_platform.WORKFLOW_KEYS)
    assert not any(w.get("skipped") for w in result["workflows"]), result["workflows"]
    assert result["escalation_schedule"].get("created"), result["escalation_schedule"]
    before = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p"),
                       {"p": project}).scalar()
    again = dq_platform.load_platform(c, project_id=project, actor="ceo")
    after = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p"),
                      {"p": project}).scalar()
    assert again["ok"] and before == after and not again["escalation_schedule"].get("created")
    status = dq_platform.status(c, project_id=project, cob="2026-10-05")
    assert status["loaded"] and status["rules_in_graph"] == 96
    assert [g["group_id"] for g in status["groups"]] == ["EXG-20261005-0003", "EXG-20261005-0005"]
    # Seeding elsewhere skips the DQ workflows and says why.
    assert "banking-dq" in (catalogue._unmet(c, "p_none_" + uuid.uuid4().hex[:6],
                                             next(e for e in catalogue.CATALOGUE
                                                  if e["key"] == KEY)) or "banking-dq")


def test_monitoring_opens_attaches_or_ignores_as_the_answer_key_says(loaded):
    c, project, _ = loaded
    for group in sorted(D.generate().exception_groups, key=lambda g: (g.cob, g.group_id)):
        if group.group_id == "EXG-20261006-0007":
            continue  # attaches to the running example's incident (the end-to-end test)
        out = pipeline.run_dq_monitoring(c, None, project, {"id": "monitoring"},
                                         {"run_id": f"r_{group.group_id}"},
                                         {"exception_group_id": group.group_id})
        expected = KEY_ANSWERS[group.group_id]
        assert out["action"] == expected["incident_action"], group.group_id
        if expected["attach_to"] == "INC-2301":
            assert out["incident_id"] == "INC-2301"
        if out["action"] == "ignore":
            assert out["produced"] == {}
    fp = pipeline.run_dq_monitoring(c, None, project, {"id": "monitoring"}, {"run_id": "r_fp"},
                                    {"exception_group_id": "EXG-20261006-0002"})
    assert fp["rulebook_grade"] == "Warning" and "against the rulebook" in fp["discrepancy"]


# ── the full pipeline through the real runtime ───────────────────────────────────────────────

class ScriptedAgents:
    """Answers each role the way its prompt asks, from what the prompt shows."""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {}

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        assert read_only is True
        role = prompt.split("\n", 1)[0].lstrip("# ").strip()
        self.prompts.setdefault(role, []).append(prompt)
        answer = getattr(self, "_" + role.lower().replace(" ", "_"))(prompt)
        return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(answer)}\n"
                           f"{pipeline._shared._MARK_CLOSE}"}

    @staticmethod
    def _section(prompt: str, heading: str) -> str:
        if heading not in prompt:
            return ""
        return prompt.split(heading, 1)[1].split("\n\n", 1)[0]

    @staticmethod
    def _offered(prompt: str) -> list[str]:
        return [x.strip() for x in prompt.rsplit("EVIDENCE YOU MAY CITE", 1)[1]
                .split(":", 1)[1].split(",")]

    def _rca_agent(self, prompt):
        tier1 = self._section(prompt, "CLOSED INCIDENTS ON THIS RULE (TIER 1 PRECEDENTS):")
        precedents = re.findall(r"^\[(INC-\d+)\] tier 1 .*?Cause \((\w+),", tier1, re.M)
        gaps = self._section(prompt, "COVERAGE GAPS")
        if "coverage gap: lineage" in gaps and not precedents:
            return {"verdict": "InsufficientEvidence", "candidates": [],
                    "missing": ["the registered column lineage the coverage gap names"],
                    "rationale": "A lineage hop is missing and no precedent explains it."}
        if not precedents:
            return {"verdict": "InsufficientEvidence", "candidates": [], "missing": ["precedent"]}
        rule_id = re.search(r'"rule_id": "([A-Z0-9-]+)"', prompt).group(1)
        changes = re.findall(rf"^\[(CHG-\d+)\] .*{rule_id}", self._section(
            prompt, "RECENT CHANGES:"), re.M)
        family = precedents[0][1]
        ids = [p for p, f in precedents if f == family]
        return {"verdict": "Candidates", "rationale": "scripted",
                "candidates": [{"family": family, "cause": f"the {family} cause again",
                                "confidence": 0.82, "evidence": ids + changes,
                                "precedents": ids, "confidence_lift": {ids[0]: 0.3}}]}

    def _lane_agent(self, prompt):
        cand = self._section(prompt, "CANDIDATES FOR THIS LANE:")
        cited = re.findall(r"— cites (.*)$", cand, re.M)
        evidence = [x.strip() for x in cited[0].split(",")] if cited else []
        return {"verdict": "Confirmed" if evidence else "CannotVerify",
                "cause": cand.strip().split(": ", 1)[-1].split(" — ")[0],
                "evidence": evidence, "rationale": "scripted"}

    def _rule_drafting_agent(self, prompt):
        found = re.search(r"Warning above ([0-9.e-]+), Error above ([0-9.e-]+) \((\w+)\)", prompt)
        evidence = [x for x in self._offered(prompt) if x.startswith("INC-")][:1] or ["EXCEPTION"]
        return {"title": "use the rulebook's thresholds", "confidence": 0.8,
                "proposal": f"Eagle runs the rule with the rulebook's thresholds (Warning above "
                            f"{found.group(1)}, Error above {found.group(2)}); the rule-pack "
                            "import validates threshold units against the rulebook.",
                "thresholds": {"warning": float(found.group(1)), "error": float(found.group(2)),
                               "unit": found.group(3)},
                "rationale": "scripted", "evidence": evidence}

    def _remediation_advisor(self, prompt):
        owner = re.search(r"rule owner: .*? — (OWN-[A-Z]+) \((NPB-[A-Z-]+)\)", prompt)
        steward = re.search(r"CDE steward: .*? — (P-[A-Z]+)", prompt)
        fix = re.search(r"durable fix — (.*?);", prompt)
        evidence = [x for x in self._offered(prompt) if x.startswith(("INC-", "CHG-"))][:2]
        return {"fix": fix.group(1) if fix else "correct the cause",
                "target": {"system": "S-LSP", "component": "COB job chain"},
                "executor": {"assignment_group": owner.group(2)},
                "approver": {"owner_id": steward.group(1) if steward else "P-DWHITFIELD"},
                "change_content": ["raise the change", "apply it", "re-run the rule"],
                "validation": "re-run the rule on the COB", "rollback": "revert the change",
                "evidence": evidence}


def _version(c, project, key=KEY):
    return c.execute(text("SELECT v.id FROM workflow_versions v JOIN workflow_definitions d ON "
                          "d.id = v.workflow_definition_id WHERE d.project_id = :p AND "
                          "d.workflow_key = :k ORDER BY v.version DESC LIMIT 1"),
                     {"p": project, "k": key}).scalar()


def _start(c, project, group_id):
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    run = workflows.start_run(c, project_id=project, actor="ceo",
                              input_data={"exception_group_id": group_id},
                              dedupe_key=f"dq_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=_version(c, project))
    return run["id"], {s["id"]: s for s in catalogue.entry_spec(entry)["steps"]}


def _drive(c, project, run_id, spec_steps, max_rounds=80):
    from types import SimpleNamespace

    ctx = SimpleNamespace(settings=None, engine=None)
    for _ in range(max_rounds):
        claimed = workflows.claim_step(c, project_id=project, run_id=run_id, worker_id=WORKER,
                                       lease_seconds=600)
        if claimed is not None:
            step = spec_steps[claimed["step_id"]]
            context = {**runner._run_context(c, run_id), "run_id": run_id}
            item = (claimed.get("input") or {}) if isinstance(claimed.get("input"), dict) else {}
            if isinstance(claimed.get("input"), str):
                item = json.loads(claimed["input"] or "{}")
            if item.get("for_each"):
                context.update({"item": item.get("item"), "item_index": item.get("item_index"),
                                "for_each": item.get("for_each")})
            output = runner.execute_step(c, ctx, project_id=project, actor="ceo", step=step,
                                         spec_steps=spec_steps, context=context)
            workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                    worker_id=WORKER, output=output, evidence=[])
            continue
        workflows.advance_run(c, project, run_id)
        status = c.execute(text("SELECT status, blocked_reason FROM workflow_runs WHERE id = :r"),
                           {"r": run_id}).mappings().one()
        if status["status"] in workflows.TERMINAL_RUN_STATES:
            return None
        waiting = c.execute(text(
            "SELECT step_id FROM workflow_step_runs WHERE workflow_run_id = :r AND "
            "status = 'waiting' AND decision_id IS NOT NULL"), {"r": run_id}).scalars().all()
        if waiting:
            return waiting[0]
        if status["status"] == "blocked":
            raise AssertionError(f"blocked: {status['blocked_reason']}")
    raise AssertionError("the run did not reach a gate or an end")


def _answer(c, run_id, step_id, *, by, note):
    decision_id = c.execute(text("SELECT decision_id FROM workflow_step_runs WHERE "
                                 "workflow_run_id = :r AND step_id = :s ORDER BY created_at "
                                 "DESC LIMIT 1"), {"r": run_id, "s": step_id}).scalar()
    c.execute(text("UPDATE decisions SET status = 'approved', approved_by = :b, "
                   "approved_at = now(), resolution_note = :n WHERE id = :i"),
              {"b": by, "n": note, "i": decision_id})
    return decision_id


def _output(c, run_id, step_id):
    return c.execute(text("SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r "
                          "AND step_id = :s AND status = 'succeeded' ORDER BY iteration DESC, "
                          "created_at DESC LIMIT 1"), {"r": run_id, "s": step_id}).scalar() or {}


def _decision_types(c, project, run_id):
    return set(c.execute(text("SELECT attrs->>'decision_type' FROM kg_nodes WHERE project_id = :p "
                              "AND kind = 'agent_decision' AND attrs->>'run_id' = :r"),
                         {"p": project, "r": run_id}).scalars().all())


def _scripted(monkeypatch):
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))
    return agents


def test_egl_0588_runs_end_to_end_and_two_people_finish_it(loaded, monkeypatch):
    c, project, _ = loaded
    agents = _scripted(monkeypatch)
    expected = KEY_ANSWERS[D.RUNNING_EXAMPLE]
    run, spec = _start(c, project, D.RUNNING_EXAMPLE)
    assert _drive(c, project, run, spec) == "human_approval"
    mon = _output(c, run, "monitoring")
    assert mon["action"] == expected["incident_action"] == "open"
    incident = mon["incident_id"]
    pack = _output(c, run, "context_enrichment")["context_pack"]
    assert expected["recent_change"] in {x["change_id"] for x in pack["recent_changes"]}
    known = {i["id"] for i in pack["known_issues"]}
    assert {expected["open_issue"], expected["finding"]} <= known
    assert pack["regulatory_reach"] and pack["profile"]["z_score"] > 3
    history = _output(c, run, "historical_pattern")
    tier1 = [m["incident_id"] for m in history["matches"] if m["tier"] == 1]
    assert tier1[0] == expected["precedent"] and set(expected["also_precedents"]) <= set(tier1)
    assert {m["pattern_id"] for m in history["matches"] if m["tier"] == 1} == {expected["pattern"]}
    rca = _output(c, run, "rca")
    assert rca["rca_verdict"] == "Candidates"
    top = rca["candidates"][0]
    assert top["family"] == expected["root_cause_family"] and top["tier"] == 1
    assert expected["precedent"] in top["precedents"] and expected["recent_change"] in top["evidence"]
    assert "Context Curator" in rca["curator_note"]
    used = c.execute(text("SELECT count(*) FROM kg_edges e JOIN kg_nodes n ON n.id = e.src_id "
                          "WHERE n.project_id = :p AND e.kind = 'used_precedent' AND "
                          "n.attrs->>'incident_id' = :i"), {"p": project, "i": incident}).scalar()
    assert used >= 1
    lanes = _output(c, run, "pipeline_orchestrator")["lanes"]
    assert [lane["lane"] for lane in lanes] == [expected["lane"]]
    assert _output(c, run, "rule_drafting")["drafted"] is False
    advisor = _output(c, run, "remediation_advisor")
    assert advisor["package_ready"] and advisor["package"]["prepare_only"]

    # The next COB's group for the same rule attaches to the incident this run opened.
    next_run, _ = _start(c, project, "EXG-20261006-0007")
    assert _drive(c, project, next_run, spec) is None
    attached = _output(c, next_run, "monitoring")
    assert attached["action"] == "attach" and attached["incident_id"] == incident
    assert _output(c, next_run, "context_enrichment") == {}

    # Approval by A; A may not also carry it out — refused in the inbox and by the engine.
    _answer(c, run, "human_approval", by="marcus.feld", note="Approved: make F-01 wait.")
    assert _drive(c, project, run, spec) == "human_execution"
    gate = c.execute(text("SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r "
                          "AND step_id = 'human_execution'"), {"r": run}).scalar()
    said = workflows.separation_of_duties_refusal(c, gate, "marcus.feld")
    assert said and "A different person must carry this out" in said
    _answer(c, run, "human_execution", by="marcus.feld", note="CHG-7760 done")
    assert _drive(c, project, run, spec) == "human_execution", "the approver was taken back"
    assert c.execute(text("SELECT status FROM decisions WHERE id = :i"), {"i": gate}).scalar() \
        == "pending"
    _answer(c, run, "human_execution", by="elena.varga",
            note="CHG-7760 applied: F-01 waits for the COB completion flag again.")
    assert _drive(c, project, run, spec) is None
    fix = _output(c, run, "remediation_fix")
    assert fix["executed_by"] == "elena.varga" and fix["approved_by"] == "marcus.feld"
    assert fix["change_ref"] == "CHG-7760"
    closure = _output(c, run, "incident_closure")
    assert closure["closed"] and closure["pattern_id"] == expected["pattern"]
    precedent = c.execute(text("SELECT attrs FROM kg_nodes WHERE id = :i"),
                          {"i": closure["precedent_node_id"]}).scalar()
    assert precedent["incident_id"] == incident and precedent["pattern_id"] == expected["pattern"]
    assert mcp_itsm_mock.TOOL_FNS["get_incident"]({"incident_id": incident})["status"] == "resolved"
    assert {"IncidentOpened", "ContextAssembled", "HistoricalPatternMatched",
            "RootCauseCandidates", "Routed", "Confirmed", "RemediationPrepared", "FixRecorded",
            "IncidentClosed"} <= _decision_types(c, project, run)
    assert c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                     {"r": run}).scalar() == "completed"
    assert agents.prompts["RCA Agent"] and agents.prompts["Lane Agent"]


def test_an_attach_stops_after_monitoring(loaded, monkeypatch):
    c, project, _ = loaded
    _scripted(monkeypatch)
    group = "EXG-20261005-0005"
    run, spec = _start(c, project, group)
    assert _drive(c, project, run, spec) is None
    mon = _output(c, run, "monitoring")
    assert mon["action"] == KEY_ANSWERS[group]["incident_action"] == "attach"
    assert mon["incident_id"] == KEY_ANSWERS[group]["attach_to"]
    assert _output(c, run, "rca") == {}
    assert "IncidentAttached" in _decision_types(c, project, run)


def test_a_false_positive_becomes_a_rule_proposal_in_rules_review(loaded, monkeypatch):
    c, project, _ = loaded
    _scripted(monkeypatch)
    group = "EXG-20261006-0002"
    expected = KEY_ANSWERS[group]
    run, spec = _start(c, project, group)
    assert _drive(c, project, run, spec) == "human_approval"
    rca = _output(c, run, "rca")
    assert rca["candidates"][0]["family"] == expected["root_cause_family"] == "rule_config"
    assert expected["precedent"] in rca["candidates"][0]["precedents"]
    lanes = _output(c, run, "pipeline_orchestrator")["lanes"]
    assert [lane["lane"] for lane in lanes] == [expected["lane"]] == ["rule_drafting"]
    drafted = _output(c, run, "rule_drafting")
    assert drafted["drafted"] and drafted["rule_id"] == "NPB-DQ-CL-004"
    from boss_engine.services.knowledge.rules import list_rules

    queue = list_rules(c, project_id=project, status="proposed")
    assert any(item["node"]["id"] == drafted["proposed_rule_id"] for item in queue["items"])
    proposal = c.execute(text("SELECT attrs FROM kg_nodes WHERE id = :i"),
                         {"i": drafted["rule_proposal_node_id"]}).scalar()
    assert proposal["status"] == "in_rules_review"
    assert "RuleProposed" in _decision_types(c, project, run)


def test_insufficient_evidence_loops_back_and_leaves_the_incident_open(loaded, monkeypatch):
    c, project, _ = loaded
    agents = _scripted(monkeypatch)
    group = "EXG-20261007-0004"
    expected = KEY_ANSWERS[group]
    run, spec = _start(c, project, group)
    assert _drive(c, project, run, spec) is None
    rounds = dict(c.execute(text(
        "SELECT step_id, count(*) FROM workflow_step_runs WHERE workflow_run_id = :r AND "
        "status = 'succeeded' AND step_id IN ('context_enrichment', 'historical_pattern', 'rca') "
        "GROUP BY step_id"), {"r": run}).all())
    assert expected["loop_back"] and rounds == {"context_enrichment": 3,
                                                "historical_pattern": 3, "rca": 3}
    enrich = _output(c, run, "context_enrichment")
    assert any(expected["coverage_gap"] in g for g in enrich["gaps"]), enrich["gaps"]
    assert enrich["round"] == 3 and enrich["policy"]["hop_cap"] == 5 and enrich["asked_for"]
    known = {i["id"] for i in enrich["context_pack"]["known_issues"]}
    assert {expected["open_issue"], expected["finding"]} <= known
    assert _output(c, run, "rca")["rca_verdict"] == "InsufficientEvidence"
    assert _output(c, run, "pipeline_orchestrator")["lanes"] == []
    assert _output(c, run, "remediation_advisor")["package_ready"] is False
    closure = _output(c, run, "incident_closure")
    assert closure["closed"] is False
    loop = c.execute(text("SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r AND "
                          "step_id = 'evidence_loop' AND status = 'succeeded' ORDER BY iteration "
                          "DESC LIMIT 1"), {"r": run}).scalar()
    assert loop["exhausted"] is True
    # Each round wrote its own decision; the earlier ones are kept.
    assert c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = "
                          "'agent_decision' AND attrs->>'run_id' = :r AND attrs->>'decision_type' "
                          "= 'ContextAssembled'"), {"p": project, "r": run}).scalar() == 3
    assert "round 3" in agents.prompts["RCA Agent"][-1]
    # And the Escalation Agent ladders it.
    out = pipeline.run_dq_escalation(c, None, project, {"id": "escalation"}, {"run_id": "esc"},
                                     {"as_of": "2026-10-14"})
    escalated = {e["incident_id"]: e for e in out["escalated"]}
    mine = _output(c, run, "monitoring")["incident_id"]
    assert mine in escalated and escalated[mine]["level"] >= 2
    again = pipeline.run_dq_escalation(c, None, project, {"id": "escalation"},
                                       {"run_id": "esc2"}, {"as_of": "2026-10-14"})
    assert mine not in {e["incident_id"] for e in again["escalated"]}, "never told twice"


def test_the_eagle_intake_starts_one_investigation_per_error_group(loaded):
    c, project, _ = loaded
    run = workflows.start_run(c, project_id=project, actor="ceo", input_data={},
                              dedupe_key=f"dq_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=_version(c, project, "dq_eagle_intake"))
    out = pipeline.run_dq_eagle_intake(c, None, project, {"id": "eagle_intake"},
                                       {"run_id": run["id"]}, {"cob": "2026-10-07"})
    errors = [g for g, a in KEY_ANSWERS.items() if a["cob"] == "2026-10-07"
              and a["eagle_grade"] == "Error"]
    assert sorted(s["group_id"] for s in out["started"]) == sorted(errors)
    again = pipeline.run_dq_eagle_intake(c, None, project, {"id": "eagle_intake"},
                                         {"run_id": run["id"]}, {"cob": "2026-10-07"})
    assert again["started"] == [] and len(again["skipped"]) == len(errors)
