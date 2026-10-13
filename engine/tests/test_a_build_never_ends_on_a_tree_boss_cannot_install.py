"""A build never hands off a workspace BOSS's own install refuses, and when one ends that way it says so.

HelpDesk AI (crun_6eaa478dc33a42c6bdb7) blocked after 21 min with "the template's tests did not pass".
The connector's tests passed; what failed was BOSS's `npm install --include=dev ...`, three times in
0.7 s, with ERESOLVE: eslint ^9 beside eslint-plugin-react-hooks ^4.6.2 (peer eslint <= 8). BOSS's
own package.json hardening had added that plugin before every install; the agent's fix round removed
it and BOSS put it back. The fix round was also told "the tests are FROZEN ... fix ONLY the
implementation" under a `$ npm run verify` header. Each part of the class is pinned here:

  * hardening never adds an eslint-8 plugin next to an app's eslint 9+;
  * BOSS's install is the same command in the verify and the template gate, with no
    --legacy-peer-deps / --force fallback anywhere;
  * before any gate, and after every fix round, BOSS runs its exact install and sends a failure
    back to the same warm session, again and again until it passes (the retry,
    crun_1fdfea1c9c904a8083dd, fixed a second conflict only after BOSS's last check);
  * a failed install reaches the warm session as an install failure, with npm's error and one
    instruction, in the implement loop and in a convergence round;
  * pass 1's gate does not run the browser suite (the retry's gate still ran test:e2e);
  * a run that ends on it says "dependencies did not install: ERESOLVE ...";
  * every writing stage, and the implementer's hand-off checklist, is told up front.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

from boss_engine.services import build_scope, code_agent
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import verification_target as vt
from boss_engine.services.sandbox import CommandResult
from boss_engine.templates import gate

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"

ERESOLVE = """npm error code ERESOLVE
npm error ERESOLVE unable to resolve dependency tree
npm error
npm error While resolving: helpdesk-ai@1.0.0
npm error Found: eslint@9.39.5
npm error node_modules/eslint
npm error   dev eslint@"^9.12.0" from the root project
npm error
npm error Could not resolve dependency:
npm error peer eslint@"^3.0.0 || ^4.0.0 || ^5.0.0 || ^6.0.0 || ^7.0.0 || ^8.0.0-0" from eslint-plugin-react-hooks@4.6.2
npm error node_modules/eslint-plugin-react-hooks
npm error   dev eslint-plugin-react-hooks@"^4.6.2" from the root project
npm error
npm error Fix the upstream dependency conflict, or retry
npm error this command with --force or --legacy-peer-deps
npm error to accept an incorrect (and potentially broken) dependency resolution.
"""


def _install_result(exit_code=1, stderr=ERESOLVE):
    return CommandResult(command=vt.NPM_INSTALL_CMD, argv=[], exit_code=exit_code, duration_ms=700,
                         timed_out=False, stdout="", stderr=stderr)


def _harden(tmp_path, dev):
    (tmp_path / "package.json").write_text(json.dumps({"name": "a", "devDependencies": dev}))
    vt.harden_web_package(tmp_path)
    return json.loads((tmp_path / "package.json").read_text())["devDependencies"]


# ---- BOSS's hardening never makes the conflict --------------------------------------------------

def test_hardening_adds_no_eslint_8_plugin_next_to_eslint_9(tmp_path):
    dev = _harden(tmp_path, {"eslint": "^9.12.0", "typescript-eslint": "^8.8.0"})
    assert dev["eslint"] == "^9.12.0"
    assert not (vt.ESLINT_COMPANIONS & set(dev)), dev
    # The build tools it owes every web app are still added.
    assert {"vite", "typescript", "@playwright/test"} <= set(dev)


def test_hardening_keeps_the_eslint_8_set_together(tmp_path):
    for declared in ({}, {"eslint": "^8.57.0"}, {"eslint": "^6.0.0"}):
        dev = _harden(tmp_path, dict(declared))
        assert vt._semver_major(dev["eslint"]) == 8
        assert vt.ESLINT_COMPANIONS <= set(dev)
        assert dev["eslint-plugin-react-hooks"].startswith("^4")


# ---- one install, no fallback -------------------------------------------------------------------

def test_the_template_gate_runs_the_install_the_verify_runs(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert gate.install_command("node", tmp_path) == vt.NPM_INSTALL_CMD
    assert vt.WEB_TEST_COMMANDS[0] == vt.NPM_INSTALL_CMD


def test_no_install_boss_runs_hides_a_broken_tree():
    """No --legacy-peer-deps / --force on any install command BOSS issues (it would hide the
    broken tree the app's own `pretest` install and its deploy then trip on)."""
    bad = re.compile(r"""["'](?:npm|pnpm)\s+(?:install|i|ci)\b[^"'\n]*--(?:legacy-peer-deps|force)""")
    hits = [f"{p.relative_to(ENGINE)}" for p in ENGINE.rglob("*.py") if bad.search(p.read_text())]
    assert hits == []
    assert "--legacy-peer-deps" not in vt.NPM_INSTALL_CMD and "--force" not in vt.NPM_INSTALL_CMD


# ---- a failed install is named as one -----------------------------------------------------------

def test_a_failed_install_is_recognised_and_summarised_in_npms_words():
    failure = vt.install_failure(_install_result())
    assert failure["command"] == vt.NPM_INSTALL_CMD and failure["exit_code"] == 1
    s = failure["summary"]
    assert s.startswith("ERESOLVE unable to resolve dependency tree")
    assert "eslint@9.39.5" in s and "eslint-plugin-react-hooks@4.6.2" in s
    assert "--legacy-peer-deps" not in s  # npm's own advice is not repeated as ours


def test_only_a_failed_install_counts():
    assert vt.install_failure(_install_result(exit_code=0, stderr="")) is None
    failed_build = CommandResult(command="npm run build", argv=[], exit_code=2, duration_ms=1,
                                 timed_out=False, stdout="", stderr="TS2304")
    assert vt.install_failure(failed_build) is None
    for cmd in ("npm ci --no-audit", "pnpm install", "python3 -m pip install -r requirements.txt"):
        assert vt.is_install_command(cmd)
    assert not vt.is_install_command("npm run test:e2e")


def test_the_fix_round_gets_the_error_and_one_instruction():
    evidence = vt.install_repair_evidence(vt.install_failure(_install_result()), ERESOLVE)
    assert "dependency install failed" in evidence and vt.NPM_INSTALL_CMD in evidence
    assert "eslint-plugin-react-hooks@4.6.2" in evidence
    assert vt.INSTALL_REPAIR_INSTRUCTION in evidence
    assert "do not use --force or --legacy-peer-deps" in vt.INSTALL_REPAIR_INSTRUCTION


def test_the_pipeline_feeds_an_install_failure_back_to_the_warm_session():
    src = inspect.getsource(gov._run_governed_code_task)
    # Every recorded verify (hand-off and each fix round) records whether BOSS's install failed.
    assert "_install_fail[0] = verification_target.install_failure(result)" in src
    # The implement loop's fix round says it was the install, not "fix the implementation".
    loop = src[src.index("elif _install_fail[0] and not use_microvm:"):]
    assert loop.index("install_repair_evidence") < loop.index("attach_failing_sources")
    # A convergence round does not arbitrate tests that never ran.
    conv = src[src.index("_tr = _run_verify()"):]
    assert conv.index("install_repair_evidence") < conv.index("_test_arbiter_prompt")


# ---- the run's error says it ---------------------------------------------------------------------

def test_a_template_hold_names_the_install_not_the_tests():
    failure = vt.install_failure(_install_result())
    held = gate.hold_outcome(["written_tests_pass", "template_tests"], failure)
    assert held["error"].startswith("Blocked: dependencies did not install: ERESOLVE")
    assert "the template's tests did not pass" not in held["error"]
    assert held["next_step"] == gate.INSTALL_NEXT
    # Without an install failure the words are as before.
    assert "the template's tests did not pass" in gate.hold_outcome(["template_tests"])["error"]


def test_held_run_reads_the_recorded_install_failure():
    failure = vt.install_failure(_install_result())
    row = {"gates_failed": ["template_tests"], "fctx_failing": None, "install_failure": failure}
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(
        mappings=lambda: SimpleNamespace(first=lambda: row)))
    assert "dependencies did not install: ERESOLVE" in gate.held_run(conn, "crun_x")["error"]
    assert "install_failure" in inspect.getsource(gov._run_governed_code_task)


