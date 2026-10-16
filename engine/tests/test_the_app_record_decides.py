"""THE APP'S RECORD DECIDES WHERE IT LIVES; A SHARED ROOT IS NEVER THE APP BY DEFAULT.

HelpDesk (helpdesk-ai/, one npm project) in paramg/boss-sandbox, whose root holds a pom.xml (an
old Java demo), a workspaces package.json and a dozen other apps, was blocked "This app needs
Java" four times. The fourth (crun_cd48617a): an earlier retry had rewritten the root's
package.json and lockfile beside its 27 helpdesk-ai/ files, every later attempt was seeded with
them, and "the root's manifest changed" outvoted the app's own files. These tests pin that shape
through resolve_gate_plan and the template gates' directory, not just the resolver.
"""
from __future__ import annotations

import json
from pathlib import Path

from boss_engine.services import app_root, verification_target


def _write(p: Path, text: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _shared_repo(tmp_path: Path) -> list[str]:
    """boss-sandbox's shape; returns HelpDesk's files (what its lineage wrote)."""
    _write(tmp_path / "pom.xml", "<project><artifactId>demo</artifactId></project>")
    _write(tmp_path / "src" / "main" / "java" / "Main.java", "class Main {}")
    _write(tmp_path / "package.json", json.dumps({"private": True,
                                                  "workspaces": ["web", "web-nqueens"]}))
    _write(tmp_path / "package-lock.json", "{}")
    for app in ("web", "web-nqueens", "web-stock"):
        _write(tmp_path / app / "package.json", json.dumps({"name": app}))
    _write(tmp_path / "serve.py", "print('sudoku')")
    files = ["helpdesk-ai/package.json", "helpdesk-ai/server.mjs", "helpdesk-ai/auth/config.mjs",
             "helpdesk-ai/tests/auth.test.mjs", "helpdesk-ai/boss.deploy.yaml"]
    _write(tmp_path / "helpdesk-ai" / "package.json",
           json.dumps({"name": "helpdesk-ai", "scripts": {"test": "node --test"}}))
    for f in files[1:]:
        _write(tmp_path / f, "x")
    return files


def test_a_root_manifest_seeded_from_a_retry_never_outvotes_the_apps_files(tmp_path):
    lineage = _shared_repo(tmp_path)
    plan = verification_target.resolve_gate_plan(
        {}, "HelpDesk AI · retry 4", tmp_path,
        changed=["package.json", "package-lock.json"], lineage=lineage)
    assert plan.source not in ("toolchain", "toolchain_missing", "stack"), plan
    assert plan.cwd == tmp_path / "helpdesk-ai"
    assert "Java" not in (plan.error or "")


def test_a_node_app_gets_npm_whatever_the_brief_sounded_like(tmp_path):
    lineage = _shared_repo(tmp_path)
    plan = verification_target.resolve_gate_plan({}, "x", tmp_path, lineage=lineage)
    assert plan.cwd == tmp_path / "helpdesk-ai"
    assert plan.commands and plan.commands[0].split(" ", 1)[0] == "npm", plan.commands


def test_the_recorded_folder_wins_with_no_evidence_at_all(tmp_path):
    _shared_repo(tmp_path)
    plan = verification_target.resolve_gate_plan({}, "x", tmp_path, recorded="helpdesk-ai")
    assert plan.cwd == tmp_path / "helpdesk-ai"


def test_no_evidence_in_a_shared_repo_is_refused_never_the_root(tmp_path):
    _shared_repo(tmp_path)
    plan = verification_target.resolve_gate_plan({}, "x", tmp_path)
    assert plan.source == "app_unknown"
    assert plan.error and "cannot tell which folder" in plan.error
    assert not plan.commands


def test_a_single_app_repo_is_still_its_own_root(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"name": "solo"}))
    _write(tmp_path / "server.js", "x")
    plan = verification_target.resolve_gate_plan({}, "x", tmp_path)
    assert plan.cwd == tmp_path and plan.source == "default"


def test_the_template_gates_directory_follows_the_same_identity(tmp_path):
    lineage = _shared_repo(tmp_path)
    where = app_root.gate_dir(tmp_path, changed=["package.json", "package-lock.json"],
                              lineage=lineage)
    assert where == tmp_path / "helpdesk-ai"
    assert app_root.gate_dir(tmp_path, recorded="helpdesk-ai") == tmp_path / "helpdesk-ai"
    assert app_root.gate_dir(tmp_path) is None  # refused, never the shared root


def test_a_subfolder_apps_java_check_never_reads_the_root_pom(tmp_path):
    lineage = _shared_repo(tmp_path)
    where, build = app_root.java_build(tmp_path, changed=["package.json"], lineage=lineage)
    assert where == tmp_path / "helpdesk-ai" and build is None
