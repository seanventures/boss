"""Ordered rules for one build-agent tool call (BOSSAPP-815).

A dict of allow/ask/off is the same map as before. A list is ordered rules:
lower priority first, first match wins, and no match denies. Database, console,
scopes, rate limits and time windows are not this slice.
"""
from __future__ import annotations

import json
from pathlib import Path

import boss_engine.services.tool_gateway as tool_gateway
from boss_engine.services.tool_gateway import verdict


def _decide(name, policy, arguments=None):
    decide = getattr(tool_gateway, "decide", None)
    assert decide is not None, "decide is missing"
    if arguments is None:
        return decide(name, policy)
    return decide(name, policy, arguments)


def test_a_lower_priority_off_beats_a_later_allow() -> None:
    rules = [
        {"id": "later-allow", "priority": 10, "effect": "allow", "tool": "bash"},
        {"id": "earlier-off", "priority": 1, "effect": "off", "tool": "bash"},
    ]
    out = _decide("bash", rules)
    assert out["decision"] == "deny"
    assert out["matched"] == ["earlier-off"]
    assert "earlier-off" in out["explanation"]
    assert verdict("bash", rules) == "deny"


def test_a_priority_tie_keeps_list_order() -> None:
    rules = [
        {"id": "first-off", "priority": 5, "effect": "off", "tool": "bash"},
        {"id": "second-allow", "priority": 5, "effect": "allow", "tool": "bash"},
    ]
    out = _decide("bash", rules)
    assert out["decision"] == "deny"
    assert out["matched"] == ["first-off"]
    assert "first-off" in out["explanation"]


def test_an_argument_condition_distinguishes_two_calls_of_the_same_tool() -> None:
    rules = [
        {"id": "rm", "priority": 1, "effect": "off", "tool": "bash",
         "when": {"cmd": "rm", "force": True}},
        {"id": "other-bash", "priority": 2, "effect": "allow", "tool": "bash"},
    ]
    blocked = _decide("bash", rules, {"cmd": "rm", "force": True})
    allowed = _decide("bash", rules, {"cmd": "ls", "force": True})
    assert blocked["decision"] == "deny"
    assert blocked["matched"] == ["rm"]
    assert "rm" in blocked["explanation"]
    assert allowed["decision"] == "run"
    assert allowed["matched"] == ["other-bash"]
    assert verdict("bash", rules, {"cmd": "rm", "force": True}) == "deny"
    assert verdict("bash", rules, {"cmd": "ls", "force": True}) == "run"


def test_a_star_matches_any_tool_and_a_missing_when_is_enough() -> None:
    rules = [{"id": "all-ask", "priority": 1, "effect": "ask", "tool": "*"}]
    out = _decide("write_file", rules)
    assert out["decision"] == "wait"
    assert out["matched"] == ["all-ask"]
    assert "all-ask" in out["explanation"]
    assert verdict("write_file", rules) == "wait"


def test_a_when_key_absent_from_arguments_does_not_match() -> None:
    rules = [
        {"id": "needs-path", "priority": 1, "effect": "allow", "tool": "write_file",
         "when": {"path": "ran.py"}},
        {"id": "else-off", "priority": 2, "effect": "off", "tool": "write_file"},
    ]
    absent = _decide("write_file", rules, {})
    other = _decide("write_file", rules, {"content": "ran.py"})
    present = _decide("write_file", rules, {"path": "ran.py"})
    assert absent["matched"] == ["else-off"]
    assert absent["decision"] == "deny"
    assert other["matched"] == ["else-off"]
    assert present["matched"] == ["needs-path"]
    assert present["decision"] == "run"
    assert verdict("write_file", rules, None) == "deny"


def test_a_value_that_does_not_equal_does_not_match() -> None:
    rules = [
        {"id": "exact", "priority": 1, "effect": "off", "tool": "bash", "when": {"cmd": "ls"}},
        {"id": "rest", "priority": 2, "effect": "allow", "tool": "bash"},
    ]
    out = _decide("bash", rules, {"cmd": "ls -la"})
    assert out["matched"] == ["rest"]
    assert out["decision"] == "run"


def test_no_list_match_denies_and_names_that() -> None:
    rules = [{"id": "writes", "priority": 1, "effect": "allow", "tool": "write_file"}]
    out = _decide("bash", rules)
    assert out["decision"] == "deny"
    assert out["matched"] == []
    assert "no rule matched" in out["explanation"]
    assert verdict("bash", []) == "deny"