def test_a_blocked_build_without_a_template_says_it_too():
    failure = vt.install_failure(_install_result())
    error, reason = gov.blocked_gate_messages("qg_1", ["written_tests_pass"], None, None, failure)
    assert error.startswith("Blocked: dependencies did not install: ERESOLVE")
    assert reason.startswith("dependencies did not install")


def test_the_gates_own_failed_install_says_it_in_the_same_words(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    out = gate.install_dependencies("node", tmp_path, lambda c, w: _install_result())
    assert out["error"].startswith("dependencies did not install: ERESOLVE")
    assert gate.install_failure_of({"install": out})["summary"] == out["summary"]


# ---- said up front --------------------------------------------------------------------------------

def test_every_writing_stage_and_the_hand_off_checklist_are_told():
    code_agent._SANDBOX_FACTS.clear()
    try:
        assert code_agent.CLEAN_INSTALL_FACT in code_agent.sandbox_facts()
    finally:
        code_agent._SANDBOX_FACTS.clear()
    for text in (code_agent.CLEAN_INSTALL_FACT, gov.FIRST_PASS_CHECKLIST):
        assert "BOSS installs with a clean `npm install`; peer-dependency errors fail the build." in text
        assert "eslint 9 needs eslint-plugin-react-hooks" in text
    assert vt.NPM_INSTALL_CMD in gov.FIRST_PASS_CHECKLIST


# ---- edit -> install -> edit, in the warm session, before the gates ------------------------------

def _failing_then_passing(n_failures, summary="ERESOLVE ... eslint-plugin-react-hooks@4.6.2"):
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] <= n_failures:
            return ({"command": vt.NPM_INSTALL_CMD, "exit_code": 1, "summary": summary,
                     "boss_added": ["@typescript-eslint/parser"], "cwd": "helpdesk-ai"}, ERESOLVE)
        return None
    return check, calls


