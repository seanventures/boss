"""A build's writing stages may fan out to parallel sub-agents, and the sub-agents stay inside
the same sandbox as their parent.

A HelpDesk AI build spent 32 minutes writing ~120 files one at a time. The CLI can run
sub-agents in parallel (its `Agent` tool), but BOSS never allowed it. Now a writing stage may,
within build_speed. The hard requirement is that a sub-agent can do nothing its parent cannot:
- it runs INSIDE the same CLI process, so its process, cwd, uid and environment are unchanged;
- it has the parent's --allowedTools and permission mode;
- the network tools are refused outright;
- a reviewer never fans out (review independence).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services import code_agent
from boss_engine.services.code_agent import (SUB_AGENT_DENIED_TOOLS, SUB_AGENT_TOOL,
                                             ClaudeCodeAgent, sub_agent_rule, sub_agent_tools)


class _Spawned(BaseException):
    """Stops _invoke at the moment it would start the CLI."""


def _launch(monkeypatch, tmp_path: Path, **kwargs) -> dict:
    seen: dict = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["cwd"] = kw.get("cwd")
        seen["env"] = dict(kw.get("env") or {})
        raise _Spawned()

    monkeypatch.setattr(code_agent.owned_process, "popen", fake_popen)
    monkeypatch.setenv("BOSS_CAPTURE_AGENT_TURNS", "0")
    import boss_engine.services.cli_harness as cli_harness
    monkeypatch.setattr(cli_harness, "claude_login_token", lambda settings: None)
    agent = ClaudeCodeAgent(get_settings())
    with pytest.raises(_Spawned):
        agent._invoke("Build the thing.", tmp_path, **kwargs)
    return seen


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1] if name in cmd else ""


def test_the_sub_agent_tool_is_added_once():
    assert sub_agent_tools("Read,Edit,Bash") == f"Read,Edit,Bash,{SUB_AGENT_TOOL}"
    assert sub_agent_tools(f"Read,{SUB_AGENT_TOOL}") == f"Read,{SUB_AGENT_TOOL}"


def test_a_writing_stage_fans_out_and_the_rule_says_how_many(monkeypatch, tmp_path):
    seen = _launch(monkeypatch, tmp_path, parallel_agents=3)
    allowed = _flag(seen["cmd"], "--allowedTools").split(",")
    assert SUB_AGENT_TOOL in allowed
    assert "up to 3 parallel sub-agents" in seen["cmd"][-1]


def test_a_sub_agent_cannot_reach_what_the_parent_cannot(monkeypatch, tmp_path):
    """Same process, same place, same identity, same permissions: only the tool list grows."""
    solo = _launch(monkeypatch, tmp_path)
    fan = _launch(monkeypatch, tmp_path, parallel_agents=3)
    # Same working directory and the same environment: the sandbox, uid and network fence are
    # properties of the process, and the sub-agents run inside it.
    assert fan["cwd"] == solo["cwd"] == str(tmp_path)
    assert fan["env"] == solo["env"]
    # The same permission mode, the same launcher (the sandbox wrapper and priority): only the
    # tool list and the refusal list differ.
    assert _flag(fan["cmd"], "--permission-mode") == _flag(solo["cmd"], "--permission-mode")
    assert fan["cmd"][: fan["cmd"].index("-p")] == solo["cmd"][: solo["cmd"].index("-p")]
    # Network tools are refused outright, so a general-purpose sub-agent cannot ask for them.
    assert _flag(fan["cmd"], "--disallowedTools") == SUB_AGENT_DENIED_TOOLS
    allowed = set(_flag(fan["cmd"], "--allowedTools").split(","))
    assert not allowed & set(SUB_AGENT_DENIED_TOOLS.split(","))
    assert allowed - set(_flag(solo["cmd"], "--allowedTools").split(",")) == {SUB_AGENT_TOOL}


def test_a_reviewer_never_fans_out(monkeypatch, tmp_path):
    seen = _launch(monkeypatch, tmp_path, read_only=True, parallel_agents=6)
    assert SUB_AGENT_TOOL not in _flag(seen["cmd"], "--allowedTools").split(",")
    assert "--disallowedTools" not in seen["cmd"]
    assert "PARALLEL SUB-AGENTS" not in seen["cmd"][-1]


def test_no_fan_out_is_exactly_the_old_command(monkeypatch, tmp_path):
    seen = _launch(monkeypatch, tmp_path, parallel_agents=0)
    assert SUB_AGENT_TOOL not in _flag(seen["cmd"], "--allowedTools").split(",")
    assert "--disallowedTools" not in seen["cmd"]


def test_the_rule_tells_the_parent_to_integrate_and_run_every_test():
    rule = sub_agent_rule(4)
    assert "up to 4" in rule and "run the full build and every test yourself" in rule
    assert "Two sub-agents never write the same file" in rule


def test_the_machine_caps_what_a_setting_asks_for(monkeypatch):
    from boss_engine.services import project_settings as ps

    # build_streams_min/max are unset here: Build speed sets the max.
    monkeypatch.setattr(ps, "effective", lambda c, p, s, key: {"build_parallelism": 0,
                                                               "build_speed": "fastest"}.get(key))
    monkeypatch.setattr(ps, "machine_parallelism", lambda: 2)
    assert ps.build_parallelism(None, "p", object()) == 2
    monkeypatch.setattr(ps, "machine_parallelism", lambda: 12)
    assert ps.build_parallelism(None, "p", object()) == 6
    monkeypatch.setattr(ps, "effective", lambda c, p, s, key: {"build_parallelism": 0,
                                                               "build_speed": "balanced"}.get(key))
    assert ps.build_parallelism(None, "p", object()) == 3


def test_the_machine_answer_is_never_zero():
    from boss_engine.services.project_settings import machine_parallelism

    assert machine_parallelism() >= 1
