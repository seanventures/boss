"""BOSS's own words never reach a newcomer; a handed-on attempt never reads as failed.

The e2e recipe-box walk (2026-11-18) found, on the build page, the merge card and the work list:
"· retry 2" in titles, the raw repair prompt as a headline ("Autonomous review-repair: the quality
gate blocked on code_review_passed, acceptance_met…"), "not convened: profile 'lean' does not
include this seat", gate keys in brackets, "Review passed ✗" beside "Changes asked", and "Tests
9/9 passed" while the must-have check said the suite fails to load. Pinned here, without a DB:
the one mapping (plain_words), its web twin, the test summary and the one state of a handed-on
attempt.
"""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.services import plain_words as pw
from boss_engine.services.build_detail import verify_counts
from boss_engine.services.continuation import handed_on
from boss_engine.services.factory_view import GATE_LABELS, build_state
from boss_engine.services.project_experience.overview import POLISHING_WORDS, polishing, tiles

REPAIR_NOTE = ("Autonomous review-repair: the quality gate blocked on code_review_passed, "
               "acceptance_met. Address EVERY review finding below so ALL gates pass, and do NOT "
               "regress the checks that already passed. If 'tests_strong_enough' failed, "
               "STRENGTHEN the tests so a one-line mutation to the logic is caught.")
SKIP = "not convened: profile 'lean' does not include this seat"
WEB = Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "plain-words.ts"


def test_the_one_mapping_says_each_internal_phrase_plainly():
    assert pw.plain_title("Recipe box: Fix the open review item · retry 2") == \
        "Recipe box: Fix the open review item"
    assert pw.plain_note(REPAIR_NOTE) == ("BOSS is fixing what its checks found: Review asked for "
                                          "changes, Must-haves not all met")
    assert pw.plain_note("Use the BOSS client id from settings") == \
        "Use the BOSS client id from settings"            # a person's note is theirs
    assert pw.plain_skip(SKIP) == "Not part of the Lean quality level"
    # A failing sentence names the failure, never the pass label beside a cross.
    said = pw.plain_sentence("Blocked: this attempt failed its checks (code_review_passed, "
                             "acceptance_met).")
    assert "Review asked for changes" in said and "Review passed" not in said
    assert pw.plain_sentence("Auto review-repair round 2 (gates: ['code_review_passed']); "
                             "iterating autonomously in crun_x.") == \
        "BOSS is fixing what its checks found: Review asked for changes"
    for text in (pw.plain_title("X · retry 3"), pw.plain_note(REPAIR_NOTE), pw.plain_skip(SKIP),
                 said):
        assert pw.leaks(text) == [], text


def test_leaks_finds_every_internal_phrase_and_not_plain_english():
    assert set(pw.leaks(REPAIR_NOTE)) >= {"repair prompt", "code_review_passed", "acceptance_met"}
    assert pw.leaks("Recipe box · retry 2") == ["retry N"]
    assert pw.leaks(SKIP) == ["not convened"]
    assert pw.leaks("Review passed (code_review_passed)") == ["code_review_passed"]
    assert pw.leaks("The change is consistent with the code") == []
    assert pw.leaks("Retry the build") == []


def test_every_gate_has_failed_words_and_the_web_tables_are_the_same():
    assert set(pw.FAILED_WORDS) == set(GATE_LABELS)
    src = WEB.read_text()

    def table(name: str) -> dict[str, str]:
        body = re.search(rf"export const {name}: Record<string, string> = \{{(.*?)\n\}};", src,
                         re.S).group(1)
        return dict(re.findall(r'^\s*(\w+):\s*"([^"]*)",?\s*$', body, re.M))

    assert table("GATE_LABELS") == {k: v[0] for k, v in GATE_LABELS.items()}
    assert table("FAILED_WORDS") == pw.FAILED_WORDS


