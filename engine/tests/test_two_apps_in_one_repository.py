"""ONE PROJECT, TWO APPS, ONE REPOSITORY (ENT-FEAT-01, ENT-REC-51): each app's history is its own.

THE REAL ROWS (enterprise validation, rc10.35): Recipe Box was built into the Northgate project's
repository `local_northgate/northgate`, its change still open on `boss/code/<recipe root>`.
Neighborhood Board was then started as a NEW app into the same project (Command Center › Into
Northgate). Its build (crun_19b39c29…) cloned main — a README — and stopped before planning:
"local main had no app and Recipe Box was on an unmerged branch". The two runs carried distinct
app ids (app_cc8d45de… and app_67927ac7…); the empty-checkout guard (build_checkout) searched
earlier completed builds by REPOSITORY and never asked for them.

The fix (services/app_scope.py): a build's history is looked up by its APP (its lineage and its
recorded app id), and a build that declares a new app starts from the default branch beside the
other apps. These tests run the real pipeline (test_the_pipeline_runs_end_to_end's harness: a
local GitHub, the scripted engine, the bench database) on ONE repository holding two apps, and
scan the engine so no new lookup of a build's history scopes by repository alone.

    PYTHONPATH=<repo>:<repo>/engine BOSS_TEST_DATABASE_URL=… pytest engine/tests/test_two_apps_in_one_repository.py
"""
from __future__ import annotations

import ast
import contextlib
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import pytest

from .test_the_pipeline_runs_end_to_end import OWNER, pipeline  # noqa: F401 — fixture

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"
API = Path(__file__).resolve().parents[2] / "apps" / "api" / "app"

RECIPE, BOARD = "recipe-box", "neighborhood-board"


# ── the guard: no lookup of a build's history scopes by repository alone ─────────────────────

#: A SQL filter on a build's repository (`spec->>'repo' = …`, lower(...) or COALESCE'd).
_REPO_FILTER = re.compile(
    r"(lower\(\s*)?(COALESCE\(\s*)?(\w+\.)?spec\s*->>\s*'repo'(\s*,\s*''\s*\))?(\s*\))?"
    r"\s*(=|IN\s*\()", re.I)

#: The lookups that are about the REPOSITORY, not one app's history, each reviewed. Anything
#: else that filters builds by repository goes through app_scope.same_app_sql.
REPOSITORY_LOOKUPS = {
    # a build that names no app: which app it meant is exactly what is unknown (AR-54)
    ("services/build_checkout.py", "repository_builds"),
    # then matched by the app's NAME (naming.identity_key): already one app
    ("services/code_task_service.py", "active_same_app"),
    # the repository's branch picker: every branch of the repository
    ("services/delivery_sources.py", "repo_refs"),
    # the push fence: every boss/* branch being built in the repository, whichever app
    ("services/local_git_http.py", "busy_branches"),
    # whether another project still uses the repository's mirror
    ("services/project_files.py", "_shared_elsewhere"),
    # the project's whole footprint in a repository (all its apps' folders)
    ("services/project_scope.py", "_run_paths"),
    # the repository's activity card (runs, PRs, spend of every app in it)
    ("services/repo_registry.py", "repository_activity"),
}


def _repository_lookups() -> set[tuple[str, str]]:
    found = set()
    for base, root in ((ENGINE, ENGINE), (API, API.parents[1])):
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                sql = " ".join(n.value for n in ast.walk(node)
                               if isinstance(n, ast.Constant) and isinstance(n.value, str))
                if _REPO_FILTER.search(sql):
                    found.add((path.relative_to(root).as_posix(), node.name))
    return found


def test_no_lookup_of_a_builds_history_scopes_by_repository_alone():
    found = _repository_lookups()
    assert ("services/build_checkout.py", "repository_builds") in found, (
        "the scan found nothing: it no longer sees the engine")
    stray = found - REPOSITORY_LOOKUPS
    assert not stray, (
        f"{sorted(stray)} look up builds by repository. Two apps share a repository "
        "(ENT-FEAT-01): scope the lookup to the app with app_scope.same_app_sql / "
        "same_app_params, or, if it is genuinely about the whole repository, add it to "
        "REPOSITORY_LOOKUPS with the reason.")
    gone = REPOSITORY_LOOKUPS - found
    assert not gone, f"{sorted(gone)} no longer filter by repository: drop them from the list"


def test_the_app_lookups_share_one_definition():
    """The empty-checkout guard and the app's folder record ask app_scope, never their own SQL."""
    import inspect

    from boss_engine.services import app_root, build_checkout

    for fn in (build_checkout.earlier_builds, app_root.recorded_app_dir, app_root.identity_paths):
        assert "same_app_sql()" in inspect.getsource(fn), fn.__name__


# ── the real pipeline on one repository holding two apps ────────────────────────────────────

