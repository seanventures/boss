"""Unit and integration tests for conditional Playwright browser installs (BOSSAPP-1336)."""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import playwright_cache, stack_verify, verification_target


@pytest.fixture
def pw_cache(tmp_path, monkeypatch):
    root = tmp_path / "toolchain-cache"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("BOSS_TOOLCHAIN_CACHE_ROOT", str(root))
    return root


def _write_pkg(app_dir: Path, data: object) -> Path:
    app_dir.mkdir(parents=True, exist_ok=True)
    pkg = app_dir / "package.json"
    if isinstance(data, str):
        pkg.write_text(data, encoding="utf-8")
    else:
        pkg.write_text(json.dumps(data), encoding="utf-8")
    return app_dir


def test_uses_playwright_returns_false_for_missing_invalid_and_non_playwright_apps(tmp_path):
    assert playwright_cache.uses_playwright(tmp_path / "missing") is False
    assert playwright_cache.uses_playwright(_write_pkg(tmp_path / "badjson", "{not json")) is False
    assert playwright_cache.uses_playwright(_write_pkg(tmp_path / "array", "[]")) is False
    assert playwright_cache.uses_playwright(
        _write_pkg(
            tmp_path / "plain",
            {
                "name": "plain-node",
                "dependencies": {"express": "^4.19.0"},
                "devDependencies": {"vitest": "^2.0.0", "jest": "^29.0.0"},
            },
        )
    ) is False


@pytest.mark.parametrize("section", ["dependencies", "devDependencies", "optionalDependencies"])
@pytest.mark.parametrize("pkg_name", ["@playwright/test", "playwright"])
def test_uses_playwright_detects_playwright_in_any_dependency_section(
    tmp_path, section, pkg_name
):
    app = _write_pkg(tmp_path / "web", {"name": "web", section: {pkg_name: "^1.48.0"}})
    assert playwright_cache.uses_playwright(app) is True


def test_uses_playwright_does_not_walk_up_to_parent_package_json(tmp_path):
    _write_pkg(tmp_path, {"name": "mono", "devDependencies": {"@playwright/test": "^1.48.0"}})
    child = _write_pkg(tmp_path / "packages" / "api", {"name": "api", "dependencies": {"fastify": "^4.0.0"}})
    assert playwright_cache.uses_playwright(child) is False


def test_stack_verify_skips_playwright_ensure_when_package_json_has_no_playwright(
    tmp_path, pw_cache, monkeypatch
):
    app = _write_pkg(tmp_path / "plain", {"name": "plain", "scripts": {"test:e2e": "node e2e.js"}})
    ensure_calls: list[object] = []

    def fake_ensure(version, **kwargs):
        ensure_calls.append(version)
        return {"ok": True, "cached": True}

    monkeypatch.setattr(playwright_cache, "ensure", fake_ensure)
    plan = SimpleNamespace(
        e2e_steps=[(verification_target.PLAYWRIGHT_INSTALL_CMD, app), ("npm run test:e2e", app)]
    )
    record: dict = {}
    covered = stack_verify._ensure_browsers(plan, record)
    assert covered == {(verification_target.PLAYWRIGHT_INSTALL_CMD, str(app))}
    assert ensure_calls == []
    assert "browsers" not in record


@pytest.mark.parametrize("pkg_name", ["@playwright/test", "playwright"])
def test_stack_verify_runs_playwright_ensure_when_package_json_declares_playwright(
    tmp_path, pw_cache, monkeypatch, pkg_name
):
    app = _write_pkg(
        tmp_path / "web",
        {"name": "web", "devDependencies": {pkg_name: "^1.48.0"}},
    )
    nm = app / "node_modules" / pkg_name
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(
        json.dumps({"name": pkg_name, "version": "1.48.2"}), encoding="utf-8"
    )
    ensure_calls: list[object] = []

    def fake_ensure(version, **kwargs):
        ensure_calls.append(version)
        return {"ok": True, "cached": True, "seconds": 0.0, "detail": "cached"}

    monkeypatch.setattr(playwright_cache, "ensure", fake_ensure)
    plan = SimpleNamespace(
        e2e_steps=[(verification_target.PLAYWRIGHT_INSTALL_CMD, app), ("npm run test:e2e", app)]
    )
    record: dict = {}
    covered = stack_verify._ensure_browsers(plan, record)
    assert covered == {(verification_target.PLAYWRIGHT_INSTALL_CMD, str(app))}
    assert ensure_calls == ["1.48.2"]
    assert record["browsers"]["ok"] is True


