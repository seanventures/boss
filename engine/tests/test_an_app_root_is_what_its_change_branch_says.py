"""An app's root on its change branch is what the branch bears out (BOSSAPP-1323/1324).

THE REAL ROWS (northgate-gateway, lineage crun_24f399769d9f4f5d8fa1, branch
boss/code/crun_24f399769d9f4f5d8fa1: a Go module at the root, 53 .go files, and `schema/` — a Go
package folder holding schema.go and tenant.schema.json, no go.mod):

  crun_9a9a535c  completed  app_dir: —        verify_cwd: .
  crun_8abe60c6  blocked    app_dir: schema   verify_cwd: schema   (findings follow-up, pre-#148)
  crun_ce3bf124, crun_a20439cf, crun_5c4fca0a, crun_b14450fc, crun_95aca9c7, crun_bbf93235:
                            app_dir: schema
`recorded_app_dir` reads the lineage's newest record, and a record OUTRANKED every per-attempt
guess, so once 8abe wrote `schema` every later build — "Change it" builds included — verified,
re-checked and previewed inside `schema/`; and the root was never recorded, so nothing could
supersede it. Now a record stands only where the tree bears it out (app_root.home_on_tree), the
root is recorded as ".", and a continuation whose workspace is not its lineage's checkout (another
repository, or not the branch's head) stops before planning (verification_target.
checkout_problem) — never for the stack or an empty tree (PR #164 review E2).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import text

from boss_engine.services import app_root
from boss_engine.services import verification_target as vt
from boss_engine.services.preview_service import local_preview_command

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

ROOT_RUN = "crun_24f399769d9f4f5d8fa1"


def _go_branch(root: Path) -> Path:
    for rel, body in {
        "go.mod": "module northgate\n\ngo 1.23\n",
        "cmd/northgate-gateway/main.go": "package main\n\nfunc main() {}\n",
        "internal/config/loader.go": "package config\n",
        "schema/schema.go": "package schema\n",
        "schema/tenant.schema.json": "{}\n",
        "boss.preview.command": "go run ./cmd/northgate-gateway\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    return root


def test_a_stale_subfolder_record_never_outranks_the_module_at_the_root(tmp_path):
    root = _go_branch(tmp_path)
    assert app_root.home_on_tree(root, "schema") is None
    where = app_root.resolve(root, recorded="schema")
    assert where.path == root and where.source == "single_app"
    # A record the tree bears out still stands: an app folder with its own manifest.
    (root / "web").mkdir()
    (root / "web/package.json").write_text("{}")
    assert app_root.home_on_tree(root, "web") == "web"
    assert app_root.resolve(root, recorded="web").source == "recorded"
    # A folder that is not there is no record.
    assert app_root.home_on_tree(root, "gone") is None


def test_the_preview_never_runs_in_the_stale_folder(tmp_path):
    root = _go_branch(tmp_path)
    command = local_preview_command(root, named="schema")
    assert command and "cd schema" not in " ".join(command)


def _run(c, project_id, rid, fctx, created):
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx, created_at)"
        " VALUES (:i, :p, 'completed', 'KAN-9 Northgate API Gateway', CAST(:s AS JSONB), "
        "CAST(:f AS JSONB), now() - make_interval(mins => :m))"),
        {"i": rid, "p": project_id, "m": created,
         "s": json.dumps({"owner": "local_northgate-gateway", "repo": "northgate-gateway"}),
         "f": json.dumps({"root_run_id": ROOT_RUN, **fctx})})


def test_the_root_recorded_supersedes_the_lineages_stale_record(eng, project):  # noqa: F811
    tag = uuid.uuid4().hex[:6]
    try:
        with eng.begin() as c:
            _run(c, project, f"crun_9a9a_{tag}", {}, 60)
            _run(c, project, f"crun_8abe_{tag}", {"app_dir": "schema", "is_followup": True}, 50)
            _run(c, project, f"crun_95ac_{tag}", {"app_dir": "schema"}, 40)
            new = f"crun_next_{tag}"
            _run(c, project, new, {}, 1)
            run = {"id": new, "project_id": project, "title": "KAN-9 Northgate API Gateway",
                   "spec": {"owner": "local_northgate-gateway", "repo": "northgate-gateway"},
                   "finalize_ctx": {"root_run_id": ROOT_RUN}}
            assert app_root.recorded_app_dir(c, run) == "schema"     # the stale record
            assert app_root.remember_app_dir(c, new, ".") is True    # the tree said: the root
            assert app_root.recorded_app_dir(c, run) is None         # the root, from now on
            # Nothing is recorded for an unknown app; a subfolder still records as before.
            assert app_root.remember_app_dir(c, new, None) is False
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})


def test_the_pipeline_validates_the_record_and_records_the_root():
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    assert "_app_root.home_on_tree(root, _app_root.recorded_app_dir(_c, run))" in src
    assert 'if _known.path != Path(root) else "."' in src
    assert "verification_target.checkout_problem(" in src
    assert "continuation_tree_problem" not in src


def _git(cwd, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _checkout(path: Path, *, url="https://github.com/acme/northgate-gateway.git",
              branch="boss/code/crun_24f3", files=None) -> Path:
    """A clone as the sandbox makes one: the branch checked out, origin/<branch> at its head."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    for rel, body in (files or {"package.json": "{}", "src/server.js": "export {}\n"}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(body)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "head", "--allow-empty")
    _git(path, "remote", "add", "origin", url)
    _git(path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    return path


def test_a_rewrite_follow_up_on_its_branch_is_never_stopped(tmp_path):
    """A Node tree on a Go lineage's branch is what a Node-to-Go rewrite builds on; an empty
    first build's retry, an empty slice branch and a polyglot repo the same: never stopped."""
    node = _checkout(tmp_path / "node")
    assert vt.checkout_problem(node, branch="boss/code/crun_24f3", owner="acme",
                               repo="northgate-gateway") is None
    empty = _checkout(tmp_path / "empty", files={})
    assert vt.checkout_problem(empty, branch="boss/code/crun_24f3", owner="Acme",
                               repo="Northgate-Gateway") is None
    # A follow-up's own commits on top of the branch head are still the branch.
    (node / "main.go").write_text("package main\n")
    _git(node, "add", "-A")
    _git(node, "commit", "-qm", "rewrite")
    assert vt.checkout_problem(node, branch="boss/code/crun_24f3", owner="acme",
                               repo="northgate-gateway") is None
    assert vt.checkout_problem(tmp_path / "not-a-checkout") is None


def test_a_checkout_of_another_repository_is_stopped(tmp_path):
    other = _checkout(tmp_path / "other", url="git@github.com:acme/old-node-gateway.git")
    problem = vt.checkout_problem(other, branch="boss/code/crun_24f3", owner="acme",
                                  repo="northgate-gateway")
    assert problem and "acme/old-node-gateway" in problem and "acme/northgate-gateway" in problem


def test_a_checkout_that_is_not_the_branch_head_is_stopped(tmp_path):
    co = _checkout(tmp_path / "co")
    # The branch moved on elsewhere: its head is not in this checkout's history.
    _git(co, "checkout", "-q", "--orphan", "elsewhere")
    _git(co, "commit", "-qm", "unrelated", "--allow-empty")
    _git(co, "update-ref", "refs/remotes/origin/boss/code/crun_24f3", "HEAD")
    _git(co, "checkout", "-q", "boss/code/crun_24f3")
    problem = vt.checkout_problem(co, branch="boss/code/crun_24f3", owner="acme",
                                  repo="northgate-gateway")
    assert problem and "not on boss/code/crun_24f3" in problem


def test_a_recorded_app_folder_of_sources_beats_a_tooling_only_root_manifest(tmp_path):
    """Low (PR #164 review): a root package.json for tooling never resets a recorded Python
    service folder; a Go package folder under the module's go.mod still yields to the root."""
    (tmp_path / "package.json").write_text('{"devDependencies": {"prettier": "3"}}')
    svc = tmp_path / "service"
    svc.mkdir()
    (svc / "main.py").write_text("print('ok')\n")
    assert app_root.home_on_tree(tmp_path, "service") == "service"
    assert app_root.home_on_tree(_go_branch(tmp_path / "go"), "schema") is None