def _git(*args: str, cwd: Optional[Path] = None, git_dir: Optional[Path] = None) -> str:
    cmd = ["git", *(["--git-dir", str(git_dir)] if git_dir else []), *args]
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True, capture_output=True,
                          text=True).stdout


def _files_on(git_dir: Path, branch: str) -> set[str]:
    return set(_git("ls-tree", "-r", "--name-only", branch, git_dir=git_dir).split())


def _start(p, repo: str, *, title: str, app_dir: str, new_app: bool, faults: str = "") -> str:
    """A build started as create_code_task starts one (new_app recorded the way the tool does)."""
    from boss_engine.services import code_task_service

    p._engine_env(faults)
    os.environ["BOSS_SCRIPTED_APP_DIR"] = app_dir
    with p.engine.begin() as c:
        return code_task_service.start_code_task(
            c, project_id=p.project, task_id=None, title=title,
            instructions=(f"Build a tiny plain Node ESM service inside `{app_dir}/`: slug(text) "
                          "in src/slug.js and an HTTP server in src/server.js answering "
                          "GET /?q=<text> with {\"slug\": ...} on $PORT. Tests under node --test."),
            acceptance_criteria=["slug lowercases and joins words with hyphens",
                                 "GET /?q= answers the slug as JSON"],
            test_commands=["npm test"], owner=OWNER, repo=repo, base_branch="main",
            finalize_ctx={"new_app": True} if new_app else {}, jobs=p.jobs(repo),
            model_config={"default": "scripted"}, open_pr=True, allow_duplicate=True,
            profile="balanced", risk_tier=1)


@pytest.fixture(scope="module")
def northgate(pipeline):  # noqa: F811
    """One repository (README only on main) and Recipe Box built into it, its change open."""
    repo = f"northgate-{uuid.uuid4().hex[:6]}"
    pipeline.repos[repo] = pipeline.hub.seed(OWNER, repo, {"README.md": "# northgate\n"})
    recipe = _start(pipeline, repo, title="Recipe Box", app_dir=RECIPE, new_app=True)
    error = pipeline.run(recipe, repo)
    out = pipeline.result(recipe)
    assert error is None, error
    assert out["run"]["status"] == "completed", (out["run"]["status"], out["run"].get("error"))
    return {"repo": repo, "recipe": recipe, "git": pipeline.repos[repo],
            "recipe_head": pipeline.branch_heads(repo)[f"boss/code/{recipe}"]}


def test_a_second_app_builds_from_main_while_the_first_is_unmerged(pipeline, northgate):  # noqa: F811
    repo, git = northgate["repo"], northgate["git"]
    assert _files_on(git, "main") == {"README.md"}            # Recipe Box is not merged
    board = _start(pipeline, repo, title="Neighborhood Board", app_dir=BOARD, new_app=True)
    error = pipeline.run(board, repo)
    out = pipeline.result(board)
    run = out["run"]
    assert error is None, error
    assert "code_task.wrong_tree" not in out["events"], run.get("error")
    assert run["status"] == "completed", (run["status"], run.get("error"))
    assert run["finalize_ctx"]["app_id"] != pipeline.result(northgate["recipe"])["run"][
        "finalize_ctx"]["app_id"]
    heads = pipeline.branch_heads(repo)
    # Started from main, beside Recipe Box: none of Recipe Box's files on its branch...
    on_board = _files_on(git, f"boss/code/{board}")
    assert any(f.startswith(f"{BOARD}/") for f in on_board), on_board
    assert not any(f.startswith(f"{RECIPE}/") for f in on_board), on_board
    # ...and Recipe Box's code untouched.
    assert heads[f"boss/code/{northgate['recipe']}"] == northgate["recipe_head"]


def test_a_build_that_names_no_app_still_stops_on_the_empty_mainline(pipeline, northgate):  # noqa: F811
    """AR-54 kept: a build that does not say it is a new app, on a README-only main, beside an
    app on an unmerged branch, may be that app rebuilt from scratch: it stops before planning."""
    repo = northgate["repo"]
    lost = _start(pipeline, repo, title="Validate and fix", app_dir="plain", new_app=False)
    pipeline.run(lost, repo)
    out = pipeline.result(lost)
    assert out["run"]["status"] == "failed", out["run"]["status"]
    assert "code_task.wrong_tree" in out["events"]
    assert f"boss/code/{northgate['recipe']}" in out["run"]["error"]
    assert "start it as a new app" in out["run"]["error"]
    assert out["calls"] == [], "it reached a model"


def test_the_same_app_started_again_on_the_empty_mainline_stops(pipeline, northgate):  # noqa: F811
    """Declaring "new app" does not make Recipe Box a new app: the same recorded app id with its
    code on an unmerged branch is still refused, naming that branch."""
    repo = northgate["repo"]
    again = _start(pipeline, repo, title="Recipe Box", app_dir=RECIPE, new_app=True)
    pipeline.run(again, repo)
    out = pipeline.result(again)
    assert out["run"]["status"] == "failed"
    assert "already built this app" in out["run"]["error"]
    assert f"boss/code/{northgate['recipe']}" in out["run"]["error"]


