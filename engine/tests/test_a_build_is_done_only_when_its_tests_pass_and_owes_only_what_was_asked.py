"""A build is done only when its tests pass, and it owes only what its brief asked for.

The Notes Lite speed check (crun_3a39cd19): a three-criterion notes app ran 35 minutes, four
implement rounds (one a "frontier retry" on the same model), never turned its tests green, and
ended completed_no_pr with written_tests_pass and nothing_broke marked advisory. BOSS's own
prompts had added Prometheus metrics, a version endpoint and Playwright that nobody asked for.
"""
from __future__ import annotations

from boss_engine.services import build_scope, governed_code_task_service as g
from boss_engine.services.platform_standards import platform_standards

NOTES = ("Speed check: Notes Lite",
         "Build 'Notes Lite': an Express API in notes-lite/server/ with GET/POST/DELETE "
         "/api/notes plus /healthz, a small static page in notes-lite/web/, Vitest + "
         "supertest tests.",
         ["npm test passes", "API validates input", "the page lists, adds and deletes notes"])


def test_failing_tests_never_ship_even_on_an_advisory_pass():
    for gate in ("written_tests_pass", "nothing_broke"):
        # Rounds spent, PR or not: the build stops, failing.
        assert g.resolve_gate_failure_action(
            failing=[gate], advisory_gate=True, rounds_used=2, max_repair_rounds=2,
            open_pr=True, never_block=True) == "block"
        assert g.resolve_gate_failure_action(
            failing=[gate], advisory_gate=True, rounds_used=0, max_repair_rounds=2,
            open_pr=False, never_block=True) == "block"
    # A reviewer's opinion alone is still advisory.
    assert g.resolve_gate_failure_action(
        failing=["code_review_passed"], advisory_gate=True, rounds_used=2,
        max_repair_rounds=2, open_pr=True) == "ship"


def test_a_small_app_owes_no_ops_endpoints_or_browser_suite():
    scope = build_scope.decide(*NOTES)
    assert scope == {"ops": False, "e2e": False, "e2e_deferred": False, "bundler": False}
    token = build_scope.set_scope(scope)
    try:
        assert "OBSERVABILITY" not in platform_standards(NOTES[1], force=True).upper()
        assert build_scope.web_runners() == ()
        assert "Playwright/e2e spec" not in g._qa_prompt(*NOTES, "npm test", web=True,
                                                         app_root="notes-lite")
    finally:
        build_scope._SCOPE.reset(token)


def test_a_deployed_app_gets_ops_and_its_browser_suite_comes_in_the_next_pass():
    scope = build_scope.decide("HelpDesk AI", "React + Vite + Tailwind; test:e2e with "
                               "Playwright; expose /metrics", [])
    assert scope == {"ops": True, "e2e": False, "e2e_deferred": True, "bundler": True}
    token = build_scope.set_scope(scope)
    try:
        assert build_scope.web_runners() == ("vite",)
        for prompt in (g._qa_prompt("HelpDesk AI", "Playwright e2e", ["c"], "npm test", web=True,
                                    app_root="helpdesk"),
                       g._impl_prompt("HelpDesk AI", "Playwright e2e", ["t.test.js"], "npm test",
                                      criteria=["c"]),
                       g._solo_prompt("HelpDesk AI", "Playwright e2e", ["c"], "npm test", web=True)):
            assert "BROWSER TESTS COME LATER" in prompt
    finally:
        build_scope._SCOPE.reset(token)
    assert build_scope.decide(*NOTES, workflow_deploys=True)["ops"] is True
    # Outside a build, nothing changes.
    assert build_scope.current() is None
    assert build_scope.web_runners() == ("vite", "@playwright/test")


def test_qa_tests_are_sized_to_the_criteria_and_planned_first():
    clause = g._test_scope_clause(NOTES[2])
    assert "At most 12 test cases" in clause and "TEST PLAN:" in clause
    for prompt in (g._qa_prompt(*NOTES, "npm test"),
                   g._qa_prompt(*NOTES, "npm test", web=True, app_root="notes-lite")):
        assert "TEST SCOPE (mandatory" in prompt
    note = g._test_plan_note("Wrote 6 tests.\nTEST PLAN:\ntests/api.test.js::creates -> c1")
    assert "tests/api.test.js::creates -> c1" in note
    assert g._test_plan_note("no plan here") == ""


def test_one_fix_round_before_review_and_failing_tests_skip_the_panel():
    import inspect

    src = inspect.getsource(g)
    # The one fix round is warm and comes right after implement; the "frontier" retry on the
    # same model is gone; a build still failing after it is not reviewed or looped.
    assert '"instead": "warm_fix_round"' in src
    assert '"code_task.review_skipped_tests_failing"' in src
    assert "rounds = min(rounds, 0 if repair_attempts >= 1 else 1)" in src
    assert '"code_task.light_plan"' in src


def test_a_test_command_that_names_its_directory_runs_from_the_root(tmp_path):
    """`npm --prefix notes-lite test` ran inside notes-lite/, so npm looked for
    notes-lite/notes-lite; the agent symlinked `.` there and the sandbox refused it (crun_a936b22f)."""
    from boss_engine.services import verification_target as vt

    (tmp_path / "notes-lite").mkdir()
    (tmp_path / "notes-lite" / "package.json").write_text('{"name": "notes-lite"}')
    plan = vt.resolve_gate_plan({"test_commands": ["npm --prefix notes-lite test"]}, "t",
                                tmp_path, changed=["notes-lite/server/app.js"])
    assert plan.cwd == tmp_path
    assert vt.named_directory("npm --prefix notes-lite test") == "notes-lite"
    assert vt.named_directory("pnpm -C web test") == "web"
    assert vt.named_directory("npm test") is None


def test_a_nul_in_file_text_never_crashes_the_final_write():
    import json

    out = g.pg_json({"files": {"metrics.js": "const key = route + '\x00' + status"}})
    assert "\\u0000" not in out
    assert json.loads(out)["files"]["metrics.js"].endswith("+ '' + status")
