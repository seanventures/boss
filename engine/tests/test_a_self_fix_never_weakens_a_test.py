"""P0: a BOSS self-fix never weakens a test (BOSSAPP-571, spec §8.6, F9, R3).

The existing gates count test definitions, so a test marked `skip` or `xfail`, or an assertion
loosened in place, still counted as present. `tests_not_weakened` makes every existing test file
APPEND-ONLY and checks every new test for the ways a test passes without testing. Each refusal
is final (never retried): a weakening attempt is a signal, not a mistake.
"""
from __future__ import annotations

import pytest

from boss_engine.services import self_fix_integrity as si
from boss_engine.services.self_fix_policy import FileChange

LOCATION = {"file": "engine/boss_engine/services/stack_deploy.py",
            "function": "plan_stack_deploy"}
EXISTING = '''from boss_engine.services import stack_deploy


def test_plans_a_release():
    assert stack_deploy.plan_stack_deploy is not None
'''
GOOD_NEW = '''from boss_engine.services import stack_deploy


def test_a_release_leaving_the_shared_project_retires_its_app():
    plan = stack_deploy.plan_stack_deploy
    assert plan is not None and callable(plan)
'''


def _gate(*changes):
    return si.tests_not_weakened(list(changes), location=LOCATION)


def test_a_new_test_file_that_tests_passes():
    result = _gate(FileChange("engine/tests/test_new.py", "A", None, GOOD_NEW))
    assert result.ok and result.detail["new_tests"]


def test_appending_a_test_to_an_existing_file_passes():
    after = EXISTING + "\n\ndef test_more():\n    assert stack_deploy.plan_stack_deploy\n"
    assert _gate(FileChange("engine/tests/test_old.py", "M", EXISTING, after)).ok


@pytest.mark.parametrize("label, after", [
    ("deleted test", "from boss_engine.services import stack_deploy\n"),
    ("edited body", EXISTING.replace("is not None", "or True")),
    ("skip added", EXISTING.replace("def test_plans", "@pytest.mark.skip\ndef test_plans")),
    ("xfail added", EXISTING.replace("def test_plans", "@pytest.mark.xfail\ndef test_plans")),
])
def test_an_existing_test_is_never_changed(label, after):
    result = _gate(FileChange("engine/tests/test_old.py", "M", EXISTING, after))
    assert not result.ok and result.final, label


def test_deleting_or_renaming_a_test_file_refuses():
    assert not _gate(FileChange("engine/tests/test_old.py", "D", EXISTING, None)).ok
    assert not _gate(FileChange("engine/tests/test_new_name.py", "R", EXISTING, EXISTING,
                                old_path="engine/tests/test_old.py")).ok


@pytest.mark.parametrize("path", ["engine/tests/conftest.py", "apps/api/tests/fixtures/a.json",
                                  "apps/web/tests/__snapshots__/x.snap", "pytest.ini",
                                  "tests/tiers.yaml"])
def test_fixtures_snapshots_conftest_and_config_never_change(path):
    assert not _gate(FileChange(path, "M", "a\n", "b\n")).ok
    assert not _gate(FileChange(path, "A", None, "b\n")).ok


@pytest.mark.parametrize("label, body", [
    ("skip marker", "@pytest.mark.skip(reason='later')\ndef test_x():\n    assert stack_deploy\n"),
    ("skipif marker", "@pytest.mark.skipif(True, reason='x')\ndef test_x():\n    assert stack_deploy\n"),
    ("xfail marker", "@pytest.mark.xfail\ndef test_x():\n    assert stack_deploy\n"),
    ("pytest.skip call", "def test_x():\n    pytest.skip('x')\n    assert stack_deploy\n"),
    ("importorskip", "def test_x():\n    pytest.importorskip('nope')\n    assert stack_deploy\n"),
    ("early return", "def test_x():\n    return\n    assert stack_deploy\n"),
    ("assert True", "def test_x():\n    assert True\n"),
    ("or True", "def test_x():\n    assert stack_deploy.plan_stack_deploy() or True\n"),
    ("raises Exception around everything",
     "def test_x():\n    with pytest.raises(Exception):\n        stack_deploy.plan_stack_deploy()\n"),
    ("no assertion", "def test_x():\n    stack_deploy.plan_stack_deploy\n"),
])
def test_a_new_test_that_cannot_fail_is_refused(label, body):
    source = "import pytest\nfrom boss_engine.services import stack_deploy\n\n\n" + body
    result = _gate(FileChange("engine/tests/test_new.py", "A", None, source))
    assert not result.ok and result.final, label


