"""An approved PRD is the build's design: a short mapping pass, not a second design session.

THE CASE. HelpDesk AI's SLA-timers build (crun_c8e2a5f8ed5246308016, workflow
wfr_c8dbccd76a92488c99dc) spent 572 s in its design phase re-planning a PRD whose "7. Proposed
Solution" the owner had just approved (dec_13a8cd75a6404e539289). Now the approved PRD's solution
is the plan, and one turn-bounded read maps it onto the repository. Anything doubtful — no
solution section, an incomplete answer, the repository contradicting the PRD, no approved PRD at
all — runs the full design as before, and the event says which path ran.
"""
from __future__ import annotations

import inspect
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import approved_prd_design as apd
from boss_engine.services import build_services
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import parallel_build

SOLUTION_BODY = (
    "### 7.1 Architecture / data flow\n"
    "One shared helper `get_sla_deadline(priority, created_at)` and `is_breached(...)` in the "
    "ticket service; the list, detail, Breached filter and dashboard count all call it. The "
    "server returns remaining_seconds and breached; the client only ticks a display timer.\n\n"
    "### 7.2 Ticket list\nA countdown chip per row: green, amber under 25% left, red breached.\n")
PRD = (
    "# PRD: SLA Timers on Tickets\n\n## 1. Summary\nCompute an SLA deadline per priority.\n\n"
    "## 5. Functional Requirements\n| F1 | one shared helper |\n\n"
    f"## 7. Proposed Solution\n{SOLUTION_BODY}\n"
    "## 8. Risks & Mitigations\nClock skew: the server is the clock.\n\n"
    "## 11. Acceptance Criteria\n- urgent breaches after 1h\n")
PRD_WITHOUT_SOLUTION = PRD.replace(f"## 7. Proposed Solution\n{SOLUTION_BODY}\n", "")

PACKAGES = json.dumps({"packages": [
    {"name": "scaffold", "owns": ["server/sla.py"], "tests": [], "brief": "the helper"},
    {"name": "api", "owns": ["server/**"], "tests": ["tests/server/**"], "brief": "filter"},
    {"name": "ui", "owns": ["web/**"], "tests": ["tests/web/**"], "brief": "chip"}]})
ANSWER_FITS = (
    "=== STANDARDS ===\n- snake_case modules\n- pytest under tests/\n"
    "=== PLAN ===\n- server/sla.py: get_sla_deadline(priority, created_at)\n"
    "- server/tickets.py: breached filter\ndatabase: postgres\n"
    "=== CONTRADICTIONS ===\nNONE\n"
    f"=== WORK PACKAGES ===\n```json\n{PACKAGES}\n```\n")
ANSWER_CONTRADICTS = ANSWER_FITS.replace(
    "=== CONTRADICTIONS ===\nNONE", "=== CONTRADICTIONS ===\n- there is no ticket service; "
    "tickets live in a Rails app")


class FakeAgent:
    def __init__(self, summary: str):
        self.summary, self.calls = summary, []

    def run_role(self, prompt, cwd, *, read_only=False, model=None, max_turns=None, **_kw):
        self.calls.append({"prompt": prompt, "read_only": read_only, "max_turns": max_turns})
        return {"summary": self.summary, "cost_usd_micros": 1234}


@pytest.fixture()
def db():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _decision(c, pid, markdown, status="approved"):
    did = f"dec_{uuid4().hex[:12]}"
    c.execute(text("INSERT INTO decisions (id, project_id, question, status, recommendation) "
                   "VALUES (:d, :p, 'Approve the PRD?', :s, CAST(:r AS JSONB))"),
              {"d": did, "p": pid, "s": status, "r": json.dumps(
                  {"action": "execute_agent_tool",
                   "args": {"title": "PRD: SLA Timers", "markdown": markdown}})})
    return did


def _workflow_with_approved_meeting(c, pid, decision_id) -> str:
    """The real shape: a meeting step that awaited the spec_decision and succeeded, and the
    build step whose run the code task's origin names."""
    wid, vid, wfr = (f"wd_{uuid4().hex[:8]}", f"wv_{uuid4().hex[:8]}", f"wfr_{uuid4().hex[:8]}")
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                   "created_by) VALUES (:i, :p, 'spd', 'Standard Product Delivery', 'test')"),
              {"i": wid, "p": pid})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                   "version, spec, content_hash, created_by) VALUES (:i, :p, :w, 1, '{}'::jsonb, "
                   "'h', 'test')"), {"i": vid, "p": pid, "w": wid})
    c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                   "workflow_version_id, actor, status) VALUES (:i, :p, :w, :v, 'ceo', "
                   "'running')"), {"i": wfr, "p": pid, "w": wid, "v": vid})
    build_step = f"wfs_{uuid4().hex[:10]}"
    for sid, step, out in (
            (f"wfs_{uuid4().hex[:10]}", "meeting",
             {"tool": "request_meeting",
              "await": {"id": decision_id, "kind": "spec_decision"}}),
            (build_step, "build", {"tool": "create_code_task"})):
        c.execute(text("INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, "
                       "step_id, step_kind, status, output) VALUES (:i, :p, :r, :s, "
                       "'capability', 'succeeded', CAST(:o AS JSONB))"),
                  {"i": sid, "p": pid, "r": wfr, "s": step, "o": json.dumps(out)})
    return build_step


