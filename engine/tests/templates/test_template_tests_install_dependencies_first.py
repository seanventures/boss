"""The template's tests run after the app's dependencies are installed, in the gate AND the trial
(F2 §2.3, §2.5): the image holds no app's dependencies, so a Python template needing authlib or a
Java one needing Maven artifacts failed before a single test ran. And a re-apply whose tests are
already on the app's branch is not refused for "tests not in the change".
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.templates import gate, trial
from boss_engine.templates.manifest import validate_manifest

MANIFEST = {
    "schema": "boss.template/v1", "id": "deps", "name": "Deps", "version": "1.0.0",
    "summary": "Needs a dependency", "adds": ["routes"],
    "stacks": {"python": {"frameworks": ["fastapi"], "min_toolchain": "python311"},
               "node": {"frameworks": ["express"], "min_toolchain": "node20"},
               "java": {"frameworks": ["dropwizard"], "min_toolchain": "java21"}},
    "routes": ["GET /deps"],
    "tests": {s: {"command": c, "report": "junit", "min_tests": 1} for s, c in (
        ("python", "python3 -m pytest -q tests/deps --junitxml=r.xml"),
        ("node", "node --test tests/deps"),
        ("java", "mvn -q -B test"))},
    "license": "Apache-2.0", "min_boss": "1.0.0",
}
JUNIT = b'<testsuite><testcase name="a"/><testcase name="b"/></testsuite>'


@pytest.mark.parametrize("stack,marker,expected", [
    ("python", "requirements.txt", "python3 -m pip install --user"),
    ("node", "package-lock.json", "npm ci"),
    ("node", "package.json", "npm install"),
    ("java", "pom.xml", "mvn -q -B dependency:go-offline"),
])
def test_each_stack_installs_what_the_app_declares(tmp_path, stack, marker, expected):
    (tmp_path / marker).write_text("x")
    assert gate.install_command(stack, tmp_path).startswith(expected)
    assert gate.install_command(stack, tmp_path / "nothing-here") is None


def _runner(calls, *, install_exit=0):
    def run(command, cwd):
        calls.append(command)
        if "install" in command or "go-offline" in command or command.startswith("npm ci"):
            return SimpleNamespace(exit_code=install_exit, stdout="", stderr="no route to pypi" if install_exit else "")
        (Path(cwd) / "r.xml").write_bytes(JUNIT)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")
    return run


def _gate(tmp_path, calls, **kw):
    files = {"stacks/python/tests/deps/test_deps.py": b"def test(): pass\n"}
    return gate.evaluate(template={"stack": "python", "manifest": MANIFEST}, files=files,
                         changed={"tests/deps/test_deps.py": "def test(): pass\n"}, cwd=tmp_path,
                         run_command=_runner(calls, **kw))


def test_the_gate_installs_before_it_runs_the_tests(tmp_path):
    (tmp_path / "requirements.txt").write_text("authlib==1.3.1\n")
    calls: list[str] = []
    result = _gate(tmp_path, calls)
    assert calls[0].startswith("python3 -m pip install --user") and calls[1].startswith("python3 -m pytest")
    assert result.gates["template_tests"] is True
    assert result.evidence["template_tests"]["install"]["exit_code"] == 0


def test_a_failed_install_fails_the_gate_and_runs_no_test(tmp_path):
    (tmp_path / "requirements.txt").write_text("authlib==1.3.1\n")
    calls: list[str] = []
    result = _gate(tmp_path, calls, install_exit=1)
    assert len(calls) == 1 and result.gates["template_tests"] is False
    assert "installing the app's dependencies failed" in result.evidence["template_tests"]["error"]


def test_the_trial_installs_before_it_runs_the_tests(tmp_path):
    manifest = validate_manifest(MANIFEST)
    files = {"stacks/python/requirements.txt": b"authlib==1.3.1\n",
             "stacks/python/tests/deps/test_deps.py": b"def test(): pass\n"}
    calls: list[str] = []
    out = trial.run_stack(manifest, files, "python", runner=_runner(calls), workdir=tmp_path)
    assert calls[0].startswith("python3 -m pip install --user") and out["ok"] is True
    calls.clear()
    out = trial.run_stack(manifest, files, "python", runner=_runner(calls, install_exit=1), workdir=tmp_path)
    assert out["ok"] is False and len(calls) == 1 and "dependencies failed" in out["error"]


def test_a_reapply_whose_tests_are_already_on_the_branch_is_not_refused(tmp_path):
    """The second apply of a template: its test files are on the app's branch already and
    unchanged, so they are not in this diff. The PR carries them; the gate runs them."""
    (tmp_path / "tests" / "deps").mkdir(parents=True)
    (tmp_path / "tests" / "deps" / "test_deps.py").write_text("def test(): pass\n")
    calls: list[str] = []
    files = {"stacks/python/tests/deps/test_deps.py": b"def test(): pass\n"}
    result = gate.evaluate(template={"stack": "python", "manifest": MANIFEST}, files=files,
                           changed={"app.py": "x = 1\n"}, cwd=tmp_path, run_command=_runner(calls))
    tt = result.evidence["template_tests"]
    assert tt["already_in_app"] == ["test_deps.py"] and tt["missing_from_diff"] == []
    assert result.gates["template_tests"] is True
    # and a test file that is nowhere still fails the gate
    (tmp_path / "tests" / "deps" / "test_deps.py").unlink()
    result = gate.evaluate(template={"stack": "python", "manifest": MANIFEST}, files=files,
                           changed={"app.py": "x = 1\n"}, cwd=tmp_path, run_command=_runner([]))
    assert result.gates["template_tests"] is False
