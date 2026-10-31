"""An app that says how it runs is previewed and deployed the way it says, whether the build
wrote its boss.deploy.yaml or the repository already had it (2026-10-31).

BOSS's own repository declares itself (boss.deploy.yaml: a stack installed on a VM). Builds that
added features to it were treated as undeclared, because only a manifest the build WROTE counted:
the preview ran apps/api/app/main.py as one process ("No module named 'boss_engine'"), and every
finished, merged build read as failed.
"""
from __future__ import annotations

from boss_engine.services import app_deploy_manifest as m


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
