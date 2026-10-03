"""A repository declares how it is verified; the code-task gate runs what a change touches.

The gate used to have two targets: `unittest discover` for anything Python and the scaffold's
npm chain for anything web. BOSS's own tree is neither — ~420 engine pytest files, ~250 console
vitest files, a pnpm workspace with no root build — so a one-line engine fix could not be
verified honestly at all. `boss.verify.yaml` at the repo root names the suites, the paths each
covers and how to pick the affected tests; these tests hold the gate to it.
"""
from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import verification_target as vt
from boss_engine.services import verify_manifest as vm
from boss_engine.services.sandbox import assert_allowed_executable, looks_like_shell

from ._source import code_only

REPO = Path(__file__).resolve().parents[2]

MANIFEST = """
version: 1
setup: ["python3 -m pip --version"]
suites:
  - name: engine
    paths: ["engine/**"]
    run: ["python3 -m pytest -q {tests}"]
    select: {tests_glob: "engine/tests/test_*.py", by: imports}
  - name: web
    paths: ["apps/web/**"]
    cwd: apps/web
    setup: ["pnpm install --frozen-lockfile"]
    run: ["npx vitest run {tests}"]
    select: {tests_glob: "apps/web/tests/**/*.test.*", by: imports}
timeout_seconds: 300
"""


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, "boss.verify.yaml", MANIFEST)
    _write(tmp_path, "engine/boss_engine/services/alpha.py", "X = 1\n")
    _write(tmp_path, "engine/boss_engine/services/beta.py", "Y = 2\n")
    _write(tmp_path, "engine/tests/test_alpha.py",
           "from boss_engine.services.alpha import X\n")
    _write(tmp_path, "engine/tests/test_alpha_by_package.py",
           "from boss_engine.services import alpha\n")
    _write(tmp_path, "engine/tests/test_beta.py", "import boss_engine.services.beta\n")
    _write(tmp_path, "engine/tests/test_unrelated.py", "import yaml\n")
    _write(tmp_path, "apps/web/package.json", "{}")
    _write(tmp_path, "apps/web/app/deploy-ui.tsx", "export const A = 1;\n")
    _write(tmp_path, "apps/web/lib/runs.ts", "export const B = 1;\n")
    _write(tmp_path, "apps/web/tests/deploy.test.tsx",
           'import { A } from "../app/deploy-ui";\n')
    _write(tmp_path, "apps/web/tests/reads-source.test.ts",
           'const src = codeOnly("lib/runs.ts");\n')
    _write(tmp_path, "apps/web/tests/nested/other.test.ts", 'import x from "react";\n')
    return tmp_path


def test_an_engine_change_runs_only_the_engine_tests_that_import_it(repo):
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                          ["engine/boss_engine/services/alpha.py"])

    assert [s["name"] for s in plan.suites] == ["engine"]
    assert plan.steps == [
        ("python3 -m pip --version", "."),
        ("python3 -m pytest -q engine/tests/test_alpha.py "
         "engine/tests/test_alpha_by_package.py", "."),
    ]


def test_a_console_change_runs_its_tests_from_the_package_with_relative_paths(repo):
    """vitest run inside apps/web filters by the path it sees; a root-relative path matches
    nothing and the runner exits "No test files found"."""
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, ["apps/web/lib/runs.ts"])

    assert plan.steps[-2:] == [
        ("pnpm install --frozen-lockfile", "apps/web"),
        ("npx vitest run tests/reads-source.test.ts", "apps/web"),
    ]


def test_an_imported_component_selects_the_test_that_imports_it(repo):
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, ["apps/web/app/deploy-ui.tsx"])

    assert plan.commands[-1] == "npx vitest run tests/deploy.test.tsx"


def test_a_shared_test_helper_selects_the_tests_that_use_it(repo):
    _write(repo, "engine/tests/_source.py", "def code_only(x): return x\n")
    _write(repo, "engine/tests/test_reads_source.py", "from ._source import code_only\n")

    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, ["engine/tests/_source.py"])

    assert plan.commands[-1] == "python3 -m pytest -q engine/tests/test_reads_source.py"


def test_a_changed_test_file_always_runs(repo):
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, ["engine/tests/test_unrelated.py"])

    assert plan.commands[-1] == "python3 -m pytest -q engine/tests/test_unrelated.py"


def test_a_change_no_suite_covers_runs_nothing_and_says_so(repo):
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, ["README.md"])

    assert plan.steps == []
    assert "no suite covers" in plan.describe()


