"""A writing step either writes the answer or fails — it never passes on a tool attempt.

SEEN ON boss-1: the "Trend to content" example's assess step (custom step bound to
`model.generate_text`, on the Claude Code subscription) SUCCEEDED with

    {"text": "I'll check memory for context on who \"we\" are …\n\n**Tool: skill**"}

and the next step wrote "there's no angle here — the material is a stray tool-call fragment",
which went into the review email. An earlier run's assess text was just "Read".

THE CAUSE: a one-shot generation ran `claude -p` under Claude Code's OWN system prompt — an
agentic coding persona whose "auto memory" section says when to go and read memories — with its
MCP servers still loaded (`--tools ""` drops the built-ins only). Asked what "we" have standing
to say, the model went to check memory; the CLI's result said so (`permission_denials`, a second
turn, or `max_turns`) and BOSS read only `result`, so the preamble became the answer.

These tests run the real adapter against a fake `claude` that behaves the way the real one did:
under Claude Code's prompt or with MCP loaded it reaches for a tool; on BOSS's own short system
prompt with nothing loaded it answers.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from boss_engine.services import model_routing
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter, FallingBackAdapter
from boss_engine.services.model_failures import NO_DIRECT_ANSWER, explain
from boss_engine.services.skill_executors import execute_model_generate_text

PREAMBLE = 'I\'ll check memory for context on who "we" are.\n\n**Tool: skill**'
ANSWER = "WHAT IT IS: developers posting their AI coding setups. OURS OR NOT: yes."

FAKE_CLAUDE = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
json.dump(argv, open(os.environ["FAKE_ARGV"], "w"))
mode = os.environ.get("FAKE_MODE", "real")
# What the real CLI did: Claude Code's own prompt invites a memory/skill look-up, and MCP
# servers loaded make one callable. BOSS's own prompt with nothing loaded does not.
invited = "--system-prompt" not in argv or "--strict-mcp-config" not in argv
denied = [{"tool_name": "mcp__boss__recall", "tool_use_id": "toolu_1", "tool_input": {}}]
if mode == "max_turns":
    result = {"type": "result", "subtype": "error_max_turns", "is_error": True,
              "stop_reason": "tool_use", "terminal_reason": "max_turns", "num_turns": 2,
              "permission_denials": denied, "errors": ["Reached maximum number of turns (2)"]}
    code = 1
elif mode == "denied" or invited:
    result = {"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "terminal_reason": "completed", "num_turns": 2,
              "permission_denials": denied, "result": {preamble!r}}
    code = 0
else:
    result = {"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "terminal_reason": "completed", "num_turns": 1,
              "permission_denials": [], "result": {answer!r}}
    code = 0
result.update({"session_id": "s1", "usage": {"input_tokens": 9, "output_tokens": 5},
               "modelUsage": {"claude-sonnet-5": {}}, "total_cost_usd": 0.001})
fmt = argv[argv.index("--output-format") + 1]
if fmt == "stream-json":
    for piece in (result.get("result") or "").split(" "):
        sys.stdout.write(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta",
                                                     "text": piece + " "}}}) + "\n")
    sys.stdout.write(json.dumps(result) + "\n")
else:
    sys.stdout.write(json.dumps(result))
sys.exit(code)
'''


@pytest.fixture()
def claude(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE.replace("{python}", sys.executable)
                      .replace("{preamble!r}", repr(PREAMBLE))
                      .replace("{answer!r}", repr(ANSWER)))
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv.json"))
    monkeypatch.delenv("FAKE_MODE", raising=False)
    settings = SimpleNamespace(repo_root=tmp_path, claude_cli_timeout=20, claude_cli_model=None,
                               claude_cli_path=str(binary))
    adapter = ClaudeCliAdapter(settings)
    monkeypatch.setattr(adapter, "_child_env", lambda: dict(os.environ))
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *a, **k: adapter)
    return adapter, (lambda: json.loads((tmp_path / "argv.json").read_text()))


def _generate(**inputs):
    context = SimpleNamespace(connection=None, settings=None, project_id="p")
    return execute_model_generate_text(context, {"prompt": "Assess this trend.", **inputs})


def test_a_writing_step_answers_on_boss_s_prompt_with_no_toolbelt(claude):
    adapter, argv = claude
    out = _generate(context={"dossier": "people post their AI coding setups"})
    assert out["text"] == ANSWER, "the step's text was a tool attempt, not the answer"
    cmd = argv()
    assert "--system-prompt" in cmd, "a generation ran under Claude Code's own system prompt"
    system = cmd[cmd.index("--system-prompt") + 1]
    assert system == ClaudeCliAdapter.GENERATION_SYSTEM
    assert "--strict-mcp-config" in cmd, "the CLI's own MCP servers stayed callable"
    assert "--disable-slash-commands" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""


def test_a_step_s_own_instructions_are_its_system_prompt(claude):
    adapter, argv = claude
    _generate(system="You are the channel's editor.")
    cmd = argv()
    assert cmd[cmd.index("--system-prompt") + 1] == "You are the channel's editor."


def test_a_tool_attempt_is_a_model_failure_not_the_step_s_text(claude, monkeypatch):
    """Even on the lean form: if the CLI says the model reached for a tool, the text it
    returned is not an answer, whatever it says."""
    monkeypatch.setenv("FAKE_MODE", "denied")
    with pytest.raises(RuntimeError) as caught:
        _generate()
    assert NO_DIRECT_ANSWER in str(caught.value)
    assert "mcp__boss__recall" in str(caught.value)
    said = explain(caught.value)
    assert said["kind"] == "no_direct_answer" and said["retry"] is True


def test_a_run_cut_at_the_turn_limit_is_the_model_s_failure_not_an_outage(claude, monkeypatch):
    """`error_max_turns` exits 1. Read as "exited 1" it looked like the backend was down, and
    the falling-back adapter would ask another brain the same question."""
    adapter, _ = claude
    monkeypatch.setenv("FAKE_MODE", "max_turns")
    with pytest.raises(RuntimeError) as caught:
        adapter.create_text_response("Assess this trend.")
    assert NO_DIRECT_ANSWER in str(caught.value) and "turn limit" in str(caught.value)
    assert not FallingBackAdapter(adapter, SimpleNamespace())._unavailable(caught.value)


def test_a_talk_streams_on_the_same_lean_form_and_is_judged_the_same(claude, monkeypatch):
    adapter, argv = claude
    pieces = list(adapter.stream_text_response("Hi", system_prompt="You are Vera."))
    assert "".join(p for p in pieces if isinstance(p, str)).strip() == ANSWER
    cmd = argv()
    assert cmd[cmd.index("--system-prompt") + 1] == "You are Vera."
    assert "--strict-mcp-config" in cmd
    monkeypatch.setenv("FAKE_MODE", "denied")
    with pytest.raises(RuntimeError, match=NO_DIRECT_ANSWER):
        list(adapter.stream_text_response("Hi", system_prompt="You are Vera."))


def test_the_tool_loop_also_loads_none_of_the_cli_s_own_mcp_servers(claude):
    """BOSS's JSON tool loop keeps Claude Code's prompt and its room for one stray attempt, but
    nothing of the CLI's own is callable there either."""
    adapter, argv = claude
    adapter.create_chat_completion(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}])
    cmd = argv()
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert "--system-prompt" not in cmd and int(cmd[cmd.index("--max-turns") + 1]) >= 4
