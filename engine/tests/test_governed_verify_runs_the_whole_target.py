"""The governed quality gate must run every verification command, not just install.

Commit 262d3482 split the web target from one ``&&`` string into a list so the
sandbox — which refuses shell operators — would actually execute it. The
governed pipeline still took ``[0]``. After the split, ``[0]`` is
``npm install``, and ``_run_verify`` treats a successful install as
``tests_passed=true``.

Concrete trigger: a web/UI governed code task with no explicit
``test_commands``. ``npm install`` succeeds. ``npm run build`` would fail.
The run is recorded as verified and can propose a PR.
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.services import verification_target as vt


WEB_SPEC = {"project_kind": "web"}
GOVERNED = Path(__file__).resolve().parents[1] / "boss_engine" / "services" / "governed_code_task_service.py"


def test_the_pipeline_resolves_the_whole_web_target_not_just_install():
    commands = vt.resolve_authoritative_test_commands(WEB_SPEC, "Landing page")
    assert commands[0].startswith("npm install")
    assert "npm run build" in commands
    assert "npm run typecheck" in commands
    assert "npm run lint" in commands
    assert "npm run test:e2e" in commands


def test_governed_verify_does_not_take_only_the_first_default_command():
    src = GOVERNED.read_text(encoding="utf-8")
    assert "default_test_commands(spec, title))[0]" not in src
    assert "resolve_authoritative_test_commands" in src
    assert "run_web_gate" in src  # stops at the first failure, like run_until_failure
    # Guest stages must receive the list. Passing the joined display string
    # would reintroduce the sandbox-refuses-&& failure.
    assert "test_cmds=[test_cmd]" not in src


def test_a_failing_build_is_not_verified_just_because_install_worked():
    class _Result:
        def __init__(self, ok, stdout="", stderr=""):
            self.ok = ok
            self.exit_code = 0 if ok else 1
            self.stdout = stdout
            self.stderr = stderr

    class _Sandbox:
        def __init__(self):
            self.ran = []

        def run(self, cmd, **kwargs):
            self.ran.append(cmd)
            if str(cmd).startswith("npm install"):
                return _Result(True, stdout="added 12 packages")
            return _Result(False, stderr="vite: build failed")

    commands = vt.resolve_authoritative_test_commands(WEB_SPEC, "Landing page")
    box = _Sandbox()
    results = vt.run_until_failure(box, commands)
    assert box.ran[0].startswith("npm install")
    assert box.ran[1] == "npm run build"
    assert not results[-1].ok


def test_an_explicit_command_list_still_wins():
    assert vt.resolve_authoritative_test_commands(
        {"project_kind": "web", "test_commands": ["custom cmd"]},
        "Landing",
    ) == ["custom cmd"]
