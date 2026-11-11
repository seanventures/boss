"""Standards, spec, plan AND the plan's self-check come from ONE read of the repository.

A HelpDesk AI build ran three cold design sessions (standards 0.6, spec 2.2, plan 5.3 minutes),
each re-reading the same repository. One session now writes all the marked sections. A part it
did not deliver is written by its own session as before, so a short answer never becomes a thin
design. The plan review is a SELF-CHECK section of the session that wrote the plan
(BOSSAPP-1003, audit 997 §7.6): review_plan was its own fresh session that re-read the repo to
judge a plan it was handed inline — 88 calls, $42.80/month, nine times the plan stage itself —
under the same contract (the ROUTING: NOT_A_CODE_TASK check and a final PLAN: OK/REVISE
marker). A session that skips the section still gets the separate review session, so the check
never silently disappears; the human approve-plan gate is untouched.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g
from boss_engine.services.review_verdict import plan_verdict

JOINT = """Some preamble the model wrote.
=== STANDARDS ===
- kebab-case files
- errors are typed
=== SPEC ===
Summary: tickets.
Acceptance: a customer sees only their tickets.
=== PLAN ===
- server/tickets.ts: listTickets(user)
database: postgres
=== PLAN REVIEW ===
The plan covers every criterion; the DST edge case is named.
PLAN: OK
"""


def test_the_sections_are_read_back():
    parts = g._split_design(JOINT)
    assert parts["standards"].startswith("- kebab-case files")
    assert "Acceptance: a customer sees only their tickets." in parts["spec"]
    assert parts["plan"].endswith("database: postgres")
    assert "=== PLAN ===" not in parts["spec"]
    assert parts["plan_review"].endswith("PLAN: OK")
    assert "PLAN REVIEW" not in parts["plan"]


def test_the_self_check_parses_with_the_same_verdict_grammar():
    parts = g._split_design(JOINT)
    assert plan_verdict(parts["plan_review"]) == "ok"
    assert plan_verdict(parts["plan_review"].replace("PLAN: OK", "PLAN: REVISE")) == "revise"
    # The routing check lives in the same section and still trips the same parse.
    assert g._is_not_a_code_task("ROUTING: NOT_A_CODE_TASK — a PRD, not code")


def test_a_missing_section_is_empty_so_its_own_session_writes_it():
    parts = g._split_design("=== SPEC ===\nOnly a spec.")
    assert parts["spec"] == "Only a spec."
    assert parts["standards"] == "" and parts["plan"] == "" and parts["plan_review"] == ""
    # WORK PACKAGES (parallel_build) is the last section; empty means "do not split".
    assert g._split_design("") == {"standards": "", "spec": "", "plan": "",
                                   "plan_review": "", "packages": ""}


def test_the_joint_prompt_asks_for_every_part_the_old_sessions_did():
    prompt = g._design_prompt("HelpDesk AI", "Build a helpdesk.", ["customers see their own"])
    for head in ("=== STANDARDS ===", "=== SPEC ===", "=== PLAN ===", "=== PLAN REVIEW ==="):
        assert head in prompt
    assert "database: postgres" in prompt and "Do NOT edit any files" in prompt
    assert "acceptance criteria" in prompt
    # The self-check keeps the review's whole contract: routing and the closing marker.
    assert "ROUTING: NOT_A_CODE_TASK" in prompt
    assert "PLAN: OK" in prompt and "PLAN: REVISE" in prompt


def test_the_separate_plan_session_self_checks_too():
    prompt = g._plan_prompt("HelpDesk AI", "Spec: tickets.")
    assert "=== PLAN ===" in prompt and "=== PLAN REVIEW ===" in prompt
    assert "ROUTING: NOT_A_CODE_TASK" in prompt and "PLAN: REVISE" in prompt


def test_the_pipeline_runs_one_design_session_and_falls_back_per_part():
    src = inspect.getsource(g._run_governed_code_task)
    design = src[src.index('set_llm_context(stage="design")'):src.index("plan_ok = ")]
    # One joint session first; the per-part sessions only when its part is missing.
    assert design.index("_design_prompt(") < design.index("_standards_prompt()")
    assert 'if not parts["standards"]' in design and 'if not parts["plan"]' in design
    # The self-check comes from the session that wrote the plan …
    assert 'd4 = {"summary": parts["plan_review"]' in design
    assert 'plan_parts["plan_review"]' in design
    # … and the separate review session remains ONLY as the fallback for a skipped section.
    fallback = design[design.index('if not str(d4.get("summary") or "").strip()'):]
    assert '_review_plan_prompt(title, d3.get("summary", ""))' in fallback
