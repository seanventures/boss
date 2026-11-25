"""The Work panel follows what the work is (operator, 2026-11-20: "why an investigation needs code
task pipeline?").

northpeak-lending's "Investigate DQX-0588 v2 on COB 2026-10-05" (DQ RCA and Remediation, run #36,
Done) was drawn as Spec → Build → Review & tests → Merge → Staging → Production with "Nothing
built yet" and "Pick a prod host"; "Extract the rules of NorthPeak Bank" as five empty
code-pipeline dots. Now a workflow run that changes no app carries:

  shape    investigation | job (code work stays `code`, with no flow of its own)
  flow     its workflow's own steps, condensed to about six nodes, with agents and decisions
  outcome  Asked (the trigger) · Found (root cause + evidence) · Remediation, or Asked · Produced

Fixtures shaped like those rows (the step outputs are trimmed copies of the real ones). Real
Postgres; no model call anywhere.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld

from boss_engine.services.project_experience import work, work_outcome

# The DQ RCA workflow's steps (id, kind, capability, name) and what each recorded.
DQ_STEPS = [
    ("monitoring", "capability", "dq_monitoring", "Sentinel"),
    ("incident_route", "condition", None, "A new incident?"),
    ("attached_stop", "timer", None, "Attached to an open incident — stop"),
    ("evidence_loop", "bounded_loop", None, "Evidence loop"),
    ("context_enrichment", "capability", "dq_context_enrichment", "Context Hydration"),
    ("historical_pattern", "capability", "dq_historical_pattern", "Past Incidents"),
    ("rca", "capability", "dq_rca", "Root Cause Analysis"),
    ("pipeline_orchestrator", "capability", "dq_orchestration", "Flow Orchestrator"),
    ("lanes", "for_each", None, "Swimlanes"),
    ("lane", "capability", "dq_lane", "Swimlane"),
    ("rule_drafting", "capability", "dq_rule_drafting", "Rule Proposal"),
    ("remediation_advisor", "capability", "dq_remediation_package", "Corrective Action"),
    ("left_open", "timer", None, "No confirmed cause — the incident stays open"),
    ("human_approval", "decision", None, "Human approval"),
    ("human_execution", "decision", None, "Human execution"),
    ("remediation_fix", "capability", "dq_remediation_fix", "Fix (design target)"),
    ("incident_closure", "capability", "dq_incident_closure", "Retrospective"),
    ("escalation", "capability", "dq_escalation", "Escalation Watch"),
]
TRIGGER = ("Opened INC-505 for DQX-0588 v2 on COB 2026-10-05 (NPB-DQ-LN-003), severity Medium. "
           "Failure reason: Principal outstanding reconciles to its components failed for 412 of "
           "61220 (rate 0.00673 vs Error above 0.0025)")
CAUSE = ("F-01 extracted principal_outstanding before the LSP 'Update Loan Summary' COB job "
         "finished, because CHG-7731 removed the F-01 wait-for-completion dependency.")
FIX = "Restore the F-01 job dependency so the extract waits for the COB completion flag."
DQ_OUT = {
    "monitoring": {"summary": TRIGGER, "ran_as": {"how": "named", "agent": "dq_monitoring_agent"}},
    "context_enrichment": {"summary": "Context assembled for INC-505: 1 CDE, 3 owners."},
    "historical_pattern": {"summary": "Matched 5 closed incident(s) for INC-505."},
    "rca": {"summary": "Candidate root causes for INC-505 (round 1)", "rca_verdict": "Candidates",
            "rationale": "The exact INC-2187 job-ordering pattern on the same rule.",
            "ran_as": {"how": "named", "agent": "dq_rca_agent"}, "cited_fact_ids": ["a", "b"],
            "candidates": [
                {"cause": "The EDW patch window made F-01 land late.", "family": "feed_etl",
                 "confidence": 0.18, "evidence": ["CHG-7750"], "precedents": ["INC-1988"]},
                {"cause": CAUSE, "family": "source_processing", "confidence": 0.84,
                 "evidence": ["EXCEPTION", "CHG-7731"], "precedents": ["INC-2187"]}]},
    "pipeline_orchestrator": {"summary": "Routed INC-505 to lane_source_processing (0.84)."},
    "lane": {"summary": "lane_source_processing on INC-505: Confirmed — CHG-7731 removed it."},
    "rule_drafting": {"summary": "No swimlane confirmed a false positive: nothing to draft."},
    "remediation_advisor": {"summary": "Prepared the remediation for INC-505.",
                            "package": {"fix": FIX, "status": "prepared"}},
    "remediation_fix": {"summary": "The approved fix for INC-505 was carried out under CHG-7760."},
    "incident_closure": {"summary": "Closed INC-505: the re-run grades Clean.",
                         "ran_as": {"how": "named", "agent": "dq_incident_closure_agent"}},
}
# Seconds each step took (the RCA and the lane are the long ones, as on the real run).
DQ_SECS = {"rca": 40, "lane": 19, "remediation_advisor": 22}
SKIPPED = {"attached_stop", "left_open", "escalation"}

EXTRACT_STEPS = [("extract", "capability", "extract_rulebook_rules", None),
                 ("review", "capability", "rules_review", None)]
EXTRACT_OUT = {
    "extract": {"summary": "Read 136 of 136 sections of “NorthPeak Bank: Loan Data Quality "
                           "Rulebook v3.2” and proposed 96 rule(s) with 190 threshold(s) for "
                           "review.",
                "sections": 136, "sections_read": 136, "rules_proposed": 96,
                "thresholds_proposed": 190, "model_calls": 12, "scan_id": "rbx_ee62447aa442",
                "review_link": "/?area=knowledge&kg_tab=review&kg_scan=rbx_ee62447aa442",
                "ran_as": {"how": "named", "agent": "rulebook_analyst"}},
    "review": {"outcome": "approved", "approved_by": "auto_approval"},
}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("px-kinds-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client):
    slug = f"px-kinds-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "NorthPeak Lending", "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _run(db, pid: str, key: str, name: str, steps: list, outputs: dict, run_input: dict, *,
         secs: dict = (), skipped: set = frozenset(), status: str = "completed",
         decisions: dict = ()) -> str:
    at = datetime.now(timezone.utc) - timedelta(hours=2)
    did, vid, rid = _id("wfd"), _id("wfv"), _id("wfr")
    spec = {"steps": [{"id": s, "kind": k, **({"name": n} if n else {}),
                       **({"binding": {"capability": c}} if c else {})} for s, k, c, n in steps]}
    db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                    "namespace, created_by) VALUES (:d, :p, :k, :n, 'product', 'ceo')"),
               {"d": did, "p": pid, "k": f"{key}_{uuid.uuid4().hex[:6]}", "n": name})
    db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                    "version, status, spec, content_hash, created_by) VALUES (:v, :p, :d, 1, "
                    "'published', CAST(:s AS JSONB), :v, 'ceo')"),
               {"v": vid, "p": pid, "d": did, "s": json.dumps(spec)})
    db.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
        "actor, status, input, created_at, updated_at, completed_at) VALUES (:i, :p, :d, :v, "
        "'ceo', :st, CAST(:in AS JSONB), :t, :u, :u)"),
        {"i": rid, "p": pid, "d": did, "v": vid, "t": at, "u": at + timedelta(minutes=20),
         "st": status, "in": json.dumps(run_input)})
    secs = dict(secs)
    clock = at
    for sid, kind, _cap, _name in steps:
        if kind in ("condition", "for_each", "bounded_loop") and sid not in skipped:
            st, out, began, ended = "succeeded", {}, None, clock
        elif sid in skipped:
            st, out, began, ended = "skipped", {}, None, clock
        else:
            st, out = "succeeded", dict(outputs.get(sid) or {})
            began, ended = (None, clock) if kind == "decision" else (
                clock, clock + timedelta(seconds=secs.get(sid, 0.4)))
        dec = None
        if kind == "decision" and sid in dict(decisions):
            dec = _id("dec")
            db.execute(text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status, "
                "risk_tier, approved_by, approved_at, created_at) VALUES (:i, :p, :q, "
                "CAST('{}' AS JSONB), 'approved', 2, :by, :at, :at)"),
                {"i": dec, "p": pid, "q": f"{sid}?", "by": dict(decisions)[sid], "at": clock})
            out = {"outcome": "approved", "decision_id": dec}
        db.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
            "status, output, decision_id, started_at, completed_at) VALUES (:i, :p, :r, :s, :k, "
            ":st, CAST(:o AS JSONB), :d, :b, :e)"),
            {"i": _id("wsr"), "p": pid, "r": rid, "s": sid, "k": kind, "st": st,
             "o": json.dumps(out), "d": dec, "b": began, "e": ended})
        clock = (ended or clock) + timedelta(seconds=1)
    return rid


def _investigation(db, pid: str) -> str:
    return _run(db, pid, "dq_incident_rca", "DQ RCA and Remediation", DQ_STEPS, DQ_OUT,
                {"title": "Investigate DQX-0588 v2 on COB 2026-10-05",
                 "exception_group_id": "EXG-20261005-0003"},
                secs=DQ_SECS, skipped=SKIPPED,
                decisions={"human_approval": "ceo", "human_execution": "ceo"})


def _extraction(db, pid: str) -> str:
    return _run(db, pid, "rulebook_extraction", "Extract Rules from a Rulebook", EXTRACT_STEPS,
                EXTRACT_OUT, {"title": "Extract the rules of NorthPeak Bank: Loan Data Quality "
                                       "Rulebook v3.2", "document_id": "kgn_f62fe02b06d94fe3995e"},
                secs={"extract": 815})


def _code(db, pid: str) -> str:
    steps = [("meeting", "capability", "meeting", None), ("build", "capability", "build", None),
             ("deploy", "capability", "deploy", None)]
    return _run(db, pid, "minor_feature", "Minor Feature", steps, {},
                {"title": "Add a dark mode", "work_kind": "feature"}, status="running")


def _row(engine, pid: str, rid: str) -> dict:
    with engine.connect() as db:
        return next(w for w in work.everything(db, pid) if w["id"] == rid)


def test_an_investigation_is_drawn_as_its_own_steps(engine, project):
    with engine.begin() as db:
        rid = _investigation(db, project)
    w = _row(engine, project, rid)
    assert w["shape"] == "investigation" and w["kind"] == "incident"
    flow = w["flow"]
    # Twelve steps that did work, condensed to six; no routing, timer or untaken branch.
    assert len(flow) == 6
    names = [n for node in flow for n in node["steps"]]
    assert "A new incident?" not in names and "Escalation Watch" not in names
    assert names[0] == "Sentinel" and names[-1] == "Retrospective"
    labels = [n["label"] for n in flow]
    # A node is named for the step in it that did the most work.
    assert "Root Cause Analysis" in labels and "Corrective Action" in labels
    rca = next(n for n in flow if n["label"] == "Root Cause Analysis")
    assert rca["agent"]["role_key"] == "dq_rca_agent" and rca["duration_s"] == 40
    person = next(n for n in flow if n["you"])
    assert person["steps"] == ["Human approval", "Human execution"]
    assert person["decided"] == "approved" and person["decided_by"]
    assert person["agent"] is None
    assert all(n["state"] == "done" for n in flow)


def test_an_investigation_says_what_it_found_and_what_was_done(engine, project):
    with engine.begin() as db:
        rid = _investigation(db, project)
    oc = _row(engine, project, rid)["outcome"]
    # Asked: the incident, not the workflow's name.
    assert oc["asked"]["text"] == "Investigate DQX-0588 v2 on COB 2026-10-05"
    assert oc["asked"]["trigger"].startswith("Opened INC-505")
    assert oc["asked"]["trigger_step"] == "Sentinel"
    assert {"label": "Exception group id", "value": "EXG-20261005-0003"} in oc["asked"]["facts"]
    # Found: the most confident candidate, with its evidence and precedents.
    f = oc["found"]
    assert f["text"] == CAUSE and f["confidence"] == 0.84 and f["step"] == "Root Cause Analysis"
    assert f["refs"] == ["EXCEPTION", "CHG-7731", "INC-2187"] and f["cited"] == 2
    assert f["others"][0]["confidence"] == 0.18
    assert [e["step"] for e in oc["evidence"]][:2] == ["Context Hydration", "Past Incidents"]
    # Remediation: proposed, approved by a person, done, closed.
    rem = oc["remediation"]
    assert rem[0] == {**rem[0], "step": "Corrective Action", "text": FIX, "state": "proposed"}
    assert [r["state"] for r in rem if r["person"]] == ["approved", "approved"]
    assert rem[-1]["step"] == "Retrospective" and rem[-1]["state"] == "done"
    assert oc["line"] == "Root cause: Source processing (0.84) · remediation approved"
    assert oc["produced"] == [] and oc["counts"] == []


def test_a_job_says_what_it_produced(engine, project):
    with engine.begin() as db:
        rid = _extraction(db, project)
    w = _row(engine, project, rid)
    assert w["shape"] == "job" and w["kind"] == "job"
    # Two steps, named from their ids when the workflow names none.
    assert [(n["label"], n["state"]) for n in w["flow"]] == [("Extract", "done"), ("Review", "done")]
    assert w["flow"][0]["agent"]["role_key"] == "rulebook_analyst"
    oc = w["outcome"]
    assert oc["found"] is None and oc["remediation"] == []
    assert oc["asked"]["facts"] == []          # a record id is never a fact a person reads
    assert oc["counts"] == [{"label": "Sections read", "value": 136},
                            {"label": "Rules proposed", "value": 96},
                            {"label": "Thresholds proposed", "value": 190}]
    assert oc["produced"][0]["text"].startswith("Read 136 of 136 sections")
    assert oc["produced"][1] == {"step": "Review", "text": "Approved by auto approval"}
    assert oc["links"] == [{"label": "Extract: review",
                            "href": "/?area=knowledge&kg_tab=review&kg_scan=rbx_ee62447aa442"}]
    assert oc["line"].startswith("Read 136 of 136 sections")


def test_code_work_keeps_its_stations(engine, project):
    with engine.begin() as db:
        rid = _code(db, project)
    w = _row(engine, project, rid)
    assert w["shape"] == "code" and w["flow"] == [] and w["outcome"] is None
    assert [s["key"] for s in w["stages"]] == ["plan", "design", "build", "review", "pr", "ship"]
    assert w["kind"] == "feature"
    # WORK PANEL V2: the workflow's own steps, each with its stage (Plan · Build · Ship · Live).
    assert [(s["id"], s["capability"], s["stage"], s["state"]) for s in w["steps"]] == [
        ("meeting", "meeting", "plan", "done"), ("build", "build", "build", "done"),
        ("deploy", "deploy", "ship", "done")]
    assert w["input_file"] is None and w["rerun_of"] is None


def test_the_flow_condenses_by_least_work_and_never_mixes_people_with_agents():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    t = now - timedelta(minutes=10)
    spec = {"steps": [{"id": f"s{i}", "kind": "decision" if i in (3, 4) else "capability",
                       "name": f"Step {i}"} for i in range(9)]}
    secs = [1, 30, 2, 0, 0, 3, 50, 1, 1]
    steps = {f"s{i}": {"status": "succeeded", "output": {}, "started_at": t,
                       "completed_at": t + timedelta(seconds=s)} for i, s in enumerate(secs)}
    flow = work_outcome.step_flow(spec, steps, roster={}, now=now, finished=True)
    assert len(flow) == 6
    assert [n["steps"] for n in flow if n["you"]] == [["Step 3", "Step 4"]]
    assert all(not (n["you"] and any(s in n["steps"] for s in ("Step 2", "Step 5")))
               for n in flow)
    assert {"Step 1", "Step 6"} <= {n["label"] for n in flow}


def test_what_changes_an_app_is_code_work():
    build = {"steps": [{"id": "b", "binding": {"capability": "build"}}]}
    probe = {"steps": [{"id": "x", "binding": {"capability": "dq_rca"}}]}
    assert work_outcome.delivers_code(build)
    assert not work_outcome.delivers_code(probe)
    assert work_outcome.delivers_code(probe, has_builds=True)
    assert work_outcome.delivers_code(probe, has_pr=True)
