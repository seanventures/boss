"""The plan-coverage review of #155, each probe reproduced (AR-24 review).

  3. A finding anchored at `proxy/router.go` was "missing" from a plan naming
     `internal/proxy/router.go` or `./proxy/router.go`: the look-behind refused both. A file is
     now named by a `/`-bounded path tail, in both directions.
  4. Request changes joined the note AND the sent-back plan to `instructions`: the old plan's
     `[HIGH]` bullets became required brief items, and the writer and reviewer read the rejected
     plan as the task. They now go to the planner only.
  5. After Request changes the re-plan always goes back to the person (the plan gate is kept
     whenever `plan_change_request` is set), whatever the tier or the coverage.
  6. The claim "every build" is narrowed to where the design phase runs.
  9. A delegate never approves a plan card whose plan leaves a security or HIGH item out.
  +  The Request changes note is kept on the run (`plan_change_note`), the way #158 keeps a
     rejection's (`plan_rejected_note`).
"""
from __future__ import annotations

import inspect
import json

import pytest
from sqlalchemy import text

from boss_engine.identity import approval_delegations as ad
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import plan_coverage as pc

from .test_a_plan_covers_its_brief import BRIEF, CRITERIA, ROW, Decider, _paused_build
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name


# ── 3: a path names its file in its longer or shorter form ─────────────────────────────────

@pytest.mark.parametrize("anchored,plan,verdict", [
    ("proxy/router.go", "Change internal/proxy/router.go: `Match` cleans the path.", "covered"),
    ("proxy/router.go", "Edit ./proxy/router.go (`Match`).", "covered"),
    ("internal/proxy/router.go", "Edit proxy/router.go (`Match`).", "covered"),
    ("internal/proxy/router.go", "Edit `router.go`: Match.", "covered"),
    ("proxy/router.go", "Edit otherproxy/router.go (`Match`).", "missing"),
    ("proxy/router.go", "Edit internal/proxy/router.go.bak (`Match`).", "missing"),
    ("proxy/router.go", "Edit internal/proxy/router.go, nothing else.", "missing"),  # no Match
])
def test_a_finding_s_file_is_named_by_any_bounded_form_of_its_path(anchored, plan, verdict):
    item = pc.BriefItem("bi_x", "HIGH", f"HIGH {anchored} (`Match`) — traversal", "finding",
                        [anchored], ["Match"])
    assert pc.anchored_verdict(item, plan) == verdict


# ── 4: the note and the old plan reach the planner, never the brief ────────────────────────

class _Agent:
    def __init__(self):
        self.prompts: list[str] = []

    def run_role(self, prompt, root, read_only=True, model=None, **kw):
        self.prompts.append(prompt)
        return {"summary": "", "cost_usd_micros": 0}


