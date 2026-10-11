"""A retry repairs the failed attempt's code; it never rebuilds the whole app.

A template build whose gates fail pushes nothing (the template hold), so its sandbox was thrown
away and every retry re-ran write_tests and implement from nothing — HelpDesk build 7: ~2 hours
and ~$30 a retry, for 7 failing e2e tests. Now every failing exit after implement saves the
changed files on the run (locally, never pushed), and a retry in the lineage seeds its sandbox
from them, skips the authoring stages and starts at the repair, told what failed.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from boss_engine.services import governed_code_task_service as gov

SOURCE = textwrap.dedent(inspect.getsource(gov._run_governed_code_task))  # noqa: SLF001


def _closure(name: str) -> str:
    start = SOURCE.index(f"    def {name}(")
    end = SOURCE.find("\n    def ", start + 10)
    return SOURCE[start:end if end > 0 else start + 3000]


# --- what is kept ---------------------------------------------------------------------------------

def test_dependencies_git_and_build_output_are_not_the_work():
    for kept in ("helpdesk-ai/src/App.tsx", "helpdesk-ai/e2e/login.spec.ts", ".gitignore",
                 "helpdesk-ai/package-lock.json"):
        assert gov.workspace_keepable(kept), kept
    for dropped in ("node_modules/x/index.js", "app/node_modules/a.js", ".git/config",
                    "helpdesk-ai/dist/index.js", "web/.next/x", "helpdesk-ai/test-results/.last-run.json",
                    "playwright-report/index.html", "pkg/__pycache__/m.pyc", ""):
        assert not gov.workspace_keepable(dropped), dropped


def test_the_saved_workspace_wins_and_a_preserved_gate_diff_is_the_fallback():
    saved = {"finalize_ctx": {
        "saved_workspace": {"files": [{"path": "a.ts", "content": "new"},
                                      {"path": "node_modules/x.js", "content": "dep"}]},
        "pending_diff": [{"path": "a.ts", "content": "old"}]}}
    assert gov.saved_workspace_files(saved) == [{"path": "a.ts", "content": "new"}]
    blocked = {"finalize_ctx": {"pending_diff": [{"path": "b.ts", "content": "x"}]}}
    assert gov.saved_workspace_files(blocked) == [{"path": "b.ts", "content": "x"}]
    assert gov.saved_workspace_files({"finalize_ctx": {"pending_diff": []}}) == []
    assert gov.saved_workspace_files(None) == []


def test_seeding_writes_the_files_and_never_outside_the_sandbox(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    n = gov.seed_workspace(root, [
        {"path": "helpdesk-ai/src/App.tsx", "content": "export {}"},
        {"path": "../escaped.txt", "content": "no"},
        {"path": "/etc/passwd-copy", "content": "no"},
    ])
    assert n == 1
    assert (root / "helpdesk-ai/src/App.tsx").read_text() == "export {}"
    assert not (tmp_path / "escaped.txt").exists()


# --- saved on failure, never pushed ---------------------------------------------------------------

def test_every_exit_saves_the_workspace_before_anything_else():
    rescue = _closure("_rescue_pr")
    assert rescue.index("_save_workspace(") < rescue.index('_push_held("partial_pr")'), \
        "a held template build must still keep its work locally"
    assert '_save_workspace("cancelled")' in _closure("_honour_cancel")


def test_the_repair_hand_off_saves_before_starting_the_child():
    at = SOURCE.index('_save_workspace("repair_handoff")')
    assert at < SOURCE.index("nxt = retry_code_task(", at)


def test_saving_is_local_only_and_keeps_no_secret():
    save = _closure("_save_workspace")
    tree = ast.parse(textwrap.dedent(save))
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"upsert_file", "ensure_branch", "propose_pull_request",
                        "create_pull_request"}, "saving the workspace must never push"
    assert "'saved_workspace'" in save
    assert '.get("no_committed_secrets") is False' in save
    assert "if not _impl_ran[0]" in save, "nothing is saved before implement ran"


# --- a retry seeds, skips the authoring stages, and starts at the repair -------------------------

def test_a_seeded_retry_skips_design_write_tests_and_the_first_implement():
    seed = SOURCE[SOURCE.index('_seed_from = str(fctx.get("seed_workspace_from")'):]
    seed = seed[:seed.index("# BUILD SERVICES")]
    assert "seed_workspace(root, _seed_files)" in seed
    assert "saved_workspace_files(load_code_task_run(c, _seed_from))" in seed
    assert "code_task.workspace_seed_missing" in seed, "a missing workspace says so"
    # Seeded after the verify manifest is pinned, never before.
    assert SOURCE.index("_pinned_manifest = verification_target.pin_verify_manifest(root)") \
        < SOURCE.index("seed_workspace(root, _seed_files)")
    assert "do_design = risk_tier >= 2 and not is_web_app and not _seeded" in SOURCE
    skip = SOURCE[SOURCE.index("if do_separate_tests and _seeded:"):]
    assert "do_separate_tests = False" in skip[:400]
    # The seeded tests exist before the pre-existing snapshot is taken, so the existing-tests
    # floor keeps the implementer off the QA suite.
    assert SOURCE.index("seed_workspace(root, _seed_files)") < SOURCE.index(
        "preexisting_tests = sorted(")
    # A retry's first implement pass is the repair (cross-run), fed the iteration feedback.
    assert "_cross_run_repair = (root_run != run_id) and attempt == 0" in SOURCE
    assert 'instructions.split("=== ITERATION FEEDBACK", 1)[-1]' in SOURCE
    assert "impl_prompt = _repair_prompt(base_impl_prompt, _ev, max(1, attempt))" in SOURCE


def test_seeding_pushes_nothing():
    """The hold stands: the seed writes files into the sandbox, and only `_push_held`-guarded
    sites ever reach the remote (test_template_gates_block_the_pr checks every one)."""
    seed = inspect.getsource(gov.seed_workspace)
    assert "github" not in seed and "upsert" not in seed
