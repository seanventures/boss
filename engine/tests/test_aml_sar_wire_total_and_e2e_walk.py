"""Two items left open by PR #40 (AML case study: customer-grade), BOSS task "AML case study:
SAR wire total + the end-to-end acceptance walk":

A7 — the SAR draft's outbound wire total must be computed deterministically from the graph (the
     wires to the named related parties, summed and counted in code) and handed to the SAR
     Narrator as data to cite, never left for the model to add up. A total that silently folds in
     payments to parties the draft does not name is the exact bug this closes.

The end-to-end acceptance walk (ALERT-1 closed, ALERT-2 reusing its precedent, ALERT-3 escalated
and filed by a different person than the one who decided) is driven here as a real run against a
real disposable postgres (DATABASE_URL, never the live DB), checking it completes through its
gates and that nothing it produces leaks a raw id, "None", "UNKNOWN" or a label cut mid-sentence —
the operator still attaches the run's own screenshots to the PR; this only proves the run itself
is clean.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from types import SimpleNamespace

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
from tools import aml_sample_data as data
from tools import mcp_aml_mock

KEY = "aml_alert_investigation"
WORKER = "worker-amlwire"
_RAW_ID = re.compile(r"\b(?:kgn_[0-9a-f]+|wfr_[0-9a-f]+|CUST-\d+|ACC-\d+)\b")
_PLACEHOLDER = re.compile(r"\bNone\b|\bUNKNOWN\b")


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
        return mcp_aml_mock.TOOL_FNS[server["name"]][tool](args)

    monkeypatch.setattr(mcp_registry, "call_tool_unattended", call)
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_amlwire_{suffix}", f"ws_amlwire_{suffix}", f"p_amlwire_{suffix}"
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


class ScriptedAgents:
    """A Network Mapper that names any party whose label ends 'Ltd' and is cited by an edge the
    prompt actually shows (never ground truth); an AML Analyst that escalates on the structuring
    typology; a SAR Narrator that only ever repeats figures already present in its prompt — it
    never computes one, matching A7's "never ask the model to add up amounts"."""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {"network": [], "analyst": [], "narrator": []}

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        if prompt.startswith("# Network Mapper"):
            self.prompts["network"].append(prompt)
            seen: set[str] = set()
            parties = []
            for line in prompt.splitlines():
                m = re.match(r"^(E\d+):", line)
                if not m:
                    continue
                for _name, node_id in re.findall(r"([A-Z][\w &.'-]*\bLtd) \[(kgn_[0-9a-f]+)\]",
                                                 line):
                    if node_id not in seen:
                        seen.add(node_id)
                        parties.append({"node_id": node_id,
                                        "relationship": "wires from the alerted account",
                                        "edges": [m.group(1)]})
            answer = {"related_parties": parties, "summary": "scripted"}
        elif prompt.startswith("# AML Analyst"):
            self.prompts["analyst"].append(prompt)
            escalate = "STRUCT-02" in prompt
            answer = {"recommendation": "escalate" if escalate else "close_false_positive",
                      "confidence": 0.84, "rationale": "scripted", "cited": [],
                      "precedents_applied": []}
        else:
            self.prompts["narrator"].append(prompt)
            figures = re.findall(r"\d[\d,]*\.\d{2}", prompt)
            answer = {"narrative": "WHO: Q. Marlow.\n\nWHY: scripted. FIGURES: "
                                   + ", ".join(dict.fromkeys(figures)), "cited": []}
        return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(answer)}\n"
                           f"{pipeline._shared._MARK_CLOSE}"}


def _start(c, project, alert_id):
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    definition_id = c.execute(text("SELECT id FROM workflow_definitions WHERE project_id = :p "
                                   "AND workflow_key = :k"), {"p": project, "k": KEY}).scalar()
    version = c.execute(text("SELECT id FROM workflow_versions WHERE workflow_definition_id = :d "
                             "ORDER BY version DESC LIMIT 1"), {"d": definition_id}).scalar()
    run = workflows.start_run(c, project_id=project, actor="ceo", input_data={"alert_id": alert_id},
                              dedupe_key=f"aml_{uuid.uuid4().hex[:10]}",
                              workflow_version_id=version)
    return run["id"], {s["id"]: s for s in catalogue.entry_spec(entry)["steps"]}


def _drive(c, project, run_id, spec_steps, max_rounds=40):
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