def test_an_unknown_effect_denies_and_still_counts_as_the_match() -> None:
    rules = [
        {"id": "weird", "priority": 1, "effect": "sometimes", "tool": "bash"},
        {"id": "later-allow", "priority": 2, "effect": "allow", "tool": "bash"},
    ]
    out = _decide("bash", rules)
    assert out["decision"] == "deny"
    assert out["matched"] == ["weird"]
    assert "weird" in out["explanation"]
    assert verdict("bash", rules) == "deny"


def test_a_dict_allow_ask_and_off_are_unchanged() -> None:
    assert verdict("bash", {"bash": "allow"}) == "run"
    assert verdict("other", {"bash": "off"}) == "run"
    assert verdict("bash", {"bash": "ask"}) == "wait"
    assert verdict("bash", {"bash": "off"}) == "deny"
    allow = _decide("bash", {"bash": "allow"})
    ask = _decide("bash", {"bash": "ask"})
    off = _decide("bash", {"bash": "off"})
    missing = _decide("other", {"bash": "off"})
    assert allow == {"decision": "run", "matched": [], "explanation": "allow"}
    assert ask == {"decision": "wait", "matched": [], "explanation": "ask"}
    assert off == {"decision": "deny", "matched": [], "explanation": "off"}
    assert missing == {"decision": "run", "matched": [], "explanation": "allow"}


def test_an_unknown_dict_word_denies_and_is_named() -> None:
    out = _decide("bash", {"bash": " Yolo "})
    assert out["decision"] == "deny"
    assert out["matched"] == []
    assert out["explanation"] == "yolo"
    assert verdict("bash", {"bash": "yolo"}) == "deny"


def test_a_policy_that_is_neither_dict_nor_list_runs() -> None:
    for policy in (None, "allow", 0, 3.5):
        out = _decide("bash", policy)
        assert out["decision"] == "run"
        assert out["matched"] == []
        assert out["explanation"] == "allow"
        assert verdict("bash", policy) == "run"


def test_an_empty_dict_value_is_allow() -> None:
    assert verdict("bash", {"bash": ""}) == "run"
    assert _decide("bash", {"bash": ""}) == {
        "decision": "run", "matched": [], "explanation": "allow",
    }


def test_verdict_returns_the_decide_decision_and_the_two_argument_call_works() -> None:
    policy = {"bash": "ask"}
    assert verdict("bash", policy) == _decide("bash", policy)["decision"] == "wait"
    rules = [{"id": "off", "priority": 1, "effect": "off", "tool": "bash"}]
    assert verdict("bash", rules) == _decide("bash", rules)["decision"] == "deny"


def test_the_build_loop_passes_parsed_arguments(tmp_path: Path, monkeypatch) -> None:
    """An off rule conditioned on the real arguments must stop the write.

    A list used to be treated as no policy (run). Passing nothing for the
    arguments would miss ``when`` and fall through to the later allow.
    """
    from boss_engine.services.code_agent import NativeCodeAgent

    monkeypatch.setattr("boss_engine.services.code_agent.record_agent_turns", lambda *a, **k: None)
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call", lambda *a, **k: None)

    class _ScriptedLLM:
        def __init__(self) -> None:
            self._turns = [
                {
                    "content": None,
                    "tool_calls": [{
                        "id": "c1",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "ran.py", "content": "ran = True\n"}),
                    }],
                    "raw": None,
                },
                {"content": "stopped", "tool_calls": None, "raw": None},
            ]
            self.calls: list[dict] = []

        def create_chat_completion(self, messages, tools=None, tool_choice="auto",
                                   model_override=None, redact=False):
            self.calls.append({"messages": list(messages)})
            if not self._turns:
                return {"content": "done", "tool_calls": None, "raw": None}, {"model": "test"}
            return self._turns.pop(0), {"model": "test"}

    from boss_engine.config import Settings

    agent = NativeCodeAgent.__new__(NativeCodeAgent)
    agent.settings = Settings(BOSS_CODE_AGENT_CONTEXT_FILES=0)
    agent._llm = _ScriptedLLM()
    agent.tool_policy = [
        {"id": "off-ran", "priority": 1, "effect": "off", "tool": "write_file",
         "when": {"path": "ran.py"}},
        {"id": "allow-rest", "priority": 2, "effect": "allow", "tool": "*"},
    ]
    out = agent.run_role("create ran.py", tmp_path)

    assert not (tmp_path / "ran.py").exists()
    assert out["awaiting_approval"] is False
    told = [m for m in agent._llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert told and "did not run" in told[0]["content"]
