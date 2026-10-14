"""An agent stage that runs out of time fails as a timeout, never as "I/O operation on closed file."

crun_944194c4c2ec4534b84e (HelpDesk, login-oauth, build 5 of the live demo) failed after 75
minutes with only `I/O operation on closed file. [substrate=local_subprocess]` and no traceback.

The implement prompt was over 96 KB, so ClaudeCodeAgent._invoke sent it on stdin, wrote it and
CLOSED stdin before draining. When the stage then stalled or hit its limit, the timeout handler
did `child.kill(); child.communicate()` — and Popen.communicate() flushes stdin first, on a pipe
that was already closed, raising ValueError. That ValueError replaced the timeout (which the
pipeline knows how to report) and failed the whole run.

Also here: a run that fails on an exception keeps where it died (crash_frames).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

import pytest

from boss_engine.config import Settings
from boss_engine.services import code_agent
from boss_engine.services.code_agent import _IDLE_TIMEOUT_ENV, ClaudeCodeAgent


def _fake_cli(tmp_path, body: str) -> str:
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


@pytest.fixture
def quiet_agent(tmp_path, monkeypatch):
    """A CLI that reads its whole prompt from stdin, then says nothing, ever."""
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "1")
    monkeypatch.setattr(code_agent, "capture_enabled", lambda: False, raising=False)
    cli = _fake_cli(tmp_path, "cat >/dev/null\nexec sleep 120\n")
    agent = ClaudeCodeAgent(Settings(BOSS_CLAUDE_CLI_PATH=cli, CLAUDE_CODE_OAUTH_TOKEN=""))
    # No credential lookup: the fake CLI needs none, and a test must not reach a secret store.
    monkeypatch.setattr(agent, "_child_env", lambda: {"PATH": os.environ.get("PATH", "")})
    return agent


def test_a_stalled_agent_with_a_stdin_prompt_fails_as_a_timeout(quiet_agent, tmp_path):
    big = "x" * 120_000          # over the 96 KB argv ceiling: the prompt goes on stdin
    started = time.monotonic()
    with pytest.raises(RuntimeError) as caught:
        quiet_agent._invoke(big, tmp_path)
    assert "closed file" not in str(caught.value), caught.value
    assert "timed out" in str(caught.value)
    assert time.monotonic() - started < 60


def test_the_overall_limit_also_ends_and_reaps_the_child(monkeypatch):
    """With idle detection off, the run ceiling still kills the child inside _drain, so no
    caller ever needs communicate() on pipes the readers own."""
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "0")
    child = subprocess.Popen([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    child.stdin.write("prompt")
    child.stdin.close()
    with pytest.raises(subprocess.TimeoutExpired):
        code_agent._drain(child, timeout=1)
    assert child.poll() is not None, "the timed-out child must be killed and reaped"
    code_agent._end_child(child)   # idempotent on a reaped child, and never raises


def test_a_failed_run_keeps_its_frames_without_values():
    from boss_engine.services import crash_frames

    def inner(secret):
        raise ValueError(f"I/O operation on closed file. token={secret}")

    try:
        try:
            inner("ghp_" + "a" * 36)
        except ValueError as exc:
            raise RuntimeError("wrapped") from exc
    except RuntimeError as exc:
        rec = crash_frames.record(exc)
        text = crash_frames.formatted(exc)
    assert rec["type"] == "RuntimeError"
    assert rec["cause"]["type"] == "ValueError"
    last = rec["cause"]["frames"][-1]
    assert set(last) == {"file", "line", "function"}
    assert last["function"] == "inner" and last["line"] > 0
    assert "ghp_" not in repr(rec), "frames carry no values"
    assert "inner" in text and "ghp_" + "a" * 36 not in text, "the logged traceback is redacted"


def test_every_handler_in_the_governed_pipeline_logs_its_traceback():
    import re
    from pathlib import Path

    from boss_engine.services import governed_code_task_service as g

    lines = Path(g.__file__).read_text(encoding="utf-8").splitlines()
    missing = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*except Exception( as \w+)?:", line):
            nxt = next(l for l in lines[i + 1:] if l.strip() and not l.strip().startswith("#"))
            # _record_swallowed logs the traceback too (at ERROR, with the run id) and counts it.
            if "_log_caught(" not in nxt and "_record_swallowed(" not in nxt:
                missing.append(i + 1)
    assert not missing, f"handlers that swallow a traceback: lines {missing}"
    assert "'crash_frames'" in Path(g.__file__).read_text(encoding="utf-8")
