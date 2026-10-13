"""BOSS's web gate runs what the app defines, and a blocked build names the check that failed.

onboarding_path_builder (crun_decc93dbb0154e768871) is a plain Node ESM app with no build step and
16 tests under `node --test`. BOSS gated it on `npm install && npm run build && npm run typecheck
&& npm run lint`: npm said `Missing script: "build"`, `npm test` was never in the gate, the agent
bolted a vite build onto the app to please it, and the run ended "Blocked: the template's tests did
not pass" although the template never ran. Its agent had also picked better-sqlite3, whose install
needs make, which the sandbox lacks. Each part of the class is pinned here:

  * the default web gate is composed from package.json: build, typecheck and lint each only where
    defined, `npm test` always where defined, the browser suite as the pass decides;
  * an app with no test script at all is a finding the gate fails on, not a crash;
  * a brief's test_commands still win;
  * the blocked line names the failing check and its first line, never the template's tests;
  * the writing stages are told the gate's rule and that the sandbox has no C/C++ compiler.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from boss_engine.services import build_scope, code_agent
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import verification_target as vt
from boss_engine.services.sandbox import CommandResult
from boss_engine.templates import gate as template_gate

INSTALL = vt.NPM_INSTALL_CMD


def _app(tmp_path: Path, scripts: dict) -> Path:
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "package.json").write_text(json.dumps({"name": "app", "type": "module",
                                                  "scripts": scripts}))
    return tmp_path


def _plan(root: Path, spec=None, scope=None):
    token = build_scope.set_scope(scope)
    try:
        return vt.resolve_gate_plan(spec or {"project_kind": "web"}, "an app", root,
                                    changed=["app/package.json"])
    finally:
        build_scope._SCOPE.reset(token)


def _result(command: str, code: int = 0, out: str = "", err: str = "") -> CommandResult:
    return CommandResult(command=command, argv=[], exit_code=code, duration_ms=1,
                         timed_out=False, stdout=out, stderr=err)


# ---- the gate is composed from what the app defines -----------------------------------------------

def test_a_plain_node_app_is_gated_on_its_own_tests_and_nothing_it_does_not_define(tmp_path):
    plan = _plan(_app(tmp_path, {"start": "node src/server.js", "test": "node --test"}))
    assert plan.source == "default" and plan.cwd == tmp_path / "app"
    assert plan.commands == [INSTALL, "npm test"]
    assert plan.finding is None


def test_each_defined_check_runs_in_order_and_the_tests_always_do(tmp_path):
    root = _app(tmp_path, {"lint": "eslint .", "test": "vitest run", "build": "tsc",
                           "typecheck": "tsc --noEmit"})
    assert _plan(root).commands == [INSTALL, "npm run build", "npm run typecheck",
                                    "npm run lint", "npm test"]


def test_a_stub_script_is_not_a_check(tmp_path):
    root = _app(tmp_path, {"build": "echo no build step", "test": "node --test"})
    assert _plan(root).commands == [INSTALL, "npm test"]


def test_the_browser_suite_follows_the_pass_and_what_the_app_defines(tmp_path):
    root = _app(tmp_path, {"test": "node --test", "test:e2e": "playwright test"})
    pass1 = build_scope.decide("App", "Node app with Playwright e2e tests")
    assert vt.E2E_STEP not in _plan(root, scope=pass1).commands
    pass2 = build_scope.decide("App", f"{build_scope.MORE_TESTS_MARKER}: add Playwright e2e")
    assert _plan(root, scope=pass2).commands[-1] == vt.E2E_STEP
    assert _plan(root).commands[-1] == vt.E2E_STEP           # outside a build: it is defined
    plain = _app(tmp_path / "plain", {"test": "node --test"})
    assert vt.E2E_STEP not in _plan(plain).commands           # outside a build: not defined
    # A pass 2 that asked for browser tests requires them, defined yet or not.
    assert _plan(plain, scope=pass2).commands[-1] == vt.E2E_STEP


def test_the_briefs_test_commands_still_win(tmp_path):
    root = _app(tmp_path, {"test": "node --test"})
    plan = _plan(root, spec={"project_kind": "web", "test_commands": ["npm run check"]})
    assert plan.source == "task" and plan.commands == ["npm run check"] and plan.finding is None


# ---- an app with no test script is a finding, not a crash -----------------------------------------

def test_no_test_script_is_a_finding_the_gate_fails_on(tmp_path):
    for scripts in ({"build": "tsc"}, {}, {"test": 'echo "Error: no test specified" && exit 1'}):
        plan = _plan(_app(tmp_path / str(len(scripts)), scripts))
        assert plan.finding == vt.NO_TEST_SCRIPT, scripts
        ran: list[str] = []
        results = vt.run_web_gate(lambda c: (ran.append(c), _result(c))[1], plan)
        assert ran == plan.commands
        assert not results[-1].ok and results[-1].stderr == vt.NO_TEST_SCRIPT
        assert vt.checks_headline(vt.failed_check(results[-1])).endswith(vt.NO_TEST_SCRIPT)


def test_an_app_with_only_a_browser_suite_has_tests(tmp_path):
    assert _plan(_app(tmp_path, {"build": "vite build", "test:e2e": "playwright test"})).finding is None


def test_the_gate_stops_at_the_first_failure_and_says_it_first(tmp_path):
    plan = _plan(_app(tmp_path, {"build": "tsc"}))
    ran: list[str] = []

    def run(c):
        ran.append(c)
        return _result(c, 1 if c == "npm run build" else 0, err="error TS2304: Cannot find name")
    results = vt.run_web_gate(run, plan)
    assert ran == [INSTALL, "npm run build"] and results[-1].command == "npm run build"


# ---- the blocked line names the failing check -------------------------------------------------------

MISSING = ('npm error Missing script: "build"\nnpm error\nnpm error To see a list of scripts, run:\n'
           "npm error   npm run\n")


def test_the_failed_check_is_its_command_and_first_error_line():
    check = vt.failed_check(_result("npm run build", 1, err=MISSING))
    assert check == {"command": "npm run build", "line": 'Missing script: "build"'}
    vite = vt.failed_check(_result("npm run build", 1, out=(
        "> app@1.0.0 build\n> vite build\n\x1b[31merror during build:\n"
        '\x1b[31mCould not resolve entry module "index.html".\n    at getRollupError (x.js:1:1)\n')))
    assert vite["line"] == 'error during build: Could not resolve entry module "index.html".'
    assert vt.failed_check(_result("npm test")) is None


def test_a_build_blocked_by_its_own_checks_names_them_not_the_template():
    check = vt.failed_check(_result("npm run build", 1, err=MISSING))
    # The connector's record, exactly as the gate writes it when the build's checks failed.
    ok, tt = template_gate.connector_smoke(None, None, tests_passed=False)
    assert not ok and tt["because"] == "tests"
    held = template_gate.hold_outcome(["template_tests"], None, template_tests=tt,
                                      tests=[{"command": "npm install … && npm run build",
                                              "exit_code": 1, "ok": False,
                                              "failed_check": check}])
    assert held["error"].startswith(
        "Blocked: the app's checks failed: npm run build — Missing script: \"build\". ")
    assert "template's tests did not pass" not in held["error"]
    assert held["next_step"] == template_gate.CHECKS_NEXT
    # Without the record it still names the gate that failed, never the template's tests.
    bare = template_gate.hold_outcome(["template_tests"], None, template_tests=tt)
    assert "the app's checks failed" in bare["error"]
    assert "template's tests did not pass" not in bare["error"]
    # A template whose own tests really failed keeps its words.
    real = template_gate.hold_outcome(["template_tests"], None,
                                      template_tests={"error": "2 of 9 template tests failed"})
    assert "the template's tests did not pass" in real["error"]


def test_the_pipeline_records_the_failing_check_and_the_hold_reads_it():
    src = inspect.getsource(gov)
    assert "_failed_check[0] = verification_target.failed_check(result)" in src
    assert '"failed_check": _failed_check[0]' in src
    assert src.count('tests={"failed_check": _failed_check[0]}') + src.count(
        'tests=({"failed_check": _failed_check[0]}') == 2
    held_run = inspect.getsource(template_gate.held_run)
    assert "q.evidence->'tests' AS tests" in held_run and 'tests=row.get("tests")' in held_run


# ---- the writing stages are told -------------------------------------------------------------------

def test_the_first_pass_checklist_says_how_the_gate_is_composed():
    assert vt.WEB_GATE_RULE in gov.FIRST_PASS_CHECKLIST
    assert "`npm test`" in vt.WEB_GATE_RULE and "only to satisfy the gate" in vt.WEB_GATE_RULE


def test_a_sandbox_without_a_compiler_says_so(monkeypatch):
    import shutil

    real = shutil.which
    for present, said in ((False, True), (True, False)):
        compilers = ("make", "cc", "gcc", "g++")
        monkeypatch.setattr(shutil, "which", lambda name, _p=present, _c=compilers: (
            (f"/usr/bin/{name}" if _p else None) if name in _c else real(name)))
        code_agent._SANDBOX_FACTS.clear()
        try:
            text = code_agent.sandbox_facts()
        finally:
            code_agent._SANDBOX_FACTS.clear()
        assert (code_agent.NO_COMPILER_FACT in text) is said
    assert "better-sqlite3" in code_agent.NO_COMPILER_FACT
    assert "node:sqlite" in code_agent.NO_COMPILER_FACT and "bcryptjs" in code_agent.NO_COMPILER_FACT