def test_a_touched_suite_with_no_referencing_test_falls_back_only_when_it_says_so(repo):
    _write(repo, "engine/boss_engine/services/gamma.py")
    skip = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                          ["engine/boss_engine/services/gamma.py"])
    assert skip.steps == []
    assert skip.uncovered == ["engine/boss_engine/services/gamma.py"]

    text = MANIFEST.replace("by: imports}", "by: imports, fallback: all}", 1)
    _write(repo, "boss.verify.yaml", text)
    whole = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                           ["engine/boss_engine/services/gamma.py"])
    assert whole.commands[-1].count("engine/tests/test_") == 4


# ------------------------------------------------- zero tests is no evidence, not a pass
def test_a_docs_only_change_passes_with_nothing_to_run(repo):
    _write(repo, "docs/guide/setup.md")
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                          ["README.md", "docs/guide/setup.md"])

    assert plan.steps == [] and plan.uncovered == []


def test_changed_code_outside_every_suite_is_reported_uncovered(repo):
    _write(repo, "apps/api/app/main.py", "app = None\n")

    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, ["apps/api/app/main.py"])

    assert plan.steps == []
    assert plan.uncovered == ["apps/api/app/main.py"]
    assert "no test covers apps/api/app/main.py" in plan.describe()


def test_a_module_no_test_imports_is_uncovered_even_when_its_suite_runs(repo):
    """A planted bug in an unselected module must not ride on its neighbours' green tests."""
    _write(repo, "engine/boss_engine/services/gamma.py")

    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                          ["engine/boss_engine/services/alpha.py",
                           "engine/boss_engine/services/gamma.py"])

    assert "engine/tests/test_alpha.py" in plan.commands[-1]
    assert plan.uncovered == ["engine/boss_engine/services/gamma.py"]


def test_a_deleted_file_needs_no_test_of_its_own(repo):
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                          ["engine/boss_engine/services/removed.py"])

    assert plan.uncovered == []


def test_a_workspace_package_import_selects_the_tests_of_a_change_to_that_package(repo):
    text = MANIFEST.replace('paths: ["apps/web/**"]',
                            'paths: ["apps/web/**", "packages/api-client/**"]')
    _write(repo, "boss.verify.yaml", text)
    _write(repo, "packages/api-client/package.json", '{"name": "@boss/api-client"}')
    _write(repo, "packages/api-client/src/community.ts", "export const c = 1;\n")
    _write(repo, "apps/web/tests/client.test.ts",
           'import { c } from "@boss/api-client";\n')
    _write(repo, "apps/web/tests/sub.test.ts",
           'import type { X } from "@boss/api-client/src/community";\n')

    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo,
                          ["packages/api-client/src/community.ts"])

    assert plan.commands[-1] == "npx vitest run tests/client.test.ts tests/sub.test.ts"
    assert plan.uncovered == []


def test_the_gate_fails_a_plan_that_leaves_changed_code_uncovered():
    from boss_engine.services import governed_code_task_service as g

    src = " ".join(code_only(g).split())
    assert "if result.ok and plan.uncovered:" in src
    assert "exit_code=1, duration_ms=0, timed_out=False, stdout=\"\", stderr=message" in src


# ------------------------------------------- the gate verifies with the base commit's manifest
def test_a_run_that_edits_the_verify_file_still_verifies_with_the_base_manifest(repo):
    pinned = vt.pin_verify_manifest(repo)
    _write(repo, "boss.verify.yaml", MANIFEST.replace('paths: ["engine/**"]',
                                                      'paths: ["nothing/**"]'))

    plan = vt.resolve_gate_plan({}, "t", repo, pinned=pinned,
                                changed=["engine/boss_engine/services/alpha.py"])

    assert [s["name"] for s in plan.suites] == ["engine"]
    assert "engine/tests/test_alpha.py" in plan.commands[-1]


@pytest.mark.parametrize("change, word", [
    (lambda root: _write(root, "boss.verify.yaml", "version: 1\nsuites: []\n"), "modified"),
    (lambda root: (root / "boss.verify.yaml").unlink(), "deleted"),
])
def test_changing_the_verify_file_in_a_run_is_tampering(repo, change, word):
    pinned = vt.pin_verify_manifest(repo)
    assert vt.verify_manifest_tampered(pinned, repo) is None

    change(repo)

    reason = vt.verify_manifest_tampered(pinned, repo)
    assert reason and word in reason and "boss.verify.yaml" in reason


