"""An app that says how it runs is previewed and deployed the way it says, whether the build
wrote its boss.deploy.yaml or the repository already had it (2026-10-31).

BOSS's own repository declares itself (boss.deploy.yaml: a stack installed on a VM). Builds that
added features to it were treated as undeclared, because only a manifest the build WROTE counted:
the preview ran apps/api/app/main.py as one process ("No module named 'boss_engine'"), and every
finished, merged build read as failed.
"""
from __future__ import annotations

from boss_engine.services import app_deploy_manifest as m
from boss_engine.services import workflow_capability_runner as runner


def test_a_manifest_the_build_wrote_still_wins():
    files = ["helpdesk/boss.deploy.yaml", "helpdesk/server.js"]
    assert m.app_manifest(files, ["boss.deploy.yaml"]) == "helpdesk/boss.deploy.yaml"


def test_a_root_app_uses_the_root_manifest_its_repository_already_had():
    files = ["engine/boss_engine/services/x.py", "apps/web/app/page.tsx"]
    existing = ["boss.deploy.yaml", "tests/e2e/fixtures/helpdesk-mini/helpdesk/boss.deploy.yaml"]
    assert m.app_manifest(files, existing) == "boss.deploy.yaml"


def test_an_app_in_a_folder_uses_that_folders_manifest_not_the_roots():
    files = ["helpdesk/server.js", "helpdesk/routes/tickets.js"]
    assert m.app_manifest(files, ["boss.deploy.yaml", "helpdesk/boss.deploy.yaml",
                                  "stack-demo/boss.deploy.yaml"]) == "helpdesk/boss.deploy.yaml"


def test_a_repository_whose_app_has_no_manifest_is_undeclared():
    assert m.app_manifest(["onboarding/app.py"], ["boss.deploy.yaml"]) is None
    assert m.app_manifest(["main.py"], []) is None
    assert m.app_manifest([], ["boss.deploy.yaml"]) is None


def test_the_checkout_is_searched_without_dependencies_or_worktrees(tmp_path):
    for rel in ("boss.deploy.yaml", "helpdesk/boss.deploy.yaml",
                "node_modules/pkg/boss.deploy.yaml", ".claude/worktrees/a/boss.deploy.yaml",
                "a/b/c/d/boss.deploy.yaml"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("version: 1\n")
    assert m.manifests_in(tmp_path) == ["boss.deploy.yaml", "helpdesk/boss.deploy.yaml"]
    assert m.manifests_in(tmp_path / "missing") == []


def _fail_preview(monkeypatch, manifest):
    completed = {}
    monkeypatch.setattr(runner, "_stack_the_app_declares", lambda _c, _p: manifest)
    monkeypatch.setattr(runner.workflows, "force_complete_waiting_step",
                        lambda _c, **kw: completed.update(kw))
    done = []
    took = runner._complete_advisory_failure(
        None, project_id="p", run_id="wfr_1", step={"id": "s1", "step_id": "preview"},
        output={"capability": "preview", "tool": "start_preview"}, status="failed",
        why="the app exited (code 1)", done=done, awaited_id="prev_1")
    return took, completed, done


def test_a_failed_preview_of_a_declared_stack_is_not_applicable(monkeypatch):
    took, completed, done = _fail_preview(monkeypatch, "boss.deploy.yaml")
    assert took and done[0]["outcome"] == "declared_stack"
    out = completed["output"]
    assert out["status"] == "not_applicable" and out["declared_stack"] == "boss.deploy.yaml"
    assert "deployed to its target" in out["note"] and "code 1" in out["note"]


def test_a_failed_preview_of_an_ordinary_app_still_fails(monkeypatch):
    took, completed, _ = _fail_preview(monkeypatch, None)
    assert took is False and not completed
