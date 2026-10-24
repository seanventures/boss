"""A model's decision structure is executed or re-asked, never shown as the answer.

Exam, 2026-10-24 (Metatron --print): BOSS's whole reply was the tool protocol's JSON —
`{"thought": "Stop retrying create_project. ...", "tool_calls": [{"name": "search_code",
"arguments": {...}, {...}]}` with one brace missing. The CLI parser took the first balanced object
INSIDE it (a tool call's `arguments`) for the reply, found no protocol keys and returned the raw
text; the loop took text-without-tool-calls for the answer. Fixed once (services/protocol_reply.py)
and enforced at three doors: the CLI adapter, the tool loop (any adapter), and the thread's reply.
"""
from __future__ import annotations

import json
import types

import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.config import get_settings
from boss_engine.services import protocol_reply
from boss_engine.services.agent_service import readable_reply
from boss_engine.services.claude_cli_adapter import CliChatBrain, _unwrapped_final
from boss_engine.tools.registry import ToolRegistry, ToolSpec

#: The exam's reply, verbatim in shape: the first call's `arguments` is never closed.
MALFORMED = ('{"thought": "Check the platform code first.", "tool_calls": [{"name": "search_code", '
             '"arguments": {"pattern": "def list_repositories", "repo": "boss"}, {"name": '
             '"search_code", "arguments": {"pattern": "pinned_project", "repo": "boss"}]}')
WELL_FORMED = json.dumps({"thought": "look", "tool_calls": [
    {"name": "search_code", "arguments": {"pattern": "x"}}]})


def test_the_protocol_object_is_read_at_its_start_and_a_malformed_one_is_salvaged():
    got = protocol_reply.parse(MALFORMED)
    assert got["thought"] == "Check the platform code first."
    assert [c["name"] for c in got["tool_calls"]] == ["search_code", "search_code"]
    assert got["tool_calls"][0]["arguments"] == {"pattern": "def list_repositories", "repo": "boss"}
    assert protocol_reply.parse("```json\n" + WELL_FORMED + "\n```")["tool_calls"]
    assert protocol_reply.parse('{"action": "answer", "confidence": 0.9}')
    # prose, and JSON that is no decision, are left alone
    for text in ("All three projects exist.", '{"count": 3}',
                 'The call was {"name": "x", "arguments": {}}', ""):
        assert protocol_reply.parse(text) is None, text


def test_the_cli_adapter_never_returns_the_raw_protocol(monkeypatch):
    brain = CliChatBrain.__new__(CliChatBrain)
    monkeypatch.setattr(brain, "_invoke", lambda prompt: {"result": MALFORMED}, raising=False)
    monkeypatch.setattr(brain, "_usage", lambda data: {}, raising=False)
    monkeypatch.setattr(brain, "_build_prompt", lambda m, t: "p", raising=False)
    tools = [{"type": "function", "function": {"name": "search_code", "parameters": {}}}]
    msg, _ = brain.create_chat_completion([{"role": "user", "content": "q"}], tools=tools)
    assert [c["name"] for c in msg["tool_calls"]] == ["search_code", "search_code"], \
        "the calls it meant are executed"
    msg, _ = brain.create_chat_completion([{"role": "user", "content": "q"}], tools=None)
    assert "{" not in msg["content"] and msg["protocol"]["tool_calls"], msg
    assert _unwrapped_final(MALFORMED) == "Check the platform code first."


class _Scripted:
    """Answers each chat completion from a script of plain texts (no native tool calls)."""

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.calls: list = []

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        self.calls.append({"tools": bool(tools), "last": messages[-1]})
        text = self.texts.pop(0)
        return {"content": text, "tool_calls": None,
                "raw": {"role": "assistant", "content": text}}, {}


def _agent(adapter, ran: list) -> EngineAgent:
    reg = ToolRegistry()
    reg.register(ToolSpec("search_code", "s", {"type": "object"},
                          lambda ctx, *, project_id, actor, args: ran.append(args) or {"hits": 1},
                          permission="project:read"))
    agent = EngineAgent(types.SimpleNamespace(settings=get_settings()), registry=reg,
                        adapter=adapter)
    agent.registry.invoke = lambda name, ctx, **kw: reg.get(name).handler(  # no identity here
        ctx, project_id=kw["project_id"], actor=kw["actor"], args=kw["args"])
    return agent


def test_a_tool_call_written_as_text_is_run_not_printed():
    ran: list = []
    adapter = _Scripted(MALFORMED, "Found it: list_repositories takes a project.")
    turn = _agent(adapter, ran).chat(project_id="p", message="q", max_steps=4)
    assert len(ran) == 2 and ran[0]["pattern"] == "def list_repositories"
    assert turn.reply == "Found it: list_repositories takes a project."