def test_a_new_apps_retry_stays_on_that_app(pipeline, northgate):  # noqa: F811
    """ENT-REC-51: a new app whose first attempt died is retried on ITS lineage — not stopped by
    Recipe Box's branch, not on Recipe Box's branch — and Recipe Box's branch is untouched."""
    from boss_engine.services import code_task_service

    repo, git = northgate["repo"], northgate["git"]
    first = _start(pipeline, repo, title="Parking Rota", app_dir="parking-rota", new_app=True,
                   faults="crash")
    pipeline.run(first, repo)
    died = pipeline.result(first)["run"]
    assert died["status"] in ("failed", "blocked"), died["status"]
    assert "code_task.wrong_tree" not in pipeline.result(first)["events"], died.get("error")

    pipeline._engine_env("")
    os.environ["BOSS_SCRIPTED_APP_DIR"] = "parking-rota"
    retried = code_task_service.retry_code_task(
        pipeline.engine, pipeline.settings(repo), pipeline.jobs(repo), first, by_person=True)
    child = str(retried.get("run_id") or retried.get("new_run_id"))
    error = pipeline.run(child, repo)
    out = pipeline.result(child)
    run = out["run"]
    assert error is None, error
    assert run["status"] == "completed", (run["status"], run.get("error"))
    assert run["finalize_ctx"].get("root_run_id") == first
    assert run["finalize_ctx"].get("new_app") is True
    assert run["finalize_ctx"]["app_id"] == died["finalize_ctx"]["app_id"]
    on_branch = _files_on(git, f"boss/code/{first}")
    assert any(f.startswith("parking-rota/") for f in on_branch), on_branch
    assert not any(f.startswith((f"{RECIPE}/", f"{BOARD}/")) for f in on_branch), on_branch
    assert pipeline.branch_heads(repo)[f"boss/code/{northgate['recipe']}"] == \
        northgate["recipe_head"]


def test_a_new_app_after_the_first_is_merged_builds_beside_it(pipeline, northgate):  # noqa: F811
    """Recipe Box merged into main: the next new app starts from main WITH Recipe Box in it, and
    leaves Recipe Box's files exactly as they were."""
    repo, git = northgate["repo"], northgate["git"]
    recipe_branch = f"boss/code/{northgate['recipe']}"
    # The merge (fast-forward main onto Recipe Box's head, as a merged PR leaves it).
    _git("update-ref", "refs/heads/main", northgate["recipe_head"], git_dir=git)
    merged = {f: _git("show", f"main:{f}", git_dir=git) for f in _files_on(git, "main")
              if f.startswith(f"{RECIPE}/")}
    assert merged, "Recipe Box is not on main"
    garden = _start(pipeline, repo, title="Garden Swap", app_dir="garden-swap", new_app=True)
    error = pipeline.run(garden, repo)
    out = pipeline.result(garden)
    assert error is None, error
    assert out["run"]["status"] == "completed", (out["run"]["status"], out["run"].get("error"))
    branch = f"boss/code/{garden}"
    on_branch = _files_on(git, branch)
    assert any(f.startswith("garden-swap/") for f in on_branch), on_branch
    for f, content in merged.items():
        assert _git("show", f"{branch}:{f}", git_dir=git) == content, f"{f} changed"
    assert pipeline.branch_heads(repo)[recipe_branch] == northgate["recipe_head"]


def test_an_approved_prd_declares_a_new_app_only_when_nothing_says_otherwise(
        pipeline, northgate, monkeypatch):  # noqa: F811
    """launch_prd_build: an empty repository AND no app in the project is a new app; beside an
    app the PRD may be that app's feature, so nothing is declared (the guard decides)."""
    from boss_engine.services import decisions
    from boss_engine.services import prd_decomposition as pd

    asked: list[bool] = []
    monkeypatch.setattr(pd, "project_github", lambda *a, **k: None)
    monkeypatch.setattr(pd, "repository_has_code", lambda github: False)
    monkeypatch.setattr(pd, "_looks_like_web_app", lambda text: False)
    monkeypatch.setattr(pd, "start_decomposed_build",
                        lambda *a, **k: asked.append(k["new_app"]) or {"started": False})
    for project in (pipeline.project, f"p_none{uuid.uuid4().hex[:6]}"):
        # Only the routing is under test: what launch_prd_build records afterwards about a
        # build that never started (a project row the second id does not have) is not.
        with contextlib.suppress(Exception):
            decisions.launch_prd_build(
                pipeline.engine, pipeline.settings(northgate["repo"]), None, project_id=project,
                decision_id="dec_none", task_id="task_none", actor="operator", title="t",
                directive="A parking rota.", owner=OWNER, repo=northgate["repo"])
    assert asked == [False, True]
