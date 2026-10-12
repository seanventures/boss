"""Standards, spec and plan come from ONE read of the repository; the plan review stays its own.

A HelpDesk AI build ran three cold design sessions (standards 0.6, spec 2.2, plan 5.3 minutes),
each re-reading the same repository. One session now writes all three marked sections. A part
it did not deliver is written by its own session as before, so a short answer never becomes a
thin design. The plan review keeps fresh context: a plan is never reviewed by its author.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g

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
"""


def test_the_three_sections_are_read_back():
    parts = g._split_design(JOINT)
    assert parts["standards"].startswith("- kebab-case files")
    assert "Acceptance: a customer sees only their tickets." in parts["spec"]
    assert parts["plan"].endswith("database: postgres")
    assert "=== PLAN ===" not in parts["spec"]


def test_a_missing_section_is_empty_so_its_own_session_writes_it():
    parts = g._split_design("=== SPEC ===\nOnly a spec.")
    assert parts["spec"] == "Only a spec."
    assert parts["standards"] == "" and parts["plan"] == ""
    assert g._split_design("") == {"standards": "", "spec": "", "plan": ""}


def test_the_joint_prompt_asks_for_every_part_the_old_sessions_did():
    prompt = g._design_prompt("HelpDesk AI", "Build a helpdesk.", ["customers see their own"])
    for head in ("=== STANDARDS ===", "=== SPEC ===", "=== PLAN ==="):
        assert head in prompt
    assert "database: postgres" in prompt and "Do NOT edit any files" in prompt
    assert "acceptance criteria" in prompt


def test_the_pipeline_runs_one_design_session_and_a_separate_plan_review():
    src = inspect.getsource(g._run_governed_code_task)
    design = src[src.index('set_llm_context(stage="design")'):src.index("plan_ok = ")]
    # One joint session first; the per-part sessions only when its part is missing.
    assert design.index("_design_prompt(") < design.index("_standards_prompt()")
    assert 'if not parts["standards"]' in design and 'if not parts["plan"]' in design
    # The review is its own run_role on the plan, with fresh context.
    assert '_review_plan_prompt(title, d3.get("summary", ""))' in design
