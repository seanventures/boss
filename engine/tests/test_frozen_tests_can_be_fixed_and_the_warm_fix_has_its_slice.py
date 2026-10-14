"""QA's tests pass typecheck and lint before they are frozen; the warm fix keeps its slice.

Pre-mortem findings, each verified by calling the code directly:

7. The implementer may not edit tests, tsc and eslint cover tests/, and the arbiter only ran in
   warm rounds that are 0 once repair_attempts >= 1: a frozen QA test that failed typecheck or
   lint could be fixed by nobody.
11. The 25-minute implement budget could cut off the same-model warm fix.
12. `code_task.escalation_skipped` logged `model: null` whenever the route left the model to the
    engine's default.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import verification_target as vt

TSC_AND_ESLINT = """
> helpdesk@1.0.0 typecheck
> tsc --noEmit
tests/tickets.test.ts(3,10): error TS2307: Cannot find module '../src/tickets'.
tests/tickets.test.ts(9,5): error TS1005: ';' expected.
src/server.ts(1,1): error TS1005: ';' expected.

/work/helpdesk/tests/tickets.test.ts
   4:7  error  'unused' is assigned a value but never used  no-unused-vars
   5:1  error  Unable to resolve path to module '../src/x'  import/no-unresolved
/work/helpdesk/src/server.ts
   1:1  error  'x' is not defined  no-undef
"""


def test_only_qa_files_errors_that_qa_can_fix_now_are_reported():
    got = vt.qa_static_findings(TSC_AND_ESLINT, ["helpdesk/tests/tickets.test.ts"],
                                app_rel="helpdesk")
    assert got == [
        "tests/tickets.test.ts(9,5): error TS1005: ';' expected.",
        "/work/helpdesk/tests/tickets.test.ts: 4:7  error  'unused' is assigned a value but "
        "never used  no-unused-vars"]
    # The implementation not existing yet is never QA's to fix, and nobody else's file is.
    assert not any("TS2307" in g_ or "no-unresolved" in g_ or "server.ts" in g_ for g_ in got)
    assert vt.qa_static_findings("", ["tests/a.test.ts"]) == []


def test_qa_gets_its_turn_before_the_tests_are_frozen():
    src = inspect.getsource(g._run_governed_code_task)
    check = src.index("_qa_static = _qa_static_check(")
    assert check < src.index("tests_before = _hash_files(root, test_files)")
    turn = src[check:check + 2500]
    assert '_route("write_tests")' in turn and "_qa_static_fix_prompt(_qa_static)" in turn
    assert "resume_session_id=(r1 or {}).get(\"session_id\")" in turn
    # The checks are the plan's own typecheck and lint, never a command BOSS invents.
    assert vt.QA_STATIC_SCRIPTS == ("typecheck", "lint")
    prompt = g._qa_static_fix_prompt(["tests/a.test.ts(1,1): error TS1005"])
    assert "TEST files" in prompt and "frozen" in prompt and "suppressions" in prompt


def test_the_arbiter_runs_before_the_same_model_warm_fix():
    src = inspect.getsource(g._run_governed_code_task)
    loop = src[src.index("for attempt in range(max_repair + 2):"):src.index(
        "# The implement budget ends with the stage")]
    arb = loop.index('_arbitrate_tests(fail_output[-6000:], f"implement-{attempt}")')
    assert loop.index("if (_warm_fix[0] and do_separate_tests") < arb
    # An amended oracle is the new frozen spec, so its author's edit is not a tamper.
    assert "tests_before = _hash_files(root, test_files)" in loop[arb:arb + 600]
    # One arbiter, shared with the warm round.
    assert src.count("_test_arbiter_prompt(") == 1


def test_the_warm_fix_has_a_reserved_slice_of_the_implement_budget():
    reserve = g.WARM_FIX_RESERVE_SECONDS
    assert reserve == 360
    assert g.implement_work_deadline(10_000.0, 1500) == 10_000.0 - reserve
    assert g.implement_work_deadline(10_000.0, 2 * reserve - 1) == 10_000.0   # too small to split
    assert g.implement_work_deadline(None, 0) is None
    src = inspect.getsource(g._run_governed_code_task)
    assert "set_llm_context(stage_deadline=_impl_work_deadline)" in src
    loop = src[src.index("for attempt in range(max_repair + 2):"):]
    # The warm fix runs to the real deadline; a cold attempt past the work slice jumps to it.
    assert "set_llm_context(stage_deadline=_impl_deadline)" in loop
    assert '"code_task.warm_fix_reserved"' in loop
    assert "if ok or attempt == max_repair + 1 or _warm_fix_ran[0]:" in loop


def test_escalation_skipped_names_the_engine_and_the_model_that_ran():
    rec = g.escalation_route_record({"engine": "claude_code", "model": None},
                                    {"model": "claude-sonnet-5"})
    assert rec == {"engine": "claude_code", "model": "claude-sonnet-5"}
    assert g.escalation_route_record({"engine": "codex", "model": "gpt-5"}, {}) == {
        "engine": "codex", "model": "gpt-5"}
    assert g.escalation_route_record({}, {})["model"]          # never null
    src = inspect.getsource(g._run_governed_code_task)
    assert '"model": _before.get("model")' not in src
    assert "**escalation_route_record(_before, r2)," in src