def test_adding_a_verify_file_in_a_run_is_tampering(tmp_path):
    pinned = vt.pin_verify_manifest(tmp_path)
    _write(tmp_path, "boss.verify.yaml", MANIFEST)

    assert "created" in vt.verify_manifest_tampered(pinned, tmp_path)


def test_the_gate_pins_the_manifest_before_any_agent_and_refuses_a_changed_one():
    from boss_engine.services import governed_code_task_service as g

    src = " ".join(code_only(g).split())
    pin = src.index("_pinned_manifest = verification_target.pin_verify_manifest(root)")
    # The pin precedes every agent stage of the run.
    assert pin < src.index("a.run_role(")
    assert "verify_manifest_tampered( _pinned_manifest, root)" in src
    assert "manifest_display(root, _pinned_manifest)" in src


def test_unknown_changed_files_run_every_suite_whole(repo):
    plan = vm.plan_verify(vm.load_verify_manifest(repo), repo, None)

    assert {s["name"] for s in plan.suites} == {"engine", "web"}
    assert "tests/nested/other.test.ts" in plan.commands[-1]


@pytest.mark.parametrize("bad, fragment", [
    ('run: ["python3 -m pytest {tests} && echo ok"]', "shell syntax"),
    ('run: []', "at least one command"),
    ('run: ["python3 -m pytest {tests}"]\n    cwd: ../elsewhere', "inside the repository"),
])
def test_a_manifest_the_gate_cannot_honour_is_refused_with_the_reason(repo, bad, fragment):
    text = MANIFEST.replace('run: ["python3 -m pytest -q {tests}"]', bad, 1)
    with pytest.raises(vm.VerifyManifestError) as err:
        vm.parse_verify_manifest(text)
    assert fragment in str(err.value)


def test_explicit_task_commands_still_win_over_the_manifest(repo):
    plan = vt.resolve_gate_plan({"test_commands": ["make check"]}, "t", repo,
                                changed=["engine/boss_engine/services/alpha.py"])

    assert (plan.source, plan.commands) == ("task", ["make check"])


def test_the_gate_uses_the_manifest_from_the_repo_root(repo):
    plan = vt.resolve_gate_plan({}, "Fix alpha", repo,
                                changed=["engine/boss_engine/services/alpha.py"])

    assert plan.source == "manifest"
    assert plan.cwd == repo
    assert plan.timeout_seconds == 300
    assert plan.steps[-1][1] == repo


def test_a_broken_manifest_fails_the_gate_closed(repo):
    _write(repo, "boss.verify.yaml", "version: 2\n")

    plan = vt.resolve_gate_plan({}, "t", repo, changed=["engine/x.py"])

    assert plan.source == "manifest_error"
    assert plan.commands == [] and "version must be 1" in plan.error


def test_a_pytest_project_defaults_to_pytest(tmp_path):
    _write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\n")

    plan = vt.resolve_gate_plan({"project_kind": "python"}, "lib", tmp_path)

    assert plan.commands == [vt.PYTEST_CMD]


def test_a_conftest_alone_makes_it_a_pytest_project(tmp_path):
    _write(tmp_path, "tests/conftest.py")

    assert vt.resolve_gate_plan({"project_kind": "python"}, "lib", tmp_path).commands == [
        vt.PYTEST_CMD]