def test_the_install_is_rechecked_after_every_reply_until_it_passes():
    """The retry's shape: one conflict fixed, a second met, fixed, then BOSS's install passes."""
    check, calls = _failing_then_passing(2)
    prompts, events = [], []
    ok = vt.install_rounds(check, lambda p: prompts.append(p) or True,
                           emit=lambda k, d: events.append(k))
    assert ok is True and calls["n"] == 3 and len(prompts) == 2
    assert all(vt.NPM_INSTALL_CMD in p and vt.INSTALL_REPAIR_INSTRUCTION in p for p in prompts)
    assert "@typescript-eslint/parser" in prompts[0] and "helpdesk-ai" in prompts[0]
    assert events == ["code_task.install_failed", "code_task.install_failed",
                      "code_task.install_fixed"]


def test_the_rounds_end_on_the_budget_or_the_cap():
    check, calls = _failing_then_passing(99)
    asked = []
    assert vt.install_rounds(check, lambda p: asked.append(p) or True, rounds=3) is False
    assert len(asked) == 3 and calls["n"] == 4  # the last reply is checked too
    check, calls = _failing_then_passing(99)
    assert vt.install_rounds(check, lambda p: True, over_budget=lambda: True) is False
    assert calls["n"] == 1
    check, _ = _failing_then_passing(99)
    assert vt.install_rounds(check, lambda p: False) is False  # no warm session to ask
    check, calls = _failing_then_passing(0)
    assert vt.install_rounds(check, lambda p: 1 / 0) is True and calls["n"] == 1


def test_the_pipeline_installs_before_it_judges_each_hand_off():
    src = inspect.getsource(gov._run_governed_code_task)
    # The check is BOSS's verify install: the same preparation and the same install call.
    check = src[src.index("def _install_check():"):src.index("# The last recorded verify's")]
    once = src[src.index("def _run_verify_once("):]
    for part in ("_prepare_app(plan, plan.cwd)", "_boss_install(app_cwd, prep)"):
        assert part in check
    assert "_prepare_app(plan, cwd)" in once and "_boss_install(app_cwd, prep)" in once
    # Implement: after the session (and its continuations), before BOSS's authoritative run.
    loop = src[src.index("for attempt in range(max_repair + 2):"):]
    session = loop.index("while (check_is_inconclusive(")
    install = loop.index('_install_until_it_passes(a, m, impl_session_id, "implement")')
    assert session < install < loop.index("test_result = _run_verify()")
    # A review fix round: after the fix, before the re-verify.
    fix = src[src.index('a_fix, m_fix, impl_session_id, "fix_round")'):]
    assert fix.index("_after = {fc.path") < fix.index("_post = _run_verify()")


# ---- pass 1 does not gate on the browser suite ------------------------------------------------------

def _web_commands(scope):
    token = build_scope.set_scope(scope)
    try:
        return vt.default_test_commands({"project_kind": "web"}, "HelpDesk AI")
    finally:
        build_scope._SCOPE.reset(token)


def test_pass_one_gates_without_test_e2e_and_more_tests_keeps_it():
    pass1 = build_scope.decide("HelpDesk AI", "React app with Playwright e2e tests")
    assert pass1["e2e"] is False and pass1["e2e_deferred"] is True
    assert _web_commands(pass1) == [vt.NPM_INSTALL_CMD, "npm run build", "npm run typecheck",
                                    "npm run lint"]
    pass2 = build_scope.decide("HelpDesk AI", f"{build_scope.MORE_TESTS_MARKER}: add Playwright e2e")
    assert vt.E2E_STEP in _web_commands(pass2)
    assert vt.E2E_STEP in _web_commands(None)  # outside a build, as before
