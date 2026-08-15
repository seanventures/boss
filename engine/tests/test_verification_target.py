"""App-scoped official verify: cwd + missing verify script."""
from __future__ import annotations

import json
from pathlib import Path

from boss_engine.services.verification_target import (
    ensure_verify_script,
    official_app_id,
    resolve_verify_cwd,
)


def test_verify_cwd_prefers_named_app_package(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name":"mono"}', encoding="utf-8")
    app = tmp_path / "hookwatch"
    app.mkdir()
    (app / "package.json").write_text('{"name":"hookwatch"}', encoding="utf-8")
    assert resolve_verify_cwd(tmp_path, app_id="hookwatch") == app


def test_verify_cwd_follows_changed_files_when_app_id_missing(tmp_path: Path):
    app = tmp_path / "approveit"
    app.mkdir()
    (app / "package.json").write_text("{}", encoding="utf-8")
    (app / "src").mkdir()
    (app / "src" / "index.ts").write_text("export {}", encoding="utf-8")
    assert resolve_verify_cwd(
        tmp_path, changed=["approveit/src/index.ts"]
    ) == app


def test_verify_cwd_falls_back_to_repo_root(tmp_path: Path):
    assert resolve_verify_cwd(tmp_path, app_id="missing") == tmp_path


def test_ensure_verify_script_chains_existing_scripts_and_does_not_overwrite(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps({"scripts": {"typecheck": "tsc", "test:e2e": "playwright"}}),
        encoding="utf-8",
    )
    assert ensure_verify_script(tmp_path) is True
    scripts = json.loads(pkg.read_text(encoding="utf-8"))["scripts"]
    assert scripts["verify"] == "npm run typecheck && npm run test:e2e"
    assert ensure_verify_script(tmp_path) is False
    assert json.loads(pkg.read_text(encoding="utf-8"))["scripts"]["verify"] == scripts["verify"]


def test_official_app_id_reads_benchmark_context():
    assert official_app_id(
        {}, {"qa_benchmark": {"id": "approveit", "app_root": "approveit"}}
    ) == "approveit"
    assert official_app_id({"app_root": "bugbox"}, {}) == "bugbox"
    assert official_app_id({}, {}) is None
