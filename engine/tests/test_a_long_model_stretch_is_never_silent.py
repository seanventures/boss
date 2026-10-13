"""A long model stretch is never silent, and the per-token stream never bloats the output.

A HelpDesk AI build had two silent stretches of 7 and 12 minutes in which the model thought
before acting. The CLI now streams partial messages; BOSS turns them into one "Thinking… 2m"
line per half minute, and keeps the token lines out of the stored stdout.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from boss_engine.services import code_agent
from boss_engine.services.code_agent import LivePulse, _drain, is_partial_line

THINK = '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"h"}}}\n'
WRITE = '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"x"}}}\n'
TURN = '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}\n'


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_partial_lines_are_recognised():
    assert is_partial_line(THINK) and is_partial_line(WRITE)
    assert not is_partial_line(TURN)


def test_a_stretch_pulses_every_half_minute_with_its_phase_and_length():
    clock = _Clock()
    pulse = LivePulse(clock=clock)
    assert pulse.feed(THINK) is None          # the stretch starts
    clock.t = 10
    assert pulse.feed(THINK) is None          # under 30s: quiet
    clock.t = 31
    beat = pulse.feed(THINK)
    assert beat == {"direction": "completion", "level": "info", "content": "Thinking… 31s so far"}
    clock.t = 50
    assert pulse.feed(THINK) is None          # rate-limited
    clock.t = 125
    assert pulse.feed(THINK)["content"] == "Thinking… 2m 05s so far"
    clock.t = 126
    assert pulse.feed(WRITE) is None          # a new phase starts its own clock
    clock.t = 160
    assert pulse.feed(WRITE)["content"] == "Writing… 34s so far"


def test_a_whole_turn_ends_the_stretch():
    clock = _Clock()
    pulse = LivePulse(clock=clock)
    pulse.feed(THINK)
    clock.t = 40
    pulse.reset()
    assert pulse.feed(THINK) is None


def test_the_pulse_turn_keeps_its_own_level():
    from boss_engine.services.agent_turns import LEVELS
    assert "info" in LEVELS


class _Child:
    def __init__(self, out: str):
        self.stdout = io.StringIO(out)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def test_token_lines_reach_the_live_view_but_not_the_stored_output():
    seen: list[str] = []
    out, _err = _drain(_Child(THINK * 50 + TURN + WRITE * 50), timeout=5, on_line=seen.append)
    assert out == TURN
    assert len(seen) == 101


def test_the_streamed_command_asks_for_partial_messages(monkeypatch, tmp_path: Path):
    class _Spawned(BaseException):
        pass

    seen: dict = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = list(cmd)
        raise _Spawned()

    monkeypatch.setattr(code_agent.owned_process, "popen", fake_popen)
    monkeypatch.setenv("BOSS_CAPTURE_AGENT_TURNS", "1")
    monkeypatch.setattr(code_agent, "record_agent_turns", lambda *a, **k: 0)
    import boss_engine.services.cli_harness as cli_harness
    monkeypatch.setattr(cli_harness, "claude_login_token", lambda settings: None)
    from boss_engine.config import get_settings
    with pytest.raises(_Spawned):
        code_agent.ClaudeCodeAgent(get_settings())._invoke("Build it.", tmp_path)
    assert "--include-partial-messages" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--output-format") + 1] == "stream-json"
