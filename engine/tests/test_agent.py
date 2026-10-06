"""Engine-native agent tests: tool-calling loop + write-gating (no DB, fake LLM)."""
from __future__ import annotations

from boss_engine.agent import EngineAgent
from boss_engine.tools.registry import ToolRegistry, ToolSpec


class FakeAdapter:
    """Scripted create_chat_completion: yields queued messages in order."""

    def __init__(self, scripted):
        self._scripted = list(scripted)

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        msg = self._scripted.pop(0)
        return msg, {"total_tokens": 0}


def _tool_call_msg(name, args_json="{}", cid="call_1"):
    return {
        "content": None,
        "tool_calls": [{"id": cid, "name": name, "arguments": args_json}],
        "raw": {"role": "assistant", "content": None,
                "tool_calls": [{"id": cid, "type": "function",
                                "function": {"name": name, "arguments": args_json}}]},
    }


def _final_msg(text):
    return {"content": text, "tool_calls": None, "raw": {"role": "assistant", "content": text}}


def _registry(calls):
    reg = ToolRegistry()
    reg.register(ToolSpec("project_status", "r", {"type": "object"},
                          lambda ctx, **k: {"total_running": 3}, kind="read", permission="system:read"))

    def do_write(ctx, **k):
        calls.append(k["args"])
        return {"run_id": "crun_x"}

    # "redeploy" is the write used to test the allow_writes GATE: it is an ordinary write, so
    # allow_writes alone decides whether it runs. "create_code_task" is deliberately also
    # registered because it is PROPOSE_ONLY — it must NOT run even when writes are allowed, and
    # the test below pins that difference. Using one tool for both properties hid the
    # distinction and is why these tests broke when propose-only landed.
    reg.register(ToolSpec("redeploy", "w", {"type": "object"}, do_write, kind="write", permission="system:read"))
    reg.register(ToolSpec("create_code_task", "w", {"type": "object"}, do_write, kind="write", permission="system:read"))
    return reg


def _agent(scripted, calls):
    ag = EngineAgent(ctx=object(), registry=_registry(calls))
    ag._adapter = FakeAdapter(scripted)  # bypass the real LLM adapter
    return ag


def test_read_tool_flow():
    calls = []
    ag = _agent([_tool_call_msg("project_status"), _final_msg("3 tasks are running.")], calls)
    turn = ag.chat(project_id="p", actor="u", message="how many running?")
    assert turn.reply == "3 tasks are running."
    assert turn.trace[0]["tool"] == "project_status"
    assert turn.trace[0]["result"]["total_running"] == 3
    assert turn.pending_writes == []


def test_write_gated_by_default():
    calls = []
    ag = _agent([_tool_call_msg("redeploy", '{"app":"x"}'),
                 _final_msg("I drafted a redeploy; confirm to run.")], calls)
    turn = ag.chat(project_id="p", actor="u", message="redeploy x", allow_writes=False)
    assert calls == []  # the write handler NEVER ran
    assert turn.pending_writes and turn.pending_writes[0]["tool"] == "redeploy"
    assert turn.trace[0]["result"]["status"] == "confirmation_required"


def test_write_runs_when_allowed():
    calls = []
    ag = _agent([_tool_call_msg("redeploy", '{"app":"x"}'),
                 _final_msg("Redeployed.")], calls)
    turn = ag.chat(project_id="p", actor="u", message="redeploy x", allow_writes=True)
    # The model's own arguments, plus the ambient markers the loop stamps on every call
    # (`_writes_allowed` records who authorised the turn — see engine_agent). Asserting on the
    # model's arguments rather than on the whole dict keeps this about the write gate.
    assert [{k: v for k, v in c.items() if not k.startswith("_")} for c in calls] \
        == [{"app": "x"}]  # the write handler ran once
    assert calls[0]["_writes_allowed"] is True
    assert turn.trace[0]["result"]["run_id"] == "crun_x"
    assert turn.pending_writes == []


def test_expensive_write_is_offered_even_when_writes_are_allowed():
    """The deploy-became-a-rebuild bug: allow_writes=True was enough to start a build. Starting
    expensive work is now an offer regardless, so allow_writes governs ordinary writes only."""
    calls = []
    ag = _agent([_tool_call_msg("create_code_task", '{"title":"x"}'),
                 _final_msg("I can start that build if you want it.")], calls)
    turn = ag.chat(project_id="p", actor="u", message="build x", allow_writes=True)
    assert calls == []  # NOT started, despite writes being allowed
    assert turn.trace[0]["result"]["proposed"] is True
    assert turn.pending_writes and turn.pending_writes[0]["tool"] == "create_code_task"


def test_unknown_tool_is_handled():
    calls = []
    ag = _agent([_tool_call_msg("no_such_tool"), _final_msg("done")], calls)
    turn = ag.chat(project_id="p", actor="u", message="x")
    assert turn.trace[0]["result"]["ok"] is False


class _HiccupThenScripted:
    """First N-1 completions follow the script; one later call is a 529, then the script resumes.

    That is the live shape: the model asked for a write, the write ran, and the NEXT
    completion — the one that would tell the operator what happened — hit capacity.
    """

    def __init__(self, scripted, *, fail_on_call: int):
        self._scripted = list(scripted)
        self.fail_on_call = fail_on_call
        self.calls = 0

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("529 overloaded_error")
        return self._scripted.pop(0), {"total_tokens": 0}


def test_a_provider_hiccup_after_a_write_does_not_run_the_write_again(monkeypatch):
    """A 529 is a model blip, not a reason to ship twice.

    The chat path retried the WHOLE turn when Anthropic returned 529. Redeploy, start_preview,
    write_db and decide all execute directly (allow_writes=True). So: operator says "fix the
    deployment"; the agent calls redeploy; the reply completion 529s; the turn starts over;
    redeploy runs again. A redeploy is a new deployment record aimed at the same host.

    Retry the model call. Do not replay the write.
    """
    monkeypatch.setattr("boss_engine.agent.transient.time.sleep", lambda _s: None)
    calls = []
    scripted = [_tool_call_msg("redeploy", '{"app":"x"}'), _final_msg("Redeployed.")]
    ag = EngineAgent(ctx=object(), registry=_registry(calls))
    ag._adapter = _HiccupThenScripted(scripted, fail_on_call=2)
    turn = ag.chat(project_id="p", actor="u", message="redeploy x", allow_writes=True)
    assert len(calls) == 1, "a capacity blip must not call redeploy again"
    assert calls[0]["app"] == "x"
    assert turn.reply == "Redeployed."
