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


def test_sandbox_verify_runs_inside_named_app_not_repo_root(tmp_path: Path):
    """Authoritative verify must execute in {app_id}/, not the sandbox monorepo."""
    (tmp_path / "package.json").write_text(
        '{"name":"boss-sandbox","scripts":{}}', encoding="utf-8"
    )
    app = tmp_path / "hookwatch"
    app.mkdir()
    (app / "package.json").write_text('{"name":"hookwatch"}', encoding="utf-8")

    from boss_engine.config import Settings
    from boss_engine.services.sandbox import EphemeralSandbox

    cwd = resolve_verify_cwd(tmp_path, app_id="hookwatch")
    assert cwd == app

    sandbox = EphemeralSandbox(Settings(), task_id="crun_verify_cwd")
    sandbox.path = tmp_path
    result = sandbox.run(
        "python3 -c 'import pathlib; print(pathlib.Path.cwd().name)'",
        cwd=str(cwd),
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "hookwatch"


def test_vite_scaffold_declares_verify_so_official_apps_are_not_root_gated():
    from boss_engine.services.scaffold_service import scaffold_project

    pkg = json.loads(scaffold_project(name="hookwatch", kind="vite")["files"]["package.json"])
    assert "verify" in pkg["scripts"]
    assert "npm run typecheck" in pkg["scripts"]["verify"]


def test_guest_verify_cwd_prefers_app_package_json(tmp_path: Path):
    import importlib.util

    guest = Path(__file__).resolve().parents[2] / "sandbox-host" / "guest" / "in_guest_agent.py"
    spec = importlib.util.spec_from_file_location("in_guest_agent", guest)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    (tmp_path / "package.json").write_text('{"name":"mono"}', encoding="utf-8")
    app = tmp_path / "approveit"
    app.mkdir()
    (app / "package.json").write_text('{"name":"approveit"}', encoding="utf-8")
    assert mod._verify_cwd({"app_root": "approveit"}, work=tmp_path) == app
    assert mod._verify_cwd({}, work=tmp_path) == tmp_path