def test_an_unconditional_pytest_fail_is_refused_a_conditional_one_is_an_assertion():
    """A body that always calls pytest.fail can never pass; `if wrong: pytest.fail(why)` is a
    strict assertion (BOSS's first self-fix on the laptop was refused for writing one)."""
    head = "import pytest\nfrom boss_engine.services import stack_deploy\n\n\n"
    always = head + "def test_x():\n    stack_deploy.plan_stack_deploy\n    pytest.fail('later')\n"
    assert not _gate(FileChange("engine/tests/test_new.py", "A", None, always)).ok
    conditional = head + ("def test_x():\n    plan = stack_deploy.plan_stack_deploy\n"
                          "    if plan is None:\n        pytest.fail('no plan')\n    assert plan\n")
    assert _gate(FileChange("engine/tests/test_new.py", "A", None, conditional)).ok


def test_a_new_test_must_name_the_diagnosed_code():
    source = "def test_x():\n    assert 1 + 1 == 2\n"
    result = _gate(FileChange("engine/tests/test_new.py", "A", None, source))
    assert not result.ok and "never names the diagnosed code" in result.why


def test_a_module_wide_pytestmark_is_refused():
    source = "import pytest\npytestmark = pytest.mark.skip\n" + GOOD_NEW
    assert not _gate(FileChange("engine/tests/test_new.py", "A", None, source)).ok


@pytest.mark.parametrize("line", ["it.skip('x', () => {})", "test.only('x', () => {})",
                                  "describe.skip('x', () => {})", "test.todo('x')",
                                  "xit('x', () => {})"])
def test_a_skipped_or_focused_js_test_is_refused(line):
    change = FileChange("apps/web/tests/a.test.ts", "A", None, f"import x from 'y'\n{line}\n")
    assert not _gate(change).ok


# ── red, then green ──────────────────────────────────────────────────────────────────────────

def _run(code: int, out: str) -> si.SuiteRun:
    return si.SuiteRun.from_pytest(code, out)


RED = ("FAILED engine/tests/test_new.py::test_x - AssertionError: plan_stack_deploy left the "
       "app container in the shared project\n1 failed in 0.2s")
GREEN = "1 passed in 0.2s"


def test_red_on_base_then_green_on_the_fix_passes():
    assert si.red_then_green(_run(1, RED), _run(0, GREEN), location=LOCATION).ok


@pytest.mark.parametrize("label, red", [
    ("passes on base", _run(0, GREEN)),
    ("import error", _run(2, "ImportError: cannot import name 'retire_app_in_shared'\n"
                             "1 error in 0.1s")),
    ("unrelated failure", _run(1, "FAILED engine/tests/test_new.py::test_x - KeyError: 'zzz'\n"
                                  "1 failed in 0.1s")),
    ("calls a function the fix adds",
     _run(1, "E   AttributeError: module 'boss_engine.services.stack_deploy' has no attribute "
             "'retire_app_in_shared'\nFAILED engine/tests/test_new.py::test_x\n1 failed in 0.1s")),
])
def test_no_reproduction_is_refused(label, red):
    assert not si.red_then_green(red, _run(0, GREEN), location=LOCATION).ok, label


def test_a_fix_that_does_not_go_green_is_refused():
    assert not si.red_then_green(_run(1, RED), _run(1, RED), location=LOCATION).ok


# ── scope, dependencies, size, secrets ───────────────────────────────────────────────────────

def test_a_change_outside_the_diagnosed_area_is_refused():
    changes = [FileChange(LOCATION["file"], "M", "a\n", "b\n"),
               FileChange("engine/boss_engine/services/unrelated.py", "M", "a\n", "b\n")]
    result = si.scope_matches_diagnosis(changes, location=LOCATION,
                                        sources={LOCATION["file"]: "from . import core\n"})
    assert not result.ok and "unrelated.py" in result.why and result.final