def test_a_python_repo_without_pytest_keeps_unittest(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")

    assert vt.resolve_gate_plan({"project_kind": "python"}, "lib", tmp_path).commands == [
        vt.PYTHON_TEST_CMD]


def test_a_web_default_verifies_in_the_package_the_change_is_in(tmp_path):
    _write(tmp_path, "apps/site/package.json", "{}")

    plan = vt.resolve_gate_plan({"project_kind": "web"}, "site", tmp_path,
                                changed=["apps/site/src/App.tsx"])

    assert plan.cwd == tmp_path / "apps/site"


def test_a_python_default_stays_at_the_root_whatever_changed(tmp_path):
    _write(tmp_path, "tools/ui/package.json", "{}")

    plan = vt.resolve_gate_plan({"project_kind": "python"}, "lib", tmp_path,
                                changed=["tools/ui/x.js"])

    assert plan.cwd == tmp_path


def test_the_governed_gate_scopes_every_verify_by_the_runs_changed_files():
    from boss_engine.services import governed_code_task_service as g

    src = " ".join(code_only(g).split())
    assert ("resolve_gate_plan( spec, title, root, app_id=_app_id, changed=_changed_paths(), "
            "pinned=_pinned_manifest)" in src)
    assert "if plan.from_manifest: return _run_manifest_verify(plan, record=record)" in src
    assert "run_until_failure(_CwdSandbox(), plan.commands)" in src
    # A manifest-declared repo is not a scaffolded web app: its root package.json is a
    # workspace, and the scaffold's package contract would fail it for not being one.
    assert ") and not _gate_plan0.from_manifest" in src


# --------------------------------------------------------------- BOSS's own declaration
def _boss_manifest() -> vm.VerifyManifest:
    manifest = vm.load_verify_manifest(REPO)
    assert manifest is not None, "BOSS must declare boss.verify.yaml at its root"
    return manifest


def test_boss_declares_its_engine_and_console_suites_but_not_the_postgres_ones():
    manifest = _boss_manifest()

    names = {s.name for s in manifest.suites}
    assert names == {"engine", "web"}
    globs = [p for s in manifest.suites for p in s.paths]
    assert not any(g.startswith("apps/api") for g in globs)


def test_every_command_boss_declares_is_one_the_sandbox_will_run():
    allowed = Settings.model_fields["sandbox_validation_allowed_commands"].default
    settings = types.SimpleNamespace(sandbox_validation_allowed_commands=allowed)
    manifest = _boss_manifest()
    commands = [*manifest.setup, *(c for s in manifest.suites for c in (*s.setup, *s.run))]

    for command in commands:
        concrete = command.replace(vm.TESTS_PLACEHOLDER, "tests/x.py")
        assert not looks_like_shell(concrete), command
        assert_allowed_executable(concrete.split()[0], settings)


def test_a_change_to_the_verify_target_selects_its_own_tests_in_boss():
    plan = vm.plan_verify(_boss_manifest(), REPO,
                          ["engine/boss_engine/services/verification_target.py"])

    assert [s["name"] for s in plan.suites] == ["engine"]
    assert "engine/tests/test_verification_target.py" in plan.commands[-1]
    assert "engine/tests/test_a_monorepo_verifies_only_the_suites_its_change_touches.py" in (
        plan.commands[-1])


def test_a_change_to_the_api_client_selects_the_console_tests_that_import_it_in_boss():
    plan = vm.plan_verify(_boss_manifest(), REPO, ["packages/api-client/src/community.ts"])

    assert [s["name"] for s in plan.suites] == ["web"]
    for test in ("tests/api-client-community.test.ts", "tests/infrastructure.test.ts",
                 "tests/mission-operations.test.tsx"):
        assert test in plan.commands[-1]
    assert plan.uncovered == []


def test_an_api_change_in_boss_is_uncovered_not_passed():
    plan = vm.plan_verify(_boss_manifest(), REPO, ["apps/api/app/main.py"])

    assert plan.steps == []
    assert plan.uncovered == ["apps/api/app/main.py"]


def _runtime_stage_executables() -> set[str]:
    """Executables the API/worker image (Dockerfile.community's python-runtime stage, where the
    code-task gate runs) puts on PATH: the python base image's, the symlinks it creates, and the
    bins of the npm packages it installs globally."""
    text = (REPO / "Dockerfile.community").read_text(encoding="utf-8")
    stage = text.split(" AS python-runtime", 1)[1].split("\nFROM ", 1)[0]
    provided = {"python", "python3", "pip", "pip3", "node"}
    provided.update(re.findall(r"ln -s \S+ /usr/local/bin/([\w.-]+)", stage))
    joined = stage.replace("\\\n", " ")
    for line in re.findall(r"npm install (?:-g|--global) ([^&\n]+)", joined):
        for package in line.split():
            name = package.rsplit("@", 1)[0] if package.count("@") > (package[0] == "@") else package
            if not name.startswith("@"):
                provided.add(name)
    return provided


def test_every_command_boss_declares_exists_where_the_gate_runs_it():
    """The allowlist says the sandbox MAY run pnpm; only the image says it CAN. A setup step
    whose executable is missing fails every console change as a test failure."""
    manifest = _boss_manifest()
    commands = [*manifest.setup, *(c for s in manifest.suites for c in (*s.setup, *s.run))]
    provided = _runtime_stage_executables()

    for command in commands:
        assert command.split()[0] in provided, command
