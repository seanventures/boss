"""A failed build's saved test output says why it failed.

crun_b4613dcfd3a44e5fba3f (HelpDesk, login-oauth) saved last_verify_output = "[boss] \\n\\n\\n": a
stack plan that refused had no description, its failure was written from that empty description
rather than its error, and the record ended at "[boss]". verification_target.verify_log is the one
place every verify path now builds that record from.
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.services.sandbox import CommandResult
from boss_engine.services.verification_target import verify_log


def _r(command, exit_code, stdout="", stderr="", timed_out=False):
    return CommandResult(command=command, argv=command.split(), exit_code=exit_code,
                         duration_ms=1, timed_out=timed_out, stdout=stdout, stderr=stderr)


def test_a_refused_plan_with_no_description_keeps_its_error():
    refusal = "This app (helpdesk-ai) is written in a language BOSS cannot build yet."
    failed = _r("", 1, stderr=refusal)
    out = verify_log("", [failed], error=refusal)
    assert out.strip() != "[boss]" and refusal in out


def test_the_failing_commands_last_lines_survive_a_long_log():
    noise = "".join(f"progress line {i}\n" for i in range(2000))
    failing = _r("npm run test:e2e", 1,
                 stdout=noise + "  1) auth.spec.ts:12 sign in › redirects\n"
                        "Error: expected 302, got 500\n")
    out = verify_log("restore restored=False", [_r("npm run build", 0, stdout="built\n"), failing])
    assert len(out) <= 4000 + 200
    assert "Error: expected 302, got 500" in out


def test_a_command_that_printed_nothing_says_so_with_its_exit():
    out = verify_log("restore", [_r("npm run test:e2e", None, timed_out=True)])
    assert "npm run test:e2e timed out" in out and "printed nothing" in out


def test_bookkeeping_alone_never_hides_the_failure():
    # Only BOSS's own lines fit the tail: the command's last real lines are added back.
    failing = _r("npm run lint", 1, stdout="src/a.ts:3:1 error no-unused-vars\n"
                 + "[boss] note\n" * 600)
    out = verify_log("x", [failing])
    assert "src/a.ts:3:1 error no-unused-vars" in out


def test_the_saved_output_is_redacted():
    leaked = "DATABASE_URL=postgres://app:hunter2hunter2@db:5432/app\nAPI_TOKEN=abcd1234efgh5678\n"
    out = verify_log("x", [_r("npm test", 1, stdout=leaked + "FAIL tests/db.test.ts\n")])
    assert "hunter2hunter2" not in out and "abcd1234efgh5678" not in out
    assert "FAIL tests/db.test.ts" in out


def test_a_passing_run_is_unchanged_in_shape():
    out = verify_log("restore restored=False", [_r("npm run build", 0, stdout="ok\n")])
    assert out.startswith("[boss] restore restored=False\n") and "ok" in out
    assert "printed nothing" not in out


def test_every_verify_path_records_through_verify_log():
    src = Path(__file__).resolve().parents[1].joinpath(
        "boss_engine/services/governed_code_task_service.py").read_text(encoding="utf-8")
    assert src.count("verification_target.verify_log(") >= 4
    assert '[-4000:]' not in src.split("def _run_manifest_verify", 1)[1].split("def _changed", 1)[0]
    assert "stderr=(plan.error or plan.description) if failed" in src


def test_a_long_command_output_keeps_its_tail(tmp_path):
    from types import SimpleNamespace

    from boss_engine.services.sandbox import EphemeralSandbox

    box = EphemeralSandbox.__new__(EphemeralSandbox)
    box.settings = SimpleNamespace(sandbox_validation_output_limit_chars=1000)
    kept = box._truncate("x" * 5000 + "THE REAL ERROR\n")
    assert "THE REAL ERROR" in kept and len(kept) < 1100