def test_one_import_hop_or_a_named_file_is_in_scope():
    changes = [FileChange(LOCATION["file"], "M", "a\n", "b\n"),
               FileChange("engine/boss_engine/services/core.py", "M", "a\n", "b\n"),
               FileChange("engine/boss_engine/services/named.py", "M", "a\n", "b\n")]
    result = si.scope_matches_diagnosis(
        changes, location=LOCATION,
        named=[{"path": "engine/boss_engine/services/named.py", "reason": "it renders the step"}],
        sources={LOCATION["file"]: "from .core import x\n"})
    assert result.ok


def test_a_dependency_change_is_refused():
    assert not si.no_new_dependencies([FileChange("apps/api/requirements.txt", "M", "a", "b")]).ok
    assert not si.no_new_dependencies([FileChange("Dockerfile.community", "M", "a", "b")]).ok
    assert si.no_new_dependencies([FileChange(LOCATION["file"], "M", "a", "b")]).ok


def test_a_change_too_big_for_a_self_fix_is_refused():
    big = "\n".join(f"x{i} = {i}" for i in range(400)) + "\n"
    assert not si.within_size([FileChange(LOCATION["file"], "M", "", big)],
                              max_files=6, max_lines=300).ok


def test_a_secret_on_an_added_line_is_refused():
    leaked = 'KEY = "ghp_' + "a" * 36 + '"\n'
    assert not si.no_committed_secrets([FileChange(LOCATION["file"], "M", "", leaked)]).ok


# Live, 2026-10-24 (sfx_de308b793d694ac983ff): the fix for propose_action was refused 'secrets'
# for these three added lines of builtin.py — code references, no value anywhere.
_CODE_REFERENCES = [
    "            workflow_key = _workflow_key_for_proposed_name(name)\n",
    "                            f\"with tool='start_workflow', workflow_key='{workflow_key}' and a \"\n",
    "                        \"proposed_args\": {\"workflow_key\": workflow_key}}\n",
    "    api_token = settings.github.token\n",
    "    headers = {\"x-api-key\": self.api_key}\n",
]


@pytest.mark.parametrize("line", _CODE_REFERENCES)
def test_a_code_reference_on_an_added_line_is_not_a_secret(line):
    gate = si.no_committed_secrets([FileChange("engine/boss_engine/tools/builtin.py", "M", "", line)])
    assert gate.ok, gate.detail if hasattr(gate, "detail") else gate


@pytest.mark.parametrize("path,line", [
    ("engine/boss_engine/tools/builtin.py", 'api_key = "Zq8vT4mN2pW6rX1y"\n'),           # quoted literal
    ("engine/boss_engine/tools/builtin.py", "client_secret='Zq8vT4mN2pW6rX1y'\n"),
    ("engine/boss_engine/tools/builtin.py", 'headers = {"token": "Zq8vT4mN2pW6rX1y"}\n'),
    ("engine/boss_engine/tools/builtin.py", "x = 'sk-ant-" + "a" * 24 + "'\n"),         # token shape
    ("engine/boss_engine/tools/builtin.py", 'password = derive("hunter2")\n'),         # literal arg
    ("engine/boss_engine/tools/builtin.py",
     'api_key = os.getenv("X", "literal-default-secret")\n'),
    ("engine/boss_engine/tools/builtin.py", "secret = b64decode('c2VjcmV0LXZhbHVl')\n"),
    ("scripts/install/example.env", "OPENAI_API_KEY=Zq8vT4mN2pW6rX1y\n"),               # env literal
    ("scripts/install/deploy.sh", "export API_TOKEN=Zq8vT4mN2pW6rX1y\n"),
])
def test_a_literal_secret_is_still_refused(path, line):
    assert not si.no_committed_secrets([FileChange(path, "M", "", line)]).ok


def test_a_kept_diff_shows_code_references_and_masks_literals():
    """sfx_de308b79's kept attempt read `workflow_key = [SECRET])` to the reviewer and to the
    next attempt that learns from it. Code stays code; a literal is still masked."""
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,3 @@\n"
            "+    workflow_key = _workflow_key_for_proposed_name(name)\n"
            "+    api_key = \"Zq8vT4mN2pW6rX1y\"\n"
            "+    token = 'ghp_" + "a" * 36 + "'\n")
    kept = si.redact_diff_secrets(diff)
    assert "workflow_key = _workflow_key_for_proposed_name(name)" in kept
    assert "Zq8vT4mN2pW6rX1y" not in kept and "ghp_" + "a" * 36 not in kept
    assert kept.startswith("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n")
