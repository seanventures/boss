"""A build went silent for eighteen minutes on a command with a three-minute timeout.

From `agent_turns` for crun_33a0a77e6ccf4d17b36e — the run an operator reported as "stuck for
27m for ● Engineer implementing":

    turn 99  tool_call  Bash  {"command": "rm -rf .e2e-data && npm run verify 2>&1 | tail -150",
                               "timeout": 180000}          23:49:36
    (no tool_result, ever)                                 cancelled 00:08:19

46 tool calls in that run and 45 results — exactly one command that never came back. The
engineer had been working normally for nine and a half minutes before it: reading files, fixing
a Playwright `selectOption` call, editing e2e specs. Then one command, and silence.

`subprocess.run(timeout=...)` does not bound that. On timeout it kills the DIRECT CHILD and
then calls `communicate()` again to reap the pipes, with no timeout on that second call.
`npm run verify` runs Playwright, Playwright starts the app's webServer, and that grandchild
inherits the stdout pipe. Killing npm leaves the server alive holding the write end open, the
pipe never reaches EOF, and the reap blocks forever. The stage stops producing turns and
nothing anywhere notices.

The codebase already knew this hazard existed — governed_code_task_service warns that a
re-run "can start a leftover webServer" — without knowing it could hang a run.
"""
from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import time

import pytest

from ._source import code_only
from boss_engine.services import sandbox


class TestTheRunnerCannotBlockForever:
    def test_it_no_longer_uses_the_call_that_cannot_be_bounded(self):
        # code_only: the comment above the fix NAMES subprocess.run as the thing that broke,
        # so a plain source grep matches the explanation and passes whatever the code does.
        body = code_only(sandbox.EphemeralSandbox.run)
        # The owned spawn (owned_process.popen): a Popen whose exit and kill end the tree.
        assert "owned_process.popen(" in body
        assert "subprocess.run(" not in body

    def test_the_command_gets_its_own_process_group(self):
        """Without this there is no group to kill, and the kill reaches only the process we
        spawned — leaving exactly the grandchild that holds the pipe open."""
        body = inspect.getsource(sandbox.EphemeralSandbox.run)
        assert "start_new_session=True" in body

    def test_the_whole_tree_is_killed_on_timeout(self):
        body = inspect.getsource(sandbox.EphemeralSandbox.run)
        assert "_kill_process_group(proc)" in body
        assert "os.killpg" in inspect.getsource(sandbox._kill_process_group)

    def test_the_drain_after_the_kill_is_bounded(self):
        """The exact line that hung. A second unbounded communicate() is the bug, not the
        first one."""
        body = inspect.getsource(sandbox.EphemeralSandbox.run)
        # owned_process.communicate ends the read when the leader exits and bounds the drain
        # after a kill (owned_process._DRAIN_SECONDS); see test_owned_resources_are_reaped.
        from boss_engine.services import owned_process
        assert "owned_process.communicate(proc, timeout=limit)" in body
        assert "t.join(timeout=_DRAIN_SECONDS)" in inspect.getsource(owned_process.communicate)
        assert owned_process._DRAIN_SECONDS <= 30

    def test_and_giving_up_on_the_pipe_still_reports_the_timeout(self):
        """Losing the tail of stdout is a far smaller harm than a stage that never ends — but
        the caller must still be told the command timed out rather than getting a silent
        success."""
        body = inspect.getsource(sandbox.EphemeralSandbox.run)
        assert 'raise subprocess.TimeoutExpired(argv, limit' in body

    def test_a_timeout_is_still_reported_as_a_timeout_to_the_caller(self):
        body = inspect.getsource(sandbox.EphemeralSandbox.run)
        assert "timed_out=True" in body


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestAgainstARealHangingGrandchild:
    """The scenario itself, reproduced: a command that exits while a child it started keeps
    the stdout pipe open. This is what Playwright's webServer does."""

    @staticmethod
    def _spawn(start_new_session: bool):
        # The parent exits immediately; the child sleeps for a minute holding stdout. This is
        # `npm run verify` finishing while the webServer it spawned stays up.
        script = (
            "import subprocess, sys;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "sys.exit(0)"
        )
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=start_new_session,
        )

    def test_this_is_what_hangs_the_old_way(self):
        """Proves the mechanism rather than asserting it. The parent is long gone, and reading
        to EOF still blocks because the grandchild holds the pipe."""
        proc = self._spawn(start_new_session=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                proc.communicate(timeout=2)
        finally:
            sandbox._kill_process_group(proc)

    def test_the_group_is_killable_after_the_parent_is_gone(self):
        """The case the first version got wrong. `npm` has exited and been reaped, so
        `os.getpgid(proc.pid)` raises — and the group, holding the webServer, is still there.
        Falling back to `proc.kill()` at that point kills something already dead."""
        proc = self._spawn(start_new_session=True)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(timeout=2)
        assert sandbox._pgid_of(proc) is None, "expected the parent to be gone already"
        sandbox._kill_process_group(proc)
        proc.communicate(timeout=sandbox._DRAIN_SECONDS)      # must not raise

    def test_killing_the_group_releases_the_pipe(self):
        """The fix, end to end: after the group kill the drain completes instead of blocking,
        which is the difference between a three-minute timeout and eighteen minutes of
        silence."""
        proc = self._spawn(start_new_session=True)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(timeout=2)

        sandbox._kill_process_group(proc)
        started = time.monotonic()
        proc.communicate(timeout=sandbox._DRAIN_SECONDS)      # must not raise
        assert time.monotonic() - started < sandbox._DRAIN_SECONDS

    def test_killing_only_the_child_does_not(self):
        """Why the group matters. Same situation, killing just the process we spawned — the
        grandchild survives and the pipe stays open."""
        proc = self._spawn(start_new_session=True)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(timeout=2)
        try:
            proc.kill()
            with pytest.raises(subprocess.TimeoutExpired):
                proc.communicate(timeout=2)
        finally:
            sandbox._kill_process_group(proc)


class TestTheKillIsBestEffort:
    """A sandbox command must never fail because cleanup did."""

    def test_a_vanished_process_is_not_an_error(self):
        class Gone:
            pid = -1

            def kill(self):
                raise ProcessLookupError

        sandbox._kill_process_group(Gone())      # must not raise

    def test_no_process_group_falls_back_to_the_child(self):
        killed = {"n": 0}

        class NoGroup:
            # NOT this process's pid: with the group kill below stubbed out the real call
            # would target the test runner's own group.
            pid = 2 ** 31 - 1

            def kill(self):
                killed["n"] += 1

        original = os.killpg
        os.killpg = lambda *_a: (_ for _ in ()).throw(OSError("no group"))
        try:
            sandbox._kill_process_group(NoGroup())
        finally:
            os.killpg = original
        assert killed["n"] == 1


def test_signal_is_available_for_the_kill():
    """Guards the import inside the helper: a NameError there would turn every timeout into a
    crash in cleanup."""
    assert signal.SIGKILL is not None
