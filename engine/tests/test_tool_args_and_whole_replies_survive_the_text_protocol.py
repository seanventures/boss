"""A text-protocol tool call keeps its arguments, a call missing one is refused, and the
model's whole answer is stored.

Ask BOSS on northgate-gateway, 2026-11-20 ~21:35 UTC, claude_code / claude-sonnet-5:

1. Sonnet wrote `{"name": "find_tools", "query": "deploy a stack target for the first time"}` —
   the argument beside the name, not under `arguments`. The CLI parser read only `arguments`, ran
   find_tools with `{}`, the search returned nothing (twice), and the agent told the operator no
   tool deploys a stack for the first time. `deploy_app` does.
2. The final answer quoted a word unescaped (`the app's "Deploy" control`) and wrote paragraphs
   with raw newlines, so the JSON did not parse; the salvage read `"final"` up to its first
   unescaped quote and the stored reply ended mid-sentence at "(the app's".

Both are fixed once, for every CLI text engine (Claude Code, Codex — one CliChatBrain parser) and
every tool (the registry's door)."""
from __future__ import annotations

import json

from boss_engine.agent.engine_agent import _calls_meant
from boss_engine.services import protocol_reply
from boss_engine.services.claude_cli_adapter import CliChatBrain
from boss_engine.services.codex_cli_adapter import CodexCliAdapter
from boss_engine.tools.builtin import build_discuss_read_registry, register_find_tools
from boss_engine.tools.registry import ToolRegistry

#: The incident's tool call, verbatim in shape.
FLAT_CALL = ('{"thought": "find the tool that deploys a stack the first time", "tool_calls": '
             '[{"name": "find_tools", "query": "deploy a stack target for the first time"}]}')

#: The incident's final answer, in shape: raw newlines, then an unescaped quoted word, then more.
TAIL = ("from here, so this is the one thing only you can set in motion.\n\n"
        "Sequence that unblocks this: you trigger the first deploy → I confirm the path.")
CUT_FINAL = ('{"thought": "Answer all three parts.", "final": "**What I tried first:** '
             '`inspect_host` rejected three calls.\n\nStanding up the stack is an operator action '
             '(the app\'s "Deploy" control in the console) — I can\'t trigger it ' + TAIL + '"}')


def _brain(cls, reply: str, monkeypatch):
    brain = cls.__new__(cls)
    monkeypatch.setattr(brain, "_invoke", lambda prompt: {"result": reply}, raising=False)
    monkeypatch.setattr(brain, "_usage", lambda data: {}, raising=False)
    monkeypatch.setattr(brain, "_build_prompt", lambda m, t: "p", raising=False)
    return brain


TOOLS = [{"type": "function", "function": {"name": "find_tools", "parameters": {}}}]


def test_a_flattened_tool_call_keeps_its_arguments_on_every_cli_engine(monkeypatch):
    for cls in (CliChatBrain, CodexCliAdapter):
        msg, _ = _brain(cls, FLAT_CALL, monkeypatch).create_chat_completion(
            [{"role": "user", "content": "q"}], tools=TOOLS)
        (call,) = msg["tool_calls"]
        assert json.loads(call["arguments"]) == {
            "query": "deploy a stack target for the first time"}, (cls.__name__, call)
        raw = msg["raw"]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(raw)["query"].startswith("deploy"), cls.__name__


def test_every_place_a_model_puts_arguments_is_read():
    want = {"query": "first deploy"}
    for call in ({"name": "t", "arguments": want}, {"name": "t", "arguments": json.dumps(want)},
                 {"name": "t", "args": want}, {"name": "t", "input": want},
                 {"name": "t", "parameters": want}, {"name": "t", "query": "first deploy"},
                 {"name": "t", "arguments": {}, "query": "first deploy"}):
        got = protocol_reply.call_arguments(call)
        assert (json.loads(got) if isinstance(got, str) else got) == want, call
    assert protocol_reply.call_arguments({"name": "t"}) == {}
    # the loop's own reading of a structure written as the answer shares it
    (meant,) = _calls_meant(json.loads(FLAT_CALL))
    assert json.loads(meant["arguments"])["query"].startswith("deploy")


def test_a_call_missing_a_required_argument_is_refused_with_what_to_send():
    reg = ToolRegistry()
    register_find_tools(reg)
    for args in ({}, {"query": None}, {"query": "   "}):
        out = reg.invoke("find_tools", None, project_id="p", actor="a", args=args)
        assert out["ok"] is False and out["error"] == "missing_required_arguments", (args, out)
        assert out["missing"] == ["query"] and "query" in out["takes"]
        assert '"arguments"' in out["message"] and "find_tools" in out["message"]
        assert "tools" not in out, "a refused call never looks like an empty search"
    # the incident's search, with its query, finds the deploy tool in the chat registry
    chat = build_discuss_read_registry()
    found = chat.invoke("find_tools", None, project_id="p", actor="a",
                        args={"query": "first deploy"})
    assert found["ok"] is True and "deploy_app" in found["names"], found


def test_the_whole_final_answer_is_read_when_the_json_does_not_parse(monkeypatch):
    got = protocol_reply.parse(CUT_FINAL)
    assert got["thought"] == "Answer all three parts."
    assert got["final"].endswith(TAIL), got["final"][-120:]
    assert '(the app\'s "Deploy" control in the console)' in got["final"]
    msg, _ = _brain(CliChatBrain, CUT_FINAL, monkeypatch).create_chat_completion(
        [{"role": "user", "content": "q"}], tools=TOOLS)
    assert msg["content"].endswith(TAIL) and not msg["tool_calls"]
    # a thought that quotes a word unescaped still ends where the next key starts
    two = '{"thought": "the "Deploy" button", "final": "done."}'
    assert protocol_reply.parse(two) == {"thought": 'the "Deploy" button', "final": "done."}
    # a paragraph break inside a well-formed string is text, not a reason to salvage
    assert protocol_reply.parse('{"final": "one.\n\ntwo."}') == {"final": "one.\n\ntwo."}