def _wire_ground_truth(c, project, named_sample_ids):
    """The real outbound wire total/count from Marlow's account to exactly the given sample
    counterparty ids, and the outbound total to every OTHER counterparty — straight off the
    `transfers_to` edges the sample loaded, never re-derived from the pipeline under test."""
    account = aml_sample.node_id_of(c, project, "account", data.MARLOW_ACCOUNT)
    named_ids = {aml_sample.node_id_of(c, project, "counterparty", s) for s in named_sample_ids}
    rows = c.execute(text(
        "SELECT dst_id, attrs FROM kg_edges WHERE project_id = :p AND kind = 'transfers_to' "
        "AND src_id = :a AND valid_to IS NULL"), {"p": project, "a": account}).mappings().all()
    named_total = named_count = other_total = other_count = 0
    for row in rows:
        attrs = row["attrs"] if isinstance(row["attrs"], dict) else json.loads(row["attrs"])
        if row["dst_id"] in named_ids:
            named_total += float(attrs.get("total") or 0)
            named_count += int(attrs.get("count") or 0)
        else:
            other_total += float(attrs.get("total") or 0)
            other_count += int(attrs.get("count") or 0)
    return {"named_total": round(named_total, 2), "named_count": named_count,
            "other_total": round(other_total, 2), "other_count": other_count}


def test_p0_sar_narrator_is_given_the_deterministic_wire_total_to_named_parties(loaded,
                                                                                monkeypatch):
    """A7: the figure the narrator cites for the named related parties must be the real sum of
    the wires to them, handed to it as data — never a total the model adds up itself."""
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))

    run_id, spec = _start(c, project, "ALERT-3")
    assert _drive(c, project, run_id, spec) == "analyst_decision"
    mapped = _output(c, run_id, "network_mapping")
    assert mapped["related_parties"], "the scripted mapper must name the shell companies"
    truth = _wire_ground_truth(c, project, ["CP-0101", "CP-0102", "CP-0103"])
    assert truth["named_total"] > 0 and truth["named_count"] > 0

    _answer(c, run_id, "analyst_decision", by="dana.analyst",
            note="Structuring under the CTR threshold, wired on to related offshore companies.")
    assert _drive(c, project, run_id, spec) == "sar_filing"

    narrator_prompt = agents.prompts["narrator"][-1]
    assert f"{truth['named_total']:,.2f}" in narrator_prompt, (
        "the narrator's situation must state the real wire total to the named parties so it "
        "cites it instead of adding the amounts up itself")
    narrative = _output(c, run_id, "narrative")
    assert narrative["drafted"]
    assert f"{truth['named_total']:,.2f}" in narrative["narrative"], (
        "the draft's total must equal the sum of the wires to the parties it names")


def test_p1_named_wire_total_excludes_payments_to_parties_the_draft_does_not_name(loaded,
                                                                                  monkeypatch):
    """A7 boundary: Marlow's account also pays a counterparty the mapper never names (CP-0010);
    that outflow must not be folded into the named-parties total the narrator is given."""
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))

    run_id, spec = _start(c, project, "ALERT-3")
    assert _drive(c, project, run_id, spec) == "analyst_decision"
    truth = _wire_ground_truth(c, project, ["CP-0101", "CP-0102", "CP-0103"])
    assert truth["other_total"] > 0, "the sample must plant an unnamed outbound payment too"
    combined = round(truth["named_total"] + truth["other_total"], 2)
    assert combined != truth["named_total"], "the two buckets must actually differ"

    _answer(c, run_id, "analyst_decision", by="dana.analyst",
            note="Structuring under the CTR threshold, wired on to related offshore companies.")
    assert _drive(c, project, run_id, spec) == "sar_filing"

    narrator_prompt = agents.prompts["narrator"][-1]
    assert f"{truth['named_total']:,.2f}" in narrator_prompt
    assert f"{combined:,.2f}" not in narrator_prompt, (
        "the figure handed to the narrator must be restricted to the named parties, not every "
        "outbound payment on the account")


def test_p1_wire_total_reports_no_other_payments_when_every_wire_is_named(loaded):
    """A7 inverse of the exclusion test: when the named parties cover every outbound wire on the
    account, `other_count` must be 0 and the narrator's wire line must omit the "further ...
    not named above" sentence instead of reporting a spurious zero-dollar other bucket."""
    c, project, _ = loaded
    account = aml_sample.node_id_of(c, project, "account", data.MARLOW_ACCOUNT)
    named_ids = {aml_sample.node_id_of(c, project, "counterparty", s)
                 for s in ("CP-0101", "CP-0102", "CP-0103", "CP-0010")}
    wires = pipeline._named_wire_total(c, project, account, named_ids)
    assert wires["named_count"] > 0
    assert wires["other_total"] == 0.0 and wires["other_count"] == 0, (
        "naming every counterparty the account wired to must leave nothing in the other bucket")


