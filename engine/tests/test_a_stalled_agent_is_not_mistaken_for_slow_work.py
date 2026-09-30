"""An agent that has gone silent must be killed in minutes, not at the end of the run.

Measured on a real build, from the activity log of crun_1a09fcbedab94c95be39:

    05:08:01  turn.tool_call.Read   (qa)
                  ... 42 minutes 20 seconds of nothing ...
    05:50:22  code_task.github_plane

Nothing was watching. `child.wait(timeout=...)` asks one question — has it finished — and the
answer is "no" whether the agent is thinking hard or has wedged. The only thing that eventually
reacted was the job's lease expiring and being reclaimed, which restarts the work from the
beginning. Seventy-minute builds, of which eight minutes were work.

The operator reported this repeatedly before anybody measured it, and each time it was
explained rather than fixed.

The stream is the liveness signal: every stdout line is already read for the live transcript,
so "has it said anything recently" costs one timestamp.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from boss_engine.services.code_agent import (
    _IDLE_TIMEOUT_DEFAULT,
    _IDLE_TIMEOUT_ENV,
    _drain,
    _idle_timeout_seconds,
)


def _child(script: str):
    return subprocess.Popen([sys.executable, "-u", "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class TestSilenceIsDetected:
    def test_a_silent_child_is_killed_rather_than_waited_out(self, monkeypatch):
        """The run timeout is an hour; the stall must not cost an hour."""
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "1")
        child = _child("import time; time.sleep(120)")   # alive, says nothing, ever
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _drain(child, timeout=3600)                  # a generous overall ceiling
        elapsed = time.monotonic() - started
        assert elapsed < 30, f"waited {elapsed:.0f}s for a silent child"
        assert child.poll() is not None, "the stalled child must actually be killed"

    def test_the_error_says_it_stalled(self, monkeypatch):
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "1")
        child = _child("import time; time.sleep(60)")
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            _drain(child, timeout=3600)
        assert "stalled" in str(caught.value.output or "")


class TestWorkThatIsMerelySlowIsLeftAlone:
    def test_a_child_that_keeps_talking_is_not_killed(self, monkeypatch):
        """Killing real work is worse than waiting: output resets the clock."""
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "2")
        # Speaks every 0.4s for ~4s — far longer than the idle limit, never silent for it.
        child = _child(
            "import time\n"
            "for i in range(10):\n"
            "    print('turn', i, flush=True)\n"
            "    time.sleep(0.4)\n"
        )
        out, _err = _drain(child, timeout=60)
        assert out.count("turn") == 10
        assert child.returncode == 0

    def test_a_quick_child_is_unaffected(self, monkeypatch):
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "5")
        out, _err = _drain(_child("print('done', flush=True)"), timeout=30)
        assert "done" in out


class TestTheOverallTimeoutStillApplies:
    def test_a_chatty_child_still_hits_the_run_ceiling(self, monkeypatch):
        """Idle detection is an ADDITION. An agent that talks forever must still be bounded."""
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "30")
        child = _child(
            "import time\n"
            "while True:\n"
            "    print('busy', flush=True)\n"
            "    time.sleep(0.2)\n"
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                _drain(child, timeout=2)
        finally:
            child.kill()


class TestTheKnob:
    def test_default_is_ten_minutes(self, monkeypatch):
        monkeypatch.delenv(_IDLE_TIMEOUT_ENV, raising=False)
        assert _idle_timeout_seconds() == float(_IDLE_TIMEOUT_DEFAULT) == 600.0

    def test_zero_disables_it(self, monkeypatch):
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "0")
        assert _idle_timeout_seconds() == 0.0

    @pytest.mark.parametrize("junk", ["", "  ", "abc", "-5"])
    def test_nonsense_falls_back_to_the_default_or_zero(self, monkeypatch, junk):
        monkeypatch.setenv(_IDLE_TIMEOUT_ENV, junk)
        value = _idle_timeout_seconds()
        assert value in (0.0, float(_IDLE_TIMEOUT_DEFAULT)), value
        assert value >= 0