def test_a_structure_with_no_answer_is_asked_once_for_prose_then_reported_honestly():
    ran: list = []
    # at the last step (no tools) the model answers with the protocol again; asked once for prose
    adapter = _Scripted(WELL_FORMED, WELL_FORMED, "The three projects exist; none has a repo.")
    turn = _agent(adapter, ran).chat(project_id="p", message="q", max_steps=2)
    assert turn.reply == "The three projects exist; none has a repo."
    assert "plain prose" in str(adapter.calls[-1]["last"]["content"])
    # ...and if it still will not, the reply is the trace's honest report, never the JSON
    adapter = _Scripted(WELL_FORMED, WELL_FORMED, MALFORMED)
    turn = _agent(adapter, []).chat(project_id="p", message="q", max_steps=2)
    assert not protocol_reply.is_decision_structure(turn.reply) and "{" not in turn.reply, turn.reply


def test_the_threads_last_door_turns_any_structure_into_a_readable_reply():
    assert readable_reply('{"thought": "t", "final": "Done: 3 projects."}') == "Done: 3 projects."
    said = readable_reply(MALFORMED, [{"tool": "list_projects", "ok": True}])
    assert "{" not in said and "list_projects" in said
    assert readable_reply("plain answer") == "plain answer"


#: A reply BOSS actually printed (captures/v2_impact.txt, kg-demo-impact,
#: northpeak-bank-b): most of the "final" text read as plain prose with real paragraph
#: breaks, but two of them, and every quote in the whole reply, were still the JSON
#: escapes that stood for them — `\n\n` and `\"…\"` printed literally rather than a blank
#: line and a quoted phrase. Not a decision structure (it does not start with `{`), so the
#: old code returned it completely unprocessed.
CAPTURED_SHAPE = (
    "...the Due-strategy's `do...while(ignoreDueDateCheck && rerun)` loop (lines 69-180 "
    "of the Due-strategy class) can.\\n\\nSo, restated with this now confirmed rather than "
    "inferred:\\n\\n**What remains genuinely unread**: the 7 other \\\"structurally "
    "exposed\\\" bodies beyond Due-strategy."
)


def test_a_stray_json_escape_in_plain_prose_is_decoded():
    said = readable_reply(CAPTURED_SHAPE)
    assert "\\n" not in said and '\\"' not in said
    assert "\n\nSo, restated" in said
    assert '"structurally exposed"' in said


def test_the_real_captured_reply_comes_out_clean():
    import pathlib

    captured = pathlib.Path(
        "/Users/param/Downloads/boss-video-kg/captures/v2_impact.txt")
    if not captured.exists():
        pytest.skip("capture not present on this machine")
    said = readable_reply(captured.read_text(encoding="utf-8"))
    assert "\\n" not in said
    assert '\\"' not in said
    assert "\n\nSo, restated with this now confirmed" in said


def test_a_fenced_or_inline_code_span_is_never_touched():
    sample = ("Prose with a bug.\\n\\nSecond line.\n\n"
             "```python\nre.compile(r\"a\\nb\")\n```\n\n"
             "Inline `\\n` stays raw too.")
    said = readable_reply(sample)
    assert said.startswith("Prose with a bug.\n\nSecond line.")
    assert 're.compile(r"a\\nb")' in said  # inside the fence, untouched
    assert "Inline `\\n` stays raw too." in said  # inside the inline span, untouched


def test_a_reply_with_no_backslash_is_returned_unchanged():
    plain = "All three projects exist; none has a repo."
    assert readable_reply(plain) == plain


def test_every_threaded_reply_and_the_router_door_pass_the_last_door():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    service = (root / "engine/boss_engine/services/agent_service.py").read_text()
    chat = service[service.index("    def chat("):]
    assert chat.index("readable_reply(turn.reply") < chat.index('role="assistant"')
    main = (root / "apps/api/app/main.py").read_text()
    route = main[main.index('"/v1/intent/route"'):]
    route = route[: route.index("\n@app.", 10)]
    assert "readable_reply(turn.reply" in route


def test_one_backslash_n_in_prose_is_the_subject_not_debris():
    from boss_engine.services.protocol_reply import unescape_stray_json
    said = "Split each record on \\n and keep the header."
    assert unescape_stray_json(said) == said
    assert unescape_stray_json('He said \\"done\\".\\n\\nNext.') == 'He said "done".\n\nNext.'
