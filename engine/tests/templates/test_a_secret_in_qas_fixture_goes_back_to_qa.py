"""A secret-looking literal the TEST AUTHOR committed is repaired by the test author.

The dead end (crun_b4613dcf, crun_ce7e8841): no_committed_secrets scans tests too, the coder may
not edit tests, and QA was never re-run, so every repair round failed the same gate. The gate is
exactly as strict; what changed is who gets the finding. These tests drive the real scanner, a
real git tree and a real test run."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from boss_engine.services import fixture_secret_repair as fsr
from boss_engine.services import governed_code_task_service as gcts
from boss_engine.templates.secret_scan import scan_files

# Assembled so this file itself carries no literal the scan would flag.
_LIT = "hunter2" + "Hunter2" + "xyz"
TEST_FILE = "helpdesk-ai/tests/api/auth_test.py"
TEST_SRC = (
    "import os\n"
    "\n"
    "def login(user, pw):\n"
    "    return bool(user) and len(pw) >= 12\n"
    "\n"
    "def test_login_accepts_a_long_password():\n"
    f'    password = "{_LIT}"\n'
    "    assert login('agent', password)\n"
)
IMPL_FILE = "helpdesk-ai/src/auth.py"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "helpdesk-ai/tests/api").mkdir(parents=True)
    (root / "helpdesk-ai/src").mkdir(parents=True)
    (root / "README.md").write_text("x\n")
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *cmd], cwd=root, check=True, capture_output=True)
    (root / TEST_FILE).write_text(TEST_SRC)        # what QA wrote this run
    (root / IMPL_FILE).write_text("def ok():\n    return True\n")
    return root


def _scan(root: Path, paths) -> list[dict]:
    return [f.as_dict() for f in scan_files({p: (root / p).read_text() for p in paths})]


def _suite_passes(root: Path) -> bool:
    code = (root / TEST_FILE).read_text() + "\ntest_login_accepts_a_long_password()\n"
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def _qa_owns(p: str) -> bool:
    return gcts._is_test_path(p)


def test_a_qa_authored_literal_is_repaired_by_the_fixture_turn_and_passes_the_gate(tmp_path):
    root = _repo(tmp_path)
    findings = _scan(root, [TEST_FILE, IMPL_FILE])
    assert [(f["path"], f["line"], f["rule"]) for f in findings] == [(TEST_FILE, 7, "assigned_secret")]
    qa, coder = fsr.split_secret_findings(findings, qa_owns=_qa_owns)
    assert qa and not coder

    prompts: list[str] = []

    def qa_turn(prompt: str) -> dict:   # the test author does exactly what it is told
        prompts.append(prompt)
        p = root / TEST_FILE
        p.write_text(p.read_text().replace(
            f'"{_LIT}"', 'os.environ.get("TEST_PASSWORD") or __import__("secrets").token_urlsafe(16)'))
        return {"cost_usd_micros": 1234}

    rec = fsr.repair_fixture_secrets(root=root, findings=qa, run_qa=qa_turn,
                                     rerun_tests=lambda: _suite_passes(root),
                                     tests_passed_before=True, rescan=scan_files)
    assert rec["repaired"] is True and rec["reverted"] is False and rec["remaining"] == []
    assert rec["role"] == "write_tests" and rec["cost_usd_micros"] == 1234
    assert f"{TEST_FILE}:7" in prompts[0] and _LIT not in prompts[0]   # names the line, never the value
    assert _scan(root, [TEST_FILE]) == []                                # the gate now passes
    assert _suite_passes(root)                                           # same test, still green


def test_a_fixture_turn_that_touches_anything_else_is_reverted(tmp_path):
    root = _repo(tmp_path)
    qa, _ = fsr.split_secret_findings(_scan(root, [TEST_FILE]), qa_owns=_qa_owns)

    def greedy(prompt: str) -> dict:   # fixes the literal AND weakens the assertion AND edits code
        p = root / TEST_FILE
        p.write_text(p.read_text().replace(f'"{_LIT}"', "os.urandom(8).hex()")
                     .replace("assert login('agent', password)", "assert True"))
        (root / IMPL_FILE).write_text("def ok():\n    return False\n")
        return {}

    rec = fsr.repair_fixture_secrets(root=root, findings=qa, run_qa=greedy, rescan=scan_files)
    assert rec["reverted"] is True and rec["repaired"] is False
    assert (root / TEST_FILE).read_text() == TEST_SRC                 # put back exactly
    assert "return True" in (root / IMPL_FILE).read_text()
    assert _scan(root, [TEST_FILE])                                    # the gate still fails


def test_a_fixture_turn_that_breaks_a_passing_suite_is_reverted(tmp_path):
    root = _repo(tmp_path)
    qa, _ = fsr.split_secret_findings(_scan(root, [TEST_FILE]), qa_owns=_qa_owns)

    def breaks(prompt: str) -> dict:
        p = root / TEST_FILE
        p.write_text(p.read_text().replace(f'"{_LIT}"', "''"))
        return {}

    rec = fsr.repair_fixture_secrets(root=root, findings=qa, run_qa=breaks,
                                     rerun_tests=lambda: _suite_passes(root),
                                     tests_passed_before=True, rescan=scan_files)
    assert rec["reverted"] is True and (root / TEST_FILE).read_text() == TEST_SRC


def test_the_gate_still_fails_on_a_literal_in_implementation_code(tmp_path):
    root = _repo(tmp_path)
    (root / IMPL_FILE).write_text(f'API_TOKEN = "{_LIT}"\n')
    findings = _scan(root, [IMPL_FILE])
    assert findings, "the scan is exactly as strict on implementation code"
    qa, coder = fsr.split_secret_findings(findings, qa_owns=_qa_owns)
    assert qa == [] and [f["path"] for f in coder] == [IMPL_FILE]   # the coder's, as before


def test_without_a_separate_test_author_test_findings_stay_with_the_coder():
    f = {"path": TEST_FILE, "line": 7, "rule": "assigned_secret"}
    qa, coder = fsr.split_secret_findings([f], qa_owns=lambda p: False)
    assert qa == [] and coder == [f]


def test_the_coder_still_cannot_edit_tests():
    src = Path(gcts.__file__).read_text()
    # The fixture turn runs as the TEST AUTHOR's route, never the implementer's...
    block = src[src.index("def _fixture_qa_turn"):src.index("fixture_repair_record = _fsr.repair_fixture_secrets")]
    assert '_route("write_tests")' in block and "implement" not in block
    # ...and it does not touch the implementer's tamper verdict, which still gates the build.
    assert "coder_did_not_edit_tests" in gcts._TEST_GATES
    assert re.search(r'gates\["coder_did_not_edit_tests"\] = tests_untouched', src)
    assert "tests_untouched" not in src[src.index("A SECRET IN QA'S FIXTURE"):src.index("_tpl_gates = dict(gates)")]


def test_a_retry_of_a_build_that_failed_on_test_secrets_runs_the_fixture_repair():
    # retry_code_task carries the parent's failing gates; the child records why it repairs.
    assert fsr.parent_failed_on_test_secrets({"prior_failing_gates": ["no_committed_secrets"]})
    assert not fsr.parent_failed_on_test_secrets({"prior_failing_gates": ["code_review_passed"]})
    src = Path(gcts.__file__).read_text()
    # Ownership is fixed BEFORE a retry may skip write_tests, so a reused suite is still QA's.
    assert src.index("_qa_authors_tests = do_separate_tests") < src.index('"skipped_stage": "write_tests"')
    # The repair is evaluated at the gate of EVERY run, a retry's included, not only in-round.
    assert "if _template_record and gates.get(\"no_committed_secrets\") is False:" in src


def test_check_line_edits_allows_only_flagged_lines_and_imports():
    before = "a\nb\nc\n"
    assert fsr.check_line_edits(before, "a\nB\nc\n", {2}) == []
    assert fsr.check_line_edits(before, "import secrets\na\nB\nc\n", {2}) == []
    assert fsr.check_line_edits(before, "A\nB\nc\n", {2})
    assert fsr.check_line_edits(before, "a\nc\n", {2})
    assert fsr.check_line_edits(before, "a\nB\nextra()\nc\n", {2})