def _compile_run_verify_once(ns: dict):
    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    tree = ast.parse(src)
    fn_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_verify_once"
    )
    # Remove `nonlocal` so the extracted function can bind module globals in `ns`.
    fn_node.body = [
        stmt for stmt in fn_node.body if not isinstance(stmt, ast.Nonlocal)
    ]
    mod = ast.Module(body=[fn_node], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, inspect.getsourcefile(g) or "<governed>", "exec"), ns)
    return ns["_run_verify_once"]


@pytest.mark.parametrize(
    ("deps", "expect_pw_call"),
    [
        ({}, False),
        ({"devDependencies": {"@playwright/test": "^1.48.0"}}, True),
        ({"dependencies": {"playwright": "^1.48.0"}}, True),
    ],
)
def test_governed_verify_once_guards_playwright_install_by_package_json(
    tmp_path, monkeypatch, deps, expect_pw_call
):
    from boss_engine.services.sandbox import CommandResult

    app = _write_pkg(
        tmp_path / "app",
        {"name": "app", "scripts": {"test": "node --test"}, **deps},
    )
    sandbox_cmds: list[str] = []
    ensure_calls: list[object] = []

    def fake_ensure(version, **kwargs):
        ensure_calls.append(version)
        return {"ok": False, "cached": False, "seconds": 0.0, "detail": "miss"}

    monkeypatch.setattr(playwright_cache, "ensure", fake_ensure)

    class _Sandbox:
        def run(self, command, cwd=None, timeout=None):
            sandbox_cmds.append(str(command))
            return CommandResult(
                command=str(command),
                argv=[],
                exit_code=0,
                duration_ms=1,
                timed_out=False,
                stdout="ok",
                stderr="",
            )

    class _Vt:
        PLAYWRIGHT_INSTALL_CMD = verification_target.PLAYWRIGHT_INSTALL_CMD

        @staticmethod
        def verify_manifest_tampered(pinned, root):
            return None

        @staticmethod
        def resolve_gate_plan(*args, **kwargs):
            return SimpleNamespace(
                runs_steps=False,
                commands=["npm test"],
                description="npm test",
                cwd=app,
                stack=None,
                source="node",
                error=None,
            )

        @staticmethod
        def prepare_web_verify_env(*args, **kwargs):
            return {"changed": [], "restored": {"restored": False}}

        @staticmethod
        def run_web_gate(runner, plan):
            return [runner("npm test")]

        @staticmethod
        def verify_log(note, results, error=None):
            return "ok"

        @staticmethod
        def verify_output_disproves_pass(output):
            return False

    def _boss_install(app_cwd, prep):
        sandbox_cmds.append("npm install")
        return CommandResult(
            command="npm install",
            argv=[],
            exit_code=0,
            duration_ms=1,
            timed_out=False,
            stdout="",
            stderr="",
        )

    ns = {
        "__name__": "boss_engine.services.governed_code_task_service",
        "__package__": "boss_engine.services",
        "Path": Path,
        "verification_target": _Vt,
        "sandbox": _Sandbox(),
        "_pinned_manifest": None,
        "root": app,
        "spec": {},
        "title": "t",
        "_app_id": None,
        "_changed_paths": lambda: [],
        "_stack_layout": None,
        "_lineage_paths": [],
        "_recorded_app": None,
        "fctx": {},
        "_halt_if_needs_person": lambda plan: None,
        "_last_gate_plan": [None],
        "_last_stack_plan": [None],
        "last_verify_command": "npm test",
        "last_verify_cwd": ".",
        "last_verify_output": "",
        "test_cmd": "npm test",
        "_prepare_app": lambda plan, cwd: (
            app,
            {"browser_install_command": verification_target.PLAYWRIGHT_INSTALL_CMD},
            "",
        ),
        "_install_timeout": lambda: 300,
        "_boss_install": _boss_install,
        "_emit_cmd": lambda label, res: None,
        "_log_caught": lambda lg: None,
        "logger": None,
    }
    run_verify_once = _compile_run_verify_once(ns)
    res = run_verify_once(record=True)
    assert res.exit_code == 0
    assert "npm install" in sandbox_cmds
    assert "npm test" in sandbox_cmds
    if expect_pw_call:
        assert len(ensure_calls) == 1
        assert verification_target.PLAYWRIGHT_INSTALL_CMD in sandbox_cmds
    else:
        assert ensure_calls == []
        assert verification_target.PLAYWRIGHT_INSTALL_CMD not in sandbox_cmds
