"""A build agent's tool call is checked against policy before it runs.

Allow runs the tool. Ask does not: the session waits, and the tool's effect
never happens. Off does not run the tool either. Selectors, argument hashes,
always-allow rules and an audit log are later slices, not this one.
"""
from __future__ import annotations

import json
from pathlib import Path

from boss_engine.services.code_agent import NativeCodeAgent
from boss_engine.services.engine_contract import normalise_result


class _ScriptedLLM:
    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def create_chat_completion(self, messages, tools=None, tool_choice="auto",
                               model_override=None, redact=False):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            return {"content": "done", "tool_calls": None, "raw": None}, {"model": "test"}
        return self._turns.pop(0), {"model": "test"}


def _tool_turn(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "name": name, "arguments": json.dumps(args)}],
        "raw": None,
    }


def _agent(repo: Path, llm, policy: dict[str, str]) -> NativeCodeAgent:
    from boss_engine.config import Settings

    agent = NativeCodeAgent.__new__(NativeCodeAgent)
    agent.settings = Settings(BOSS_CODE_AGENT_CONTEXT_FILES=0)
    agent._llm = llm
    agent.tool_policy = policy
    return agent


def test_a_call_that_needs_approval_waits_and_does_not_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("boss_engine.services.code_agent.record_agent_turns", lambda *a, **k: None)
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call", lambda *a, **k: None)
    llm = _ScriptedLLM([
        _tool_turn("write_file", {"path": "ran.py", "content": "ran = True\n"}),
        {"content": "I wrote ran.py.", "tool_calls": None, "raw": None},
    ])
    out = _agent(tmp_path, llm, {"write_file": "ask"}).run_role("create ran.py", tmp_path)

    assert not (tmp_path / "ran.py").exists()
    assert out["awaiting_approval"] is True
    assert out["stop_reason"] == "awaiting_approval"
    assert out["pending_tool"] == "write_file"
    assert out["turns"] == 1
    assert len(llm._turns) == 1
    assert normalise_result(out, engine="native")["stop_reason"] == "awaiting_approval"


def test_a_disallowed_call_does_not_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("boss_engine.services.code_agent.record_agent_turns", lambda *a, **k: None)
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call", lambda *a, **k: None)
    llm = _ScriptedLLM([
        _tool_turn("write_file", {"path": "ran.py", "content": "ran = True\n"}),
        {"content": "I could not write it.", "tool_calls": None, "raw": None},
    ])
    out = _agent(tmp_path, llm, {"write_file": "off"}).run_role("create ran.py", tmp_path)

    assert not (tmp_path / "ran.py").exists()
    assert out["awaiting_approval"] is False
    assert out["turns"] == 2
    told = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert told and "did not run" in told[0]["content"]