def _events(eng, pid, run_id):
    with eng.begin() as c:
        return {r[0]: r[1] for r in c.execute(text(
            "SELECT event_type, data FROM events WHERE project_id = :p AND entity_id = :r"),
            {"p": pid, "r": run_id})}


def _design(eng, pid, fctx, agent, run_id):
    return g._design_from_approved_prd(
        eng, pid, run_id, fctx, title="SLA timers", instructions="Add SLA timers.",
        criteria=["urgent breaches after 1h"], root="/tmp", route=lambda _s: (agent, "m"))


# ── The solution section ────────────────────────────────────────────────────────────────────
def test_the_solution_section_is_read_with_its_subsections_and_stops_at_the_next_section():
    section = apd.solution_section(PRD)
    assert section.startswith("## 7. Proposed Solution")
    assert "### 7.2 Ticket list" in section and "get_sla_deadline" in section
    assert "Risks & Mitigations" not in section


def test_a_prd_without_a_solution_section_has_none():
    assert apd.solution_section(PRD_WITHOUT_SOLUTION) == ""
    assert apd.solution_section("## 7. Proposed Solution\nTBD.\n## 8. Risks\n") == ""


# ── Finding the approved PRD ────────────────────────────────────────────────────────────────
def test_a_workflow_build_finds_the_prd_its_meeting_step_had_approved(db):
    eng, pid = db
    with eng.begin() as c:
        did = _decision(c, pid, PRD)
        step = _workflow_with_approved_meeting(c, pid, did)
        found = apd.find_approved_prd(c, pid, {"origin": {"step_run_id": step}})
    assert found == {"decision_id": did, "markdown": PRD.strip()}


def test_an_approve_to_build_origin_finds_its_decision_and_an_unapproved_one_is_ignored(db):
    eng, pid = db
    with eng.begin() as c:
        approved = _decision(c, pid, PRD)
        pending = _decision(c, pid, PRD, status="pending")
        assert apd.find_approved_prd(c, pid, {"origin": {"kind": "meeting",
                                                         "decision_id": approved}})
        assert apd.find_approved_prd(c, pid, {"origin": {"decision_id": pending}}) is None
        assert apd.find_approved_prd(c, pid, {}) is None


# ── Which path runs ─────────────────────────────────────────────────────────────────────────
def test_a_prd_with_a_solution_takes_the_short_path(db):
    eng, pid = db
    with eng.begin() as c:
        did = _decision(c, pid, PRD)
    agent, run_id = FakeAgent(ANSWER_FITS), f"crun_{uuid4().hex[:10]}"
    design = _design(eng, pid, {"origin": {"decision_id": did}}, agent, run_id)
    assert design is not None and design["source"] == "approved_prd"
    # One bounded, read-only session — never the unbounded joint design + plan review.
    assert len(agent.calls) == 1
    assert agent.calls[0]["read_only"] and agent.calls[0]["max_turns"] == apd.PRD_DESIGN_MAX_TURNS
    assert "THE APPROVED SOLUTION:" in agent.calls[0]["prompt"]
    assert "get_sla_deadline" in agent.calls[0]["prompt"]
    ev = _events(eng, pid, run_id)
    assert ev["code_task.design_from_prd"]["path"] == "approved_prd"
    assert ev["code_task.design_from_prd"]["decision_id"] == did


def test_a_prd_without_a_solution_runs_the_full_design(db):
    eng, pid = db
    with eng.begin() as c:
        did = _decision(c, pid, PRD_WITHOUT_SOLUTION)
    agent, run_id = FakeAgent(ANSWER_FITS), f"crun_{uuid4().hex[:10]}"
    assert _design(eng, pid, {"origin": {"decision_id": did}}, agent, run_id) is None
    assert agent.calls == []
    ev = _events(eng, pid, run_id)["code_task.design_from_prd_declined"]
    assert ev["path"] == "full" and "no solution" in ev["why"]


def test_a_build_with_no_prd_runs_the_full_design_and_records_nothing(db):
    eng, pid = db
    agent, run_id = FakeAgent(ANSWER_FITS), f"crun_{uuid4().hex[:10]}"
    assert _design(eng, pid, {"origin": {"actor": "usr_x"}}, agent, run_id) is None
    assert agent.calls == [] and _events(eng, pid, run_id) == {}


def test_a_repository_that_contradicts_the_prd_runs_the_full_design(db):
    eng, pid = db
    with eng.begin() as c:
        did = _decision(c, pid, PRD)
    agent, run_id = FakeAgent(ANSWER_CONTRADICTS), f"crun_{uuid4().hex[:10]}"
    assert _design(eng, pid, {"origin": {"decision_id": did}}, agent, run_id) is None
    why = _events(eng, pid, run_id)["code_task.design_from_prd_declined"]["why"]
    assert "contradicts" in why and "Rails" in why


