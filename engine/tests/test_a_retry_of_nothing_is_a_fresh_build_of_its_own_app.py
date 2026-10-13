"""A retry of a build that produced nothing is a FRESH build of the same brief, in its own app.

crun_37f4ccef (2026-10-13) was a person's Retry of crun_2f76585e, which had died at the clone after
1.1 min and left nothing. It ran as a REPAIR: "A working test suite ALREADY EXISTS in this tree" —
the repo's 37 test files of OTHER apps at the root (Python, Java, JS harnesses) — reused them as
its oracle (tests_reused: repair_augment_added_no_files), counted their 735 cases against a test
budget of 20, and told the test author to delete cases from them. The author refused (it read "do
not delete any of them" in its own prompt), so nothing was deleted: after_trim 0 only because the
recount looked at the diff, which held no test file. The build then failed "no test files".

  (4) a retry whose lineage left no work (no completed stage, design, saved workspace or PR) is
      marked `fresh_build`: the brief as written, no "your previous code is present", no
      "existing tests still pass" criterion; the pipeline's repair paths read `_is_repair`;
  (5) the test budget counts only the test files THIS build wrote, inside its app's directory,
      and its trim turn names exactly those files and forbids touching any other test;
  (6) a repair's "existing suite as oracle" is the app directory's tests, never the repo root's.
"""
from __future__ import annotations

import inspect
import json
import re

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services.verification_target import app_scope, brief_app_dir, in_app_scope

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import WORKER_KEY, _workflow_build
from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import staffed  # noqa: F401

BRIEF = ("Build **HelpDesk AI**, a polished customer support web app, in the `helpdesk-ai/` folder of "
         "this repository.\n\n**Stack:** ONE npm project in `helpdesk-ai/` (TypeScript throughout).\n"
         "Copy `tests/template_login_oauth/` (the tests and the fake provider) to the same path.")
OTHER_APPS = ["tests/test_board.py", "tests/nqueens_harness.mjs", "src/test/java/org/x/SmokeTest.java",
              "web-stock/stock.test.js"]


def test_the_app_folder_is_the_one_the_brief_names():
    assert brief_app_dir(BRIEF) == "helpdesk-ai"
    assert app_scope({"instructions": BRIEF}, {}) == "helpdesk-ai"
    # A pinned benchmark app wins; a brief naming none (or several) leaves the whole repo.
    assert app_scope({"instructions": BRIEF, "app_root": "bench-app"}, {}) == "bench-app"
    assert brief_app_dir("Build a todo app.") is None
    assert brief_app_dir("Put the API in the `api/` folder and the UI in the `web/` folder.") is None
    # Only the brief: a retry's iteration notes never move the app.
    assert brief_app_dir(BRIEF + "\n\n=== ITERATION FEEDBACK (attempt 2) ===\nin `elsewhere/`") == "helpdesk-ai"


def test_the_budget_counts_only_this_builds_tests_in_its_app():
    written = ["helpdesk-ai/tests/tickets.test.ts", "helpdesk-ai/tests/auth.test.ts",
               "tests/template_login_oauth/login.test.mjs", *OTHER_APPS]
    counted = governed.budget_files(written, preexisting=OTHER_APPS,
                                    placed=["tests/template_login_oauth/login.test.mjs"],
                                    scope="helpdesk-ai")
    assert counted == ["helpdesk-ai/tests/tickets.test.ts", "helpdesk-ai/tests/auth.test.ts"]
    # A pre-existing test of the app itself is not this build's either.
    assert governed.budget_files(["helpdesk-ai/a.test.ts"], ["helpdesk-ai/a.test.ts"],
                                 scope="helpdesk-ai") == []
    # No app folder: the whole repo is the app, but pre-existing tests are still never counted.
    assert governed.budget_files(["a.test.ts", *OTHER_APPS], OTHER_APPS) == ["a.test.ts"]


def test_the_trim_turn_names_its_own_files_and_forbids_every_other_test():
    said = governed._test_budget_trim_prompt(40, 20, ["helpdesk-ai/tests/tickets.test.ts"])
    assert "helpdesk-ai/tests/tickets.test.ts" in said
    assert "Edit ONLY the files listed" in said and "never edit or delete any other test file" in said
    assert not any(p in said for p in OTHER_APPS)


def test_the_pipeline_reads_repair_and_app_scope_from_one_place():
    """Every repair decision reads `_is_repair` (not `root_run != run_id`, which a fresh build of a
    lineage also satisfies); every oracle reads `_app_tests` (not the repo-wide snapshot)."""
    src = inspect.getsource(governed)
    assert '_is_repair = root_run != run_id and not fctx.get("fresh_build")' in src
    stray = [m.group(0) for m in re.finditer(r".*root_run [!=]= run_id.*", src)
             if "_is_repair =" not in m.group(0) and "branch_exists" not in m.group(0)]
    assert stray == [], stray
    assert "test_files = list(_app_tests)" in src and "test_files = list(preexisting_tests)" not in src
    assert "(_is_repair and _app_tests)" in src and "tests_on_branch=bool(_app_tests)" in src
    assert "budget_files(test_files, preexisting_tests" in src
    assert in_app_scope("helpdesk-ai/tests/x.test.ts", "helpdesk-ai")
    assert not in_app_scope("tests/test_board.py", "helpdesk-ai")
    assert not in_app_scope("helpdesk-aix/x.test.ts", "helpdesk-ai")


def _retry(env, crun, note="retry the build as it was; change nothing"):
    with lineage.acting(WORKER_KEY):
        out = cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, fix_note=note)
    with env.engine.begin() as c:
        row = c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id=:i"),
                        {"i": out["run_id"]}).mappings().one()
    return row["spec"], row["finalize_ctx"]


def _with_brief(env, crun, **fctx):
    with env.engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET spec = spec || jsonb_build_object('instructions', "
                       "CAST(:b AS text)), finalize_ctx = finalize_ctx || CAST(:f AS JSONB) WHERE id=:i"),
                  {"b": BRIEF, "f": json.dumps(fctx), "i": crun})


def test_a_retry_of_a_build_that_died_at_the_clone_is_a_fresh_build(env, staffed):  # noqa: F811
    _, crun, _ = _workflow_build(env)
    _with_brief(env, crun)
    spec, fctx = _retry(env, crun)
    assert fctx.get("fresh_build") is True
    said = spec["instructions"]
    assert said.startswith(BRIEF) and "FRESH build" in said and "ALREADY PRESENT" not in said
    assert "retry the build as it was" in said                       # the person's note is kept
    assert not any("existing tests" in str(c) for c in spec.get("acceptance_criteria") or [])
    assert brief_app_dir(said) == "helpdesk-ai"                        # same app folder
    assert not fctx.get("inherited_design") and not fctx.get("seed_workspace_from")


def test_a_retry_of_a_build_that_did_work_is_still_a_repair(env, staffed):  # noqa: F811
    _, crun, _ = _workflow_build(env, error="tests failed")
    _with_brief(env, crun, completed_stages=["design", "write_tests", "implement"])
    spec, fctx = _retry(env, crun, note="fix the login redirect")
    assert not fctx.get("fresh_build")
    assert "ALREADY PRESENT" in spec["instructions"]