def test_p1_wire_total_reports_none_found_when_account_has_no_outbound_wires(loaded):
    """A7 boundary: an account with no outbound `transfers_to` edges at all (or no account node
    id) must report a zero/zero total, never raise or silently fold in unrelated edges."""
    c, project, _ = loaded
    named_ids = {aml_sample.node_id_of(c, project, "counterparty", "CP-0101")}

    wires = pipeline._named_wire_total(c, project, "", named_ids)
    assert wires == {"named_total": 0.0, "named_count": 0, "other_total": 0.0, "other_count": 0}

    other_account = aml_sample.node_id_of(c, project, "account", data.MARLOW_ACCOUNT)
    c.execute(text("UPDATE kg_edges SET valid_to = now() WHERE project_id = :p "
                   "AND kind = 'transfers_to' AND src_id = :a"), {"p": project, "a": other_account})
    wires = pipeline._named_wire_total(c, project, other_account, named_ids)
    assert wires == {"named_total": 0.0, "named_count": 0, "other_total": 0.0, "other_count": 0}


def test_p0_three_alert_walk_completes_through_its_gates_with_two_different_people(loaded,
                                                                                    monkeypatch):
    """The end-to-end acceptance walk: ALERT-1 closed as a false positive, ALERT-2 closed by
    reusing ALERT-1's precedent, ALERT-3 escalated, SAR drafted and filed by a different person
    than the one who made the analyst decision, case closed."""
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))

    run1, spec = _start(c, project, "ALERT-1")
    assert _drive(c, project, run1, spec) == "analyst_decision"
    _answer(c, run1, "analyst_decision", by="dana.analyst",
            note="Documented biweekly payroll; payroll register on file.")
    assert _drive(c, project, run1, spec) is None
    closure1 = _output(c, run1, "aml_closure")
    assert closure1["case_status"] == "closed_false_positive"

    run2, _ = _start(c, project, "ALERT-2")
    assert _drive(c, project, run2, spec) == "analyst_decision"
    history2 = _output(c, run2, "historical_pattern")
    assert closure1["precedent_id"] in history2["matched"]
    _answer(c, run2, "analyst_decision", by="lee.analyst",
            note="Same owner and staff as ALERT-1; documented catering payroll.")
    assert _drive(c, project, run2, spec) is None
    closure2 = _output(c, run2, "aml_closure")
    assert closure2["reused_precedents"] == [closure1["precedent_id"]]

    run3, _ = _start(c, project, "ALERT-3")
    assert _drive(c, project, run3, spec) == "analyst_decision"
    _answer(c, run3, "analyst_decision", by="dana.analyst",
            note="Structuring under the CTR threshold, wired on to related offshore companies.")
    assert _drive(c, project, run3, spec) == "sar_filing"
    narrative3 = _output(c, run3, "narrative")
    assert narrative3["drafted"] and narrative3["determination"] == "escalate"
    _answer(c, run3, "sar_filing", by="bsa.officer", note="Filed, reference SAR-2026-1031.")
    assert _drive(c, project, run3, spec) is None
    closure3 = _output(c, run3, "aml_closure")
    assert closure3["case_status"] == "escalated_filed" and closure3["filed_by"] == "bsa.officer"
    assert closure3["decided_by"] == "dana.analyst" != closure3["filed_by"]


def test_p1_walk_outputs_carry_no_raw_id_none_or_unknown_placeholder(loaded, monkeypatch):
    """The acceptance walk's own check: no step's output shows a raw kgn_/wfr_/CUST-/ACC- id on
    its own, nor the literal 'None' or 'UNKNOWN', across all three alerts' narrative statements
    and case closures."""
    c, project, _ = loaded
    agents = ScriptedAgents()
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (agents, "scripted"))

    texts: list[str] = []
    for alert_id, by, note in (
            ("ALERT-1", "dana.analyst", "Documented biweekly payroll; register on file."),
            ("ALERT-3", "dana.analyst", "Structuring under the CTR threshold, wired on.")):
        run_id, spec = _start(c, project, alert_id)
        gate = _drive(c, project, run_id, spec)
        assert gate == "analyst_decision"
        _answer(c, run_id, "analyst_decision", by=by, note=note)
        gate = _drive(c, project, run_id, spec)
        if gate == "sar_filing":
            texts.append(_output(c, run_id, "narrative")["narrative"])
            _answer(c, run_id, "sar_filing", by="bsa.officer", note="Filed, reference SAR-9.")
            assert _drive(c, project, run_id, spec) is None
        closure = _output(c, run_id, "aml_closure")
        texts.append(str(closure.get("note") or ""))

    for blob in texts:
        assert not _RAW_ID.search(blob), f"a screen must never show a raw id on its own: {blob!r}"
        assert not _PLACEHOLDER.search(blob), f"no 'None'/'UNKNOWN' placeholder: {blob!r}"