def test_an_answer_cut_short_runs_the_full_design():
    prd = {"decision_id": "dec_x", "markdown": PRD}
    # Cut off mid-plan: no closing `database:` line, no verdict.
    cut = ANSWER_FITS.split("database: postgres")[0]
    design, why = apd.design_from_answer(prd, apd.solution_section(PRD), cut)
    assert design is None and "no contradictions verdict" in why


# crun_719a19f0b9c74bae8bfd: the short pass mapped the solution but left out the CONTRADICTIONS
# section; declining it cost a 7.3 min full design.
ANSWER_NO_VERDICT = ANSWER_FITS.replace("=== CONTRADICTIONS ===\nNONE\n", "")


def test_a_complete_mapping_plan_without_a_verdict_is_the_design():
    assert "CONTRADICTIONS" not in ANSWER_NO_VERDICT
    prd = {"decision_id": "dec_719", "markdown": PRD}
    design, why = apd.design_from_answer(prd, apd.solution_section(PRD), ANSWER_NO_VERDICT)
    assert why == "" and design is not None and design["plan_ok"]
    assert "server/sla.py" in design["plan"] and design["source"] == "approved_prd"
    assert [p.name for p in parallel_build.parse_packages(design["packages"])] == \
        ["scaffold", "api", "ui"]


def test_an_explicit_contradiction_still_declines_whatever_the_plan():
    prd = {"decision_id": "dec_x", "markdown": PRD}
    design, why = apd.design_from_answer(prd, apd.solution_section(PRD), ANSWER_CONTRADICTS)
    assert design is None and "contradicts the PRD" in why and "Rails" in why


@pytest.mark.parametrize("plan", [
    "",                                                      # no plan at all
    "- server/sla.py: get_sla_deadline(priority, created_at)\n",  # cut off: no database line
    "- add the helper and the filter as the PRD says\ndatabase: none\n",  # names no file
])
def test_no_verdict_and_a_plan_that_does_not_map_still_declines(plan):
    answer = (f"=== STANDARDS ===\n- pytest\n=== PLAN ===\n{plan}"
              f"=== WORK PACKAGES ===\n```json\n{PACKAGES}\n```\n")
    prd = {"decision_id": "dec_x", "markdown": PRD}
    design, why = apd.design_from_answer(prd, apd.solution_section(PRD), answer)
    assert design is None and why == "the short pass gave no contradictions verdict"


# ── Downstream stays fed ────────────────────────────────────────────────────────────────────
def test_every_field_downstream_reads_is_there():
    prd = {"decision_id": "dec_13a8", "markdown": PRD}
    design, _ = apd.design_from_answer(prd, apd.solution_section(PRD), ANSWER_FITS)
    # The keys the full design writes (state.design, the evidence, resume and the PR receipt).
    assert {"standards", "spec", "plan", "plan_review", "plan_ok", "packages"} <= set(design)
    assert design["standards"].startswith("- snake_case modules")
    # The consistency review's AGREED SPEC is the approved PRD.
    assert design["spec"] == PRD
    assert "AGREED SPEC:" in g._consistency_prompt("t", [], design) and "F1" in \
        g._consistency_prompt("t", [], design)
    # The plan is the PRD's solution plus where it lands in this repo.
    assert "get_sla_deadline" in design["plan"] and "server/tickets.py" in design["plan"]
    # The stream split reads the partition (write_tests / implement in parallel).
    assert [p.name for p in parallel_build.parse_packages(design["packages"])] == \
        ["scaffold", "api", "ui"]
    # Not mistaken for a mis-routed task; the plan-approval gate reads plan + plan_review.
    assert design["plan_ok"] and not g._is_not_a_code_task(design["plan_review"])
    # The database decision still sees the plan's `database:` line.
    assert build_services.detect({}, "", None, design) == ["the plan says database: postgres"]


def test_the_brief_gets_the_plan_without_pasting_the_prd_twice():
    design, _ = apd.design_from_answer({"decision_id": "d", "markdown": PRD},
                                       apd.solution_section(PRD), ANSWER_FITS)
    already = f"Add SLA timers.\n\n---\n\nTHE PRODUCT SPEC BELOW IS THE BRIEF.\n\n{PRD}"
    added = apd.brief_addendum(already, design)
    assert "REFINED SPEC:\nthe approved PRD above" in added and "TECHNICAL PLAN:" in added
    assert PRD not in added
    # A brief that does not carry the PRD gets it as the spec.
    assert PRD in apd.brief_addendum("Add SLA timers.", design)


def test_the_pipeline_tries_the_prd_before_the_full_design_and_falls_back_to_it():
    src = inspect.getsource(g._run_governed_code_task)
    branch = src[src.index("elif do_design:"):src.index("plan_ok = ")]
    assert branch.index("_design_from_approved_prd(") < branch.index("if design is None:") \
        < branch.index("_design_prompt(")
    assert 'fctx["design_path"] = "approved_prd"' in branch
    assert 'fctx["design_path"] = "full"' in branch
    # The PRD-derived design is persisted and gated exactly like the full one.
    after = src[src.index("if design is None:"):src.index("r1 = {\"cost_usd_micros\": 0")]
    assert "code_task.design_persisted" in after and "_requires_plan_approval" in after