def test_a_suite_that_did_not_load_never_reads_passed():
    jest = ("FAIL tests/a.test.js\n  ● Test suite failed to run\n\n"
            "Test Suites: 1 failed, 2 passed, 3 total\nTests:       9 passed, 9 total\n")
    vitest = (" FAIL  tests/a.test.ts [ tests/a.test.ts ]\nError: Failed to load url ./missing\n"
              " Test Files  1 failed | 2 passed (3)\n      Tests  9 passed (9)\n")
    node = ("not ok 1 - tests/recipes.test.js\n  error: Cannot find module 'jsdom'\n"
            "# tests 1\n# pass 0\n# fail 1\n")
    # The very run that added this guard: jsdom missing, every file "passed".
    vitest_env = (" Test Files  4 passed (4)\n      Tests  28 passed (28)\n     Errors  1 error\n"
                  "Error: Cannot find package 'jsdom' imported from vitest\n")
    for output in (jest, vitest, node, vitest_env):
        assert verify_counts(output)["load_error"], output
    assert "jsdom" in verify_counts(node)["load_error"]
    # ORDINARY FAILURES ARE NOT LOAD FAILURES (review #224): the summary lines every failing
    # jest / vitest run prints, and a pytest assertion whose traceback names a missing module.
    plain_jest = ("Test Suites: 1 failed, 3 passed, 4 total\n"
                  "Tests:       1 failed, 11 passed, 12 total\n")
    plain_vitest = " Test Files  1 failed | 3 passed (4)\n      Tests  1 failed | 11 passed (12)\n"
    plain_pytest = ("FAILED tests/test_a.py::test_x - ModuleNotFoundError: No module named 'foo'\n"
                    "========== 1 failed, 4 passed in 0.3s ==========\n")
    for output in (plain_jest, plain_vitest, plain_pytest):
        got = verify_counts(output)
        assert got["load_error"] is None and got["failed"] == 1, output
    collecting = ("ERROR collecting tests/test_a.py\nModuleNotFoundError: No module named 'foo'\n"
                  "1 error in 0.2s\n")
    assert "foo" in verify_counts(collecting)["load_error"]
    # A clean run, and an install log that merely mentions a module, load fine.
    assert verify_counts("# tests 9\n# pass 9\n# fail 0\n")["load_error"] is None
    assert verify_counts("npm WARN Cannot find module 'fsevents'\n# tests 9\n# pass 9\n"
                         "# fail 0\n")["load_error"] is None


def test_a_handed_on_attempt_is_continued_not_blocked_or_stalled():
    ctx = {"review_repair_next_run": "crun_next", "failing_gates": ["code_review_passed"]}
    for status, phase in (("blocked", "done"), ("repairing", "done")):
        run = {"id": "crun_a", "status": status, "phase": phase, "finalize_ctx": ctx,
               "error": "Blocked: this attempt failed its checks; the repair continued in "
                        "crun_next. Open that attempt for its outcome."}
        assert handed_on(run)
        st = build_state(run)
        assert st["word"] == "Continued" and "press Retry" not in (st.get("tip") or "")
    # Still reviewing in its own round: not handed on.
    assert not handed_on({"status": "repairing", "phase": "review", "finalize_ctx": ctx})
    assert not handed_on({"status": "blocked", "phase": "done", "finalize_ctx": {}})


def _item(label: str, kind: str) -> dict:
    return {"id": "w1", "label": "Fix the open review item", "state": "running", "age": "1h",
            "source": "code_task", "href": "#",
            "stages": [{"key": "build", "state": "now"}, {"key": "review", "state": "todo"}],
            "attempts": [{"label": label, "kind": kind}, {"label": "Build", "kind": "build"}]}


def test_only_a_follow_up_running_says_boss_is_polishing():
    assert polishing(_item("Follow-up 1 · checks failing", "followup"))
    assert polishing(_item("Follow-up 1 · retry 1", "retry"))
    assert not polishing(_item("Retry 1", "retry"))
    t = tiles("p", [_item("Follow-up 1 · checks failing", "followup")], [], {})
    assert t["running"]["current"]["sub"].startswith(POLISHING_WORDS)
    t = tiles("p", [_item("Retry 1", "retry")], [], {})
    assert t["running"]["current"]["sub"].startswith("Building")
