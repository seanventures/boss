"""A build writes a small test suite, sized to the criteria and capped by a budget BOSS enforces.

A HelpDesk-size brief's suite grew with the brief: 76 KB of tests, 27.6 minutes of write_tests,
then 81 minutes of implement making every case pass. The cap is now 1-3 per acceptance
criterion plus 3 edge cases, never past the project's build_test_budget (default 20), however
long the brief. The prompt states it; BOSS then COUNTS the cases itself, gives an over-budget
suite one trim turn on the test author's warm session, and records what remains over on the
build (state.test_budget, code_task.test_budget), never passing it silently.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import project_settings as ps
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context


def test_the_cap_follows_the_criteria_but_never_passes_the_budget():
    assert g.test_budget(["a", "b"]) == 9
    assert g.test_budget([f"c{i}" for i in range(10)]) == g.TEST_BUDGET_DEFAULT == 20
    assert g.test_budget([f"c{i}" for i in range(40)], 25) == 25
    knob = ps.KNOBS["build_test_budget"]
    assert knob["kind"] == "int" and knob["min"] >= 1


def test_the_prompt_states_the_projects_budget():
    try:
        set_llm_context(test_budget=12)
        clause = g._test_scope_clause([f"c{i}" for i in range(10)])
    finally:
        clear_llm_context()
    assert "At most 12 test cases" in clause and "BOSS counts the cases" in clause
    assert "One per acceptance criterion" in clause and "START your final reply with `TEST PLAN:`" in clause
    assert "run twice in a row against the same database" in clause


def test_boss_counts_cases_in_every_language(tmp_path):
    (tmp_path / "a.test.ts").write_text(
        "describe('x', () => {\n  it('one', () => {});\n  test(\"two\", async () => {});\n"
        "  it.skip('three', () => {});\n  // it('not code'\n});\n")
    (tmp_path / "test_b.py").write_text("def test_one():\n    pass\n\nasync def test_two():\n    pass\n"
                                       "def helper():\n    pass\n")
    (tmp_path / "CTest.java").write_text("@Test\nvoid a() {}\n@Test\nvoid b() {}\n")
    (tmp_path / "x_test.go").write_text("func TestA(t *testing.T) {}\n")
    assert g.count_test_cases(tmp_path, ["a.test.ts"]) == 3
    assert g.count_test_cases(tmp_path, ["test_b.py"]) == 2
    assert g.count_test_cases(tmp_path, ["CTest.java", "x_test.go", "missing.py"]) == 3


def test_an_over_budget_suite_is_trimmed_once_and_recorded():
    src = inspect.getsource(g)
    assert "count_test_cases(" in src and "_test_budget_trim_prompt(_written, _cap)" in src
    assert "resume_session_id=r1[\"session_id\"]" in src
    assert "'{test_budget}'" in src and "code_task.test_budget" in src
    assert "set_llm_context(test_budget=" in inspect.getsource(g._record_effort)
    trim = g._test_budget_trim_prompt(31, 20)
    assert "31 test cases" in trim and "at most 20" in trim and "do not add cases" in trim
