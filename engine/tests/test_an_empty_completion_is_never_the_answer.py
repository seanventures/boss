"""A turn must not end with nothing, however the model misbehaves.

An operator asked BOSS about an error in an app BOSS itself had built — a specific, answerable
question about a specific run — and got back:

    "I could not answer that: the model returned an empty answer."

There was no provider outage. The agent loop exited on the first completion that carried
neither text nor a tool call:

    if not tool_calls:
        final_text = msg.get("content") or ""
        break

`break` skips a for/else, so the reserved final answer step and the fallback beneath it were
BOTH bypassed, and every tool result gathered on the way was thrown away. The router received
an empty string and rendered it as a failure — about a question BOSS may already have done the
work to answer.

Two properties are pinned here, and neither one is about prompting:
  * an empty completion is a hiccup, so it is PRESSED again rather than accepted;
  * whatever happens, `reply` is never empty — worst case it reports what was actually checked,
    which is evidence an operator can verify.
"""
from __future__ import annotations

from boss_engine.agent import EngineAgent
from boss_engine.tools.registry import ToolRegistry, ToolSpec


class ScriptedAdapter:
    """Yields queued completions; records how many times it was asked."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.last_messages = list(messages)
        # Running past the script means the loop asked more times than expected; keep answering
        # empty so the test fails on the ASSERTION rather than on an IndexError.
        msg = self._scripted.pop(0) if self._scripted else _empty()
        return msg, {"total_tokens": 0}


def _empty():
    """What the provider sent: no text, no tool calls, no error."""
    return {"content": "", "tool_calls": None, "raw": {"role": "assistant", "content": ""}}


def _text(body):
    return {"content": body, "tool_calls": None, "raw": {"role": "assistant", "content": body}}


def _call(name, args_json="{}", cid="call_1"):
    return {
        "content": None,
        "tool_calls": [{"id": cid, "name": name, "arguments": args_json}],
        "raw": {"role": "assistant", "content": None,
                "tool_calls": [{"id": cid, "type": "function",
                                "function": {"name": name, "arguments": args_json}}]},
    }


def _agent(scripted):
    reg = ToolRegistry()
    reg.register(ToolSpec("get_code_task", "r", {"type": "object"},
                          lambda ctx, **k: {"ok": True, "status": "succeeded",
                                            "title": "Payment Processor"},
                          kind="read", permission="system:read"))
    reg.register(ToolSpec("list_previews", "r", {"type": "object"},
                          lambda ctx, **k: {"ok": True, "previews": [{"id": "prev_1"}]},
                          kind="read", permission="system:read"))
    agent = EngineAgent(ctx=object(), registry=reg)
    agent._adapter = ScriptedAdapter(scripted)
    return agent, agent._adapter


class TestAnEmptyCompletionIsRetried:
    def test_the_turn_recovers_when_the_next_completion_has_text(self):
        """THE REPORTED BUG. One empty completion used to end the turn with "".

        The operator's question was answerable; the provider simply returned nothing once.
        """
        agent, adapter = _agent([_empty(), _text("The app rejects requests without X-Request-ID.")])
        turn = agent.chat(project_id="p", actor="u",
                          message="I get ❌ X-Request-ID header is required on the preview")
        assert turn.reply == "The app rejects requests without X-Request-ID."
        assert adapter.calls == 2, "the empty completion must be retried, not accepted"

    def test_the_model_is_told_its_message_was_empty(self):
        """Pressing without saying why invites the same empty message again."""
        agent, adapter = _agent([_empty(), _text("done")])
        agent.chat(project_id="p", actor="u", message="q")
        nudges = [m for m in adapter.last_messages
                  if m.get("role") == "user" and "empty" in str(m.get("content", ""))]
        assert nudges, "the retry must name the problem it is retrying"

    def test_evidence_already_gathered_survives_an_empty_completion(self):
        """The old `break` discarded the trace — the very thing that answers the question."""
        agent, _ = _agent([_call("get_code_task", '{"run_id":"crun_cef8b0f4"}'),
                           _empty(),
                           _text("That run finished; the 400 comes from the app's own guard.")])
        turn = agent.chat(project_id="p", actor="u", message="why did the preview 400?")
        assert [s["tool"] for s in turn.trace] == ["get_code_task"]
        assert "app's own guard" in turn.reply


class TestTheReplyIsNeverEmpty:
    def test_a_model_that_never_writes_still_produces_a_report(self):
        """Every completion empty: the worst case must still tell the operator something."""
        agent, _ = _agent([_call("get_code_task", '{"run_id":"crun_cef8b0f4"}')]
                          + [_empty()] * 12)
        turn = agent.chat(project_id="p", actor="u", message="why did the preview 400?")
        assert turn.reply.strip(), "an empty reply is what the router renders as a failure"
        # It reports the work, so the operator can check it themselves.
        assert "get_code_task" in turn.reply

    def test_it_says_plainly_that_this_is_a_fault_on_our_side(self):
        """A report of tool calls without that framing reads as a deliberate non-answer."""
        agent, _ = _agent([_empty()] * 12)
        turn = agent.chat(project_id="p", actor="u", message="anything")
        assert turn.reply.strip()
        assert "provider fault" in turn.reply or "fault on my side" in turn.reply

    def test_a_normal_turn_is_untouched(self):
        """The fix must not cost a step or change a reply that was already fine."""
        agent, adapter = _agent([_text("all good")])
        turn = agent.chat(project_id="p", actor="u", message="status?")
        assert turn.reply == "all good"
        assert adapter.calls == 1