def test_a_sent_back_plan_is_the_planners_reference_not_the_brief(eng, project,  # noqa: F811
                                                                  monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services import typed_decisions

    monkeypatch.setattr(typed_decisions, "make_decider", lambda settings: Decider("covered"))
    old_plan = ("1. Fix the request id.\n- [HIGH] internal/legacy/evil.go (`Nope`) — an item of "
                "the OLD plan, not of the brief\n")
    state = {"plan_change_request": {"note": "Fix the HIGH path traversal in router.go first.",
                                     "previous_plan": old_plan}}
    note = pc.change_request_brief(state)
    # What joining them to the brief did: the old plan's bullet became a required item.
    joined = pc.brief_items(BRIEF + note, CRITERIA)
    assert any("legacy/evil.go" in i.text for i in joined)
    agent = _Agent()
    design, _cost = g._plan_covers_brief(  # noqa: SLF001
        eng, get_settings(), project, "crun_test", {"plan": ROW["plan"], "plan_review": ""},
        title="gateway", instructions=BRIEF, criteria=CRITERIA, root=".",
        route=lambda stage: (agent, None), planner_note=note)
    cov = design["coverage"]
    assert cov["items"] == len(pc.brief_items(BRIEF, CRITERIA))
    assert not any("legacy/evil.go" in m["item"] for m in cov["missing"])
    # The planner was told: the note and the plan it sent back.
    assert agent.prompts and "router.go first" in agent.prompts[0]
    assert "OLD plan" in agent.prompts[0]


def test_the_pipeline_gives_the_note_to_the_design_session_only():
    src = inspect.getsource(g)
    design = src[src.index("        elif do_design:"):src.index("# PERSIST THE PLAN THE MOMENT")]
    assert "_design_brief = f\"{instructions}{_plan_sent_back}\"" in design
    assert "instructions = f\"{instructions}{_plan_sent_back}\"" not in src
    assert "_design_prompt(title, _design_brief, criteria)" in design
    assert "_spec_prompt(title, _design_brief, criteria)" in design
    # The coverage check reads the brief as written; the planner gets the note.
    assert "instructions=instructions, criteria=criteria, root=root, route=_route,\n" \
           "                    planner_note=_plan_sent_back)" in design
    # A sent-back plan is never replaced by an approved PRD's solution.
    assert "design = None if _plan_sent_back else _design_from_approved_prd(" in design


# ── 5: a sent-back plan always goes back to the person ─────────────────────────────────────

def test_a_re_plan_after_request_changes_is_always_asked_again():
    src = inspect.getsource(g)
    gate = src.index("if _requires_plan_approval(risk_tier) or _gaps or _asked_again:")
    assert src.index("_asked_again = bool(prior_state.get(\"plan_change_request\"))") < gate
    # Only the design path reaches it; the approved re-plan resumes without asking a third time.
    assert src.index("        if do_design and resume_design:") < gate


# ── 6: the claim says where the check runs ─────────────────────────────────────────────────

def test_the_claim_is_where_the_design_phase_runs():
    doc = pc.__doc__
    assert "for every build (not only follow-ups)" not in doc
    assert "every build that runs the design" in doc and "acceptance check" in doc


# ── 9: a delegate never approves a plan with a security or HIGH gap ────────────────────────

def _plan_card(missing):
    return {"project_id": "p", "recommendation": {
        "action": "resume_code_task", "run_id": "r", "plan": "…", "sensitive": [],
        "coverage": {"ok": not missing, "items": 3, "missing": missing}}}


@pytest.mark.parametrize("row", [
    {"severity": "HIGH", "item": "HIGH internal/proxy/router.go — traversal"},
    {"severity": "CRITICAL", "item": "CRITICAL sql injection"},
    {"severity": "MEDIUM", "security": True, "item": "MEDIUM applyCORS reflects any origin"},
])
def test_a_plan_with_a_security_or_high_gap_is_the_persons(row):
    code, why = ad.hard_limit(None, _plan_card([row]))
    assert code == "governance_never_delegated" and "security or HIGH" in why


def test_a_plan_with_only_lesser_gaps_or_none_may_be_delegated():
    assert ad.hard_limit(None, _plan_card([])) is None
    assert ad.hard_limit(None, _plan_card(
        [{"severity": "MUST-HAVE", "item": "README quick start"}])) is None


def test_coverage_marks_the_security_block_s_items():
    cov = pc.check(ROW, instructions=BRIEF, criteria=CRITERIA, decider=None)
    cors = next(m for m in cov.missing if "applyCORS" in m["item"])
    assert cors["security"] is True and pc.serious_gaps(cov.as_dict())


# ── the Request changes note is kept on the run, like a rejection's ────────────────────────

def test_the_change_note_is_kept_on_the_run(eng, project):  # noqa: F811
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    try:
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO project_members (id, project_id, member_id, display_name, "
                "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', "
                "'[\"ceo\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
                {"i": f"pm_{project}_ceo", "p": project})
            run_id, dec = _paused_build(c, project)
            DecisionService(get_settings()).transition(
                c, dec, action="revise", actor="ceo",
                note="  Fix the HIGH path traversal\n in router.go first.  ")
        with eng.begin() as c:
            fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                             {"i": run_id}).scalar()
            note = c.execute(text("SELECT resolution_note FROM decisions WHERE id=:d"),
                             {"d": dec}).scalar()
        fctx = fctx if isinstance(fctx, dict) else json.loads(fctx or "{}")
        assert fctx["plan_change_note"] == "Fix the HIGH path traversal in router.go first."
        assert "router.go first" in note
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM code_task_runs WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM project_members WHERE project_id=:p"), {"p": project})
