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
root is recorded as ".", and a continuation whose tree is not its lineage's app stops before
planning (verification_target.continuation_tree_problem).
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
    assert "verification_target.continuation_tree_problem(" in src


def test_a_continuation_on_a_tree_that_is_not_its_app_stops_before_planning(tmp_path):
    node = tmp_path / "node"
    for rel in ("package.json", "gateway/src/server.js", "gwlint/src/rules.js"):
        (node / rel).parent.mkdir(parents=True, exist_ok=True)
        (node / rel).write_text("{}" if rel.endswith(".json") else "export {}\n")
    problem = vt.continuation_tree_problem(node, required="go",
                                           branch="boss/code/crun_24f399769d9f4f5d8fa1")
    assert problem and "does not hold this lineage's Go app" in problem and "go.mod" in problem
    assert vt.continuation_tree_problem(_go_branch(tmp_path / "go"), required="go") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no project files" in vt.continuation_tree_problem(empty, required=None)
