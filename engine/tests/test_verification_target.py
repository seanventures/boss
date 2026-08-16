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


def test_harden_web_package_adds_missing_runners_and_unstubs(tmp_path: Path):
    from boss_engine.services.verification_target import harden_web_package

    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": 'echo "typecheck passed"',
                    "build": "echo \"build step skipped\"",
                    "test:e2e": "playwright test",
                    "verify": "npm run typecheck && npm run build && npm run test:e2e",
                },
                "devDependencies": {"eslint": "6.4.0"},
            }
        ),
        encoding="utf-8",
    )
    result = harden_web_package(tmp_path)
    assert "add:vite" in result["changed"]
    assert "add:@playwright/test" in result["changed"]
    assert "add:eslint-plugin-react" in result["changed"]
    assert "upgrade:eslint" in result["changed"]
    assert "pin:playwright" in result["changed"]
    assert "unstub:typecheck" in result["changed"]
    assert "unstub:build" in result["changed"]
    scripts = json.loads(pkg.read_text(encoding="utf-8"))
    assert scripts["scripts"]["typecheck"] == "tsc --noEmit"
    assert "npx --package=vite vite" in scripts["scripts"]["build"]
    assert scripts["scripts"]["test:e2e"] == (
        "npx --package=@playwright/test playwright test"
    )
    assert scripts["devDependencies"]["eslint"].startswith("^8")
    assert "vite" in scripts["devDependencies"]
    assert "@playwright/test" in scripts["devDependencies"]


def test_harden_web_package_pins_bare_vite_build(tmp_path: Path):
    from boss_engine.services.verification_target import harden_web_package

    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps({"scripts": {"build": "tsc --noEmit && vite build"}}),
        encoding="utf-8",
    )
    harden_web_package(tmp_path)
    scripts = json.loads(pkg.read_text(encoding="utf-8"))["scripts"]
    assert scripts["build"] == "tsc --noEmit && npx --package=vite vite build"


def test_web_package_contract_fails_closed_on_echo_stubs(tmp_path: Path):
    from boss_engine.services.verification_target import (
        evaluate_web_package_contract,
        harden_web_package,
    )

    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "scripts": {"typecheck": 'echo "ok"', "verify": "npm test"},
                "devDependencies": {},
            }
        ),
        encoding="utf-8",
    )
    before = evaluate_web_package_contract(tmp_path)
    assert before["ok"] is False
    assert "typecheck" in before["stubs"]
    assert "vite" in before["missing_runners"]
    harden_web_package(tmp_path)
    after = evaluate_web_package_contract(tmp_path)
    assert after["ok"] is True
    assert after["stubs"] == []
    assert after["missing_runners"] == []


def test_prepare_web_verify_env_names_npm_install(tmp_path: Path):
    from boss_engine.services.verification_target import (
        NPM_INSTALL_CMD,
        prepare_web_verify_env,
    )

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    prep = prepare_web_verify_env(tmp_path)
    assert prep["install_command"] == NPM_INSTALL_CMD
    assert "add:vite" in prep["changed"]


def test_harden_unstubs_exit_zero_and_swallowed_playwright(tmp_path: Path):
    from boss_engine.services.verification_target import (
        evaluate_web_package_contract,
        harden_web_package,
    )

    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "scripts": {
                    "verify": "exit 0",
                    "test:e2e": "npx --package=@playwright/test playwright test || true",
                },
                "devDependencies": {"vite": "^5.3.1", "@playwright/test": "^1.45.0"},
            }
        ),
        encoding="utf-8",
    )
    before = evaluate_web_package_contract(tmp_path)
    assert before["ok"] is False
    assert "verify" in before["stubs"]
    assert "test:e2e" in before["stubs"]
    result = harden_web_package(tmp_path)
    assert "unstub:verify" in result["changed"]
    assert "unstub:test:e2e" in result["changed"]
    after = evaluate_web_package_contract(tmp_path)
    assert after["ok"] is True
    scripts = json.loads(pkg.read_text(encoding="utf-8"))["scripts"]
    assert "|| true" not in scripts["test:e2e"]
    assert "exit 0" not in scripts["verify"]
    assert "npm run" in scripts["verify"]


def test_seed_acceptance_e2e_writes_named_specs_once(tmp_path: Path):
    from boss_engine.services.verification_target import seed_acceptance_e2e

    first = seed_acceptance_e2e(
        tmp_path,
        ["Create an issue and see it on the dashboard.", "Delete it."],
    )
    assert first["seeded_specs"] == 2
    spec = (tmp_path / "e2e" / "acceptance.spec.ts").read_text(encoding="utf-8")
    assert "Create an issue and see it on the dashboard." in spec
    assert (tmp_path / "playwright.config.ts").is_file()
    second = seed_acceptance_e2e(tmp_path, ["Create an issue and see it on the dashboard."])
    assert second["seeded_specs"] == 0
    assert second.get("reason") == "specs_exist"


def test_honest_verify_status_rejects_swallowed_webserver_failure():
    from boss_engine.services.verification_target import (
        honest_verify_status,
        verify_output_disproves_pass,
    )

    output = "Error: Process from config.webServer was not able to start. Exit code: 1"
    assert verify_output_disproves_pass(output) is True
    assert honest_verify_status({"command": "npm run verify", "exit_code": 0}, output) == "fail"
    assert honest_verify_status({"command": "npm run verify", "exit_code": 0}, "  ✓ create") == "pass"
    assert honest_verify_status(None, "") == "not_recorded"


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
