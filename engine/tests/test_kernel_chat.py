"""K6: CHAT RUNS ON THE KERNEL (BOSSAPP-961; HARNESS_KERNEL_DESIGN.md §8.1 K6, §8.2e).

The same scripted model is played through `EngineAgent.chat` twice — BOSS_KERNEL_CHAT off (the
old loop) and on (`kernel.run` with presets.engine_agent) — and the two turns must agree: the
same requests reach the model (messages and offered tools, byte-equal), and the AgentTurn a
caller reads is the same (reply, pending_writes, steps, exhausted, trace). The write hold, the
propose-only offers and the escalation guard all run through `_run_tool` on both paths, so the
behaviour moves whole rather than being reimplemented.

INTENDED DIFFERENCES, pinned here so they cannot widen (§8.2e):

  * a decision structure written as the final TEXT is read for its prose on the kernel path,
    but never re-asked and its "meant" calls never run — engines the kernel serves call tools
    natively, so the JSON-in-text protocol is residue (the CLI text brains, which still need
    it, keep the old loop until K8);
  * a provider's 400 on a malformed tool call is fed back on the kernel path (agent_loop's
    recovery, now everywhere) where the old chat loop raised — already pinned by
    test_kernel_conformance.TestRefusedToolCall;
  * a CLI text brain falls through: flag on or off, claude_code / claude_agent_sdk / codex
    chat runs today's loop unchanged.

The live cached-share proof (§4.3's ≥70% bar) is the skipped test at the bottom: it needs an
Anthropic API key and spends real money, so it runs on a keyed host, by hand.
"""
from __future__ import annotations

import json
import os

import pytest

from boss_engine.agent import EngineAgent
from boss_engine.agent.engine_agent import _CLI_TEXT_BRAINS
from boss_engine.kernel.scripted import Call, Empty, Fail, Say, ScriptedEngine
from boss_engine.tools.registry import ToolRegistry, ToolSpec

from .test_kernel_conformance import READ_SCHEMA, read_logs_result, same_requests

SYSTEM = "You are the test agent."
ASK = "Check the run."


def registry(written: list) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec("read_logs", "Read a run's logs.", READ_SCHEMA,
                          lambda ctx, project_id, actor, args: read_logs_result(args),
                          kind="read", permission="system:read"))

    def write(ctx, project_id, actor, args):
        written.append({k: v for k, v in args.items() if not str(k).startswith("_")})
        return {"ok": True, "run_id": "crun_x"}

    reg.register(ToolSpec("redeploy", "Ship the same build again.", {"type": "object"},
                          write, kind="write", permission="system:read"))
    reg.register(ToolSpec("create_code_task", "Start a build.", {"type": "object"},
                          write, kind="write", permission="system:read"))
    reg.register(ToolSpec("propose_action", "Offer a fix as a button.",
                          {"type": "object", "properties": {}},
                          lambda ctx, project_id, actor, args: {"ok": True, "proposed": True},
                          kind="read", permission="system:read"))
    return reg


def one_turn(monkeypatch, script, *, flag: str, written=None, **chat_kw):
    """One EngineAgent.chat turn on a fresh scripted engine, with the kernel flag pinned."""
    from boss_engine.agent import transient

    monkeypatch.setenv("BOSS_KERNEL_CHAT", flag)
    monkeypatch.setattr(transient.time, "sleep", lambda s: None)
    engine = ScriptedEngine(list(script))
    agent = EngineAgent(ctx=object(), registry=registry(written if written is not None else []))
    agent._adapter = engine
    turn = agent.chat(project_id="p", message=ASK, system=SYSTEM, **chat_kw)
    return turn, engine


def both(monkeypatch, script, **chat_kw):
    old_turn, old = one_turn(monkeypatch, script, flag="0", **chat_kw)
    new_turn, new = one_turn(monkeypatch, script, flag="1", **chat_kw)
    same_requests(old, new)
    assert old_turn.reply == new_turn.reply
    assert old_turn.steps == new_turn.steps
    assert old_turn.exhausted == new_turn.exhausted
    assert old_turn.pending_writes == new_turn.pending_writes
    assert [(t["tool"], t["args"]) for t in old_turn.trace] == \
        [(t["tool"], t["args"]) for t in new_turn.trace]
    return old_turn, new_turn


# ════ The EngineAgent scenarios, equal kernel-vs-old on the scripted engine ═══════════════════

class TestChatConformance:
    def test_a_read_chain_and_the_reserved_answer_step(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Call("read_logs", {"run": "r2"}),
                  Say("r1 and r2 both fail on auth.")]
        old_turn, new_turn = both(monkeypatch, script, max_steps=3)
        assert new_turn.reply == "r1 and r2 both fail on auth."
        assert new_turn.exhausted == "steps"
        assert new_turn.trace[0]["result"]["lines"] == ["run r1: 3 errors"]

    def test_a_transient_hiccup_is_retried_the_same_number_of_times(self, monkeypatch):
        script = [Fail(RuntimeError("529 overloaded_error")),
                  Fail(RuntimeError("502 bad gateway")), Say("third attempt answered")]
        old_turn, new_turn = both(monkeypatch, script, max_steps=6)
        assert new_turn.reply == "third attempt answered"

    def test_a_hiccup_that_will_not_pass_raises_on_both_paths(self, monkeypatch):
        exhausted = [Fail(RuntimeError("529 overloaded_error")) for _ in range(3)]
        with pytest.raises(RuntimeError, match="529"):
            one_turn(monkeypatch, exhausted, flag="0")
        with pytest.raises(RuntimeError, match="529"):
            one_turn(monkeypatch, exhausted, flag="1")

    def test_two_empty_completions_are_pressed_with_the_same_words(self, monkeypatch):
        script = [Empty(), Empty(), Say("third time lucky")]
        old_turn, new_turn = both(monkeypatch, script, max_steps=6)
        assert new_turn.reply == "third time lucky"

    def test_the_same_tool_failing_three_times_draws_the_same_nudge(self, monkeypatch):
        script = [Call("read_logs", {"run": 7}), Call("read_logs", {"run": 7}),
                  Call("read_logs", {"run": 7}), Say("I stopped retrying.")]
        # The arguments are INVALID on the kernel path (D3 refuses an integer where the schema
        # says string) and a tool ERROR on the old path (the tool never minds) — both count as
        # failures, so the nudge lands the same turn. Use a failing-but-valid call instead so
        # the transcripts stay byte-equal:
        failing = [Call("read_logs", {"run": ""}), Call("read_logs", {"run": ""}),
                   Call("read_logs", {"run": ""}), Say("I stopped retrying.")]

        def fails(ctx, project_id, actor, args):
            return {"ok": False, "error": "no such run", "tool": "read_logs"}

        def agent_with(flag):
            from boss_engine.agent import transient

            monkeypatch.setenv("BOSS_KERNEL_CHAT", flag)
            monkeypatch.setattr(transient.time, "sleep", lambda s: None)
            reg = ToolRegistry()
            reg.register(ToolSpec("read_logs", "Read logs.", READ_SCHEMA, fails,
                                  kind="read", permission="system:read"))
            engine = ScriptedEngine(list(failing))
            agent = EngineAgent(ctx=object(), registry=reg)
            agent._adapter = engine
            return agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=8), engine

        old_turn, old = agent_with("0")
        new_turn, new = agent_with("1")
        same_requests(old, new)
        assert old_turn.reply == new_turn.reply == "I stopped retrying."
        nudge = new.requests[3]["messages"][-1]["content"]
        assert nudge.startswith("SYSTEM: `read_logs` has now failed 3 times")
        del script  # documented above: why the scripted arguments changed

    def test_a_turn_budget_forces_the_answer_with_its_offer_button(self, monkeypatch):
        from boss_engine.services.turn_budget import TurnBudget

        script = [Call("read_logs", {"run": "r1"}),
                  Say("The fix is to restart; offering it.")]
        turns = []
        for flag in ("0", "1"):
            budget = TurnBudget(seconds=10_000, model_calls=2)
            turn, engine = one_turn(monkeypatch, script, flag=flag, max_steps=8, budget=budget)
            turns.append((turn, engine))
        (old_turn, old), (new_turn, new) = turns
        same_requests(old, new)
        assert old_turn.reply == new_turn.reply
        assert old_turn.exhausted == new_turn.exhausted == "budget"
        assert "propose_action ONCE in this same message" in \
            new.requests[-1]["messages"][-1]["content"]

    def test_pending_writes_hold_and_the_propose_only_offer(self, monkeypatch):
        """The write hold moves whole: an ordinary write is held as confirmation_required
        unless allow_writes; a PROPOSE_ONLY tool is offered even when writes are allowed."""
        script = [Call("redeploy", {"app": "x"}), Say("I drafted a redeploy; press to run.")]
        old_turn, new_turn = both(monkeypatch, script, max_steps=4, allow_writes=False)
        assert new_turn.pending_writes == [{"tool": "redeploy", "args": {"app": "x"}}]
        assert new_turn.trace[0]["result"]["status"] == "confirmation_required"

        ran: list = []
        turn, _ = one_turn(monkeypatch, script, flag="1", written=ran, max_steps=4,
                           allow_writes=True)
        assert ran == [{"app": "x"}] and turn.pending_writes == []

        expensive = [Call("create_code_task", {"title": "x"}),
                     Say("I can start that build if you want it.")]
        held: list = []
        turn, _ = one_turn(monkeypatch, expensive, flag="1", written=held, max_steps=4,
                           allow_writes=True)
        assert held == [], "PROPOSE_ONLY ran despite being an offer"
        assert turn.pending_writes == [{"tool": "create_code_task", "args": {"title": "x"}}]
        assert turn.trace[0]["result"]["proposed"] is True

    def test_an_escalation_nobody_asked_for_is_dropped_on_both_paths(self, monkeypatch):
        script = [Call("redeploy", {"app": "x", "force": True}), Say("done")]
        ran_old: list = []
        ran_new: list = []
        one_turn(monkeypatch, script, flag="0", written=ran_old, max_steps=4, allow_writes=True)
        one_turn(monkeypatch, script, flag="1", written=ran_new, max_steps=4, allow_writes=True)
        assert ran_old == ran_new == [{"app": "x"}], "force survived without the operator's words"

    def test_the_consoles_progress_labels_are_the_same_words_in_the_same_order(self, monkeypatch):
        """on_step still reaches the console: the kernel's on_event is mapped back to the old
        loop's phase and tool labels (design §2.8 'Streaming')."""
        script = [Call("read_logs", {"run": "r1"}), Say("r1 is clean.")]
        seen: dict = {}
        for flag in ("0", "1"):
            labels: list = []
            one_turn(monkeypatch, script, flag=flag, max_steps=4,
                     focus="workflow step wfr_1/preview", on_step=labels.append)
            seen[flag] = labels
        assert seen["0"] == seen["1"], seen
        assert seen["1"][0].startswith("reading workflow step wfr_1/preview")
        assert "reading the service logs" in seen["1"][1]

    def test_an_empty_turn_reports_from_the_trace_on_both_paths(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Empty(), Empty(), Empty()]
        old_turn, new_turn = both(monkeypatch, script, max_steps=3)
        assert "read_logs" in new_turn.reply
        assert new_turn.reply.startswith("The model would not write up an answer")


# ════ The intended difference: the protocol re-ask is n/a under native tools ══════════════════

PROTOCOL_WITH_PROSE = json.dumps({"thought": "look", "final": "All three projects exist."})
PROTOCOL_CALLS_ONLY = json.dumps({"thought": "look", "tool_calls": [
    {"name": "read_logs", "arguments": {"run": "r1"}}]})


class TestProtocolReplyIsResidue:
    def test_a_structures_prose_is_kept_without_another_model_call(self, monkeypatch):
        old_turn, old = one_turn(monkeypatch, [Say(PROTOCOL_WITH_PROSE)], flag="0", max_steps=4)
        new_turn, new = one_turn(monkeypatch, [Say(PROTOCOL_WITH_PROSE)], flag="1", max_steps=4)
        assert old_turn.reply == new_turn.reply == "All three projects exist."
        assert old.calls == new.calls == 1

    def test_meant_calls_are_not_run_and_the_json_is_never_printed(self, monkeypatch):
        """The old loop runs the calls a structure meant; the kernel path does not (its engines
        call tools natively) and never asks again — pinned so the difference stays deliberate."""
        script = [Say(PROTOCOL_CALLS_ONLY), Say("after the meant call")]
        old_turn, old = one_turn(monkeypatch, list(script), flag="0", max_steps=4)
        assert [t["tool"] for t in old_turn.trace] == ["read_logs"], "the old loop runs it"
        assert old_turn.reply == "after the meant call"
        new_turn, new = one_turn(monkeypatch, list(script), flag="1", max_steps=4)
        assert new_turn.trace == [], "the kernel path does not run text-protocol calls"
        assert new.calls == 1, "and it does not ask again"
        assert "{" not in new_turn.reply, "the raw JSON never reaches the operator"


# ════ The fall-through: CLI text brains keep today's loop until K8 ════════════════════════════

class TestCliBrainsFallThrough:
    @pytest.mark.parametrize("lane", sorted(_CLI_TEXT_BRAINS))
    def test_a_cli_lane_runs_the_old_loop_with_the_flag_on(self, lane, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setenv("BOSS_KERNEL_CHAT", "1")
        engine = ScriptedEngine([Say("answered on the old loop")])
        engine._boss_choice = SimpleNamespace(engine=lane)
        agent = EngineAgent(ctx=object(), registry=registry([]))
        agent._adapter = engine
        assert agent._kernel_engine("p") is None, f"{lane} must keep the old adapter until K8"
        turn = agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=4)
        assert turn.reply == "answered on the old loop"

    def test_an_openai_lane_runs_the_kernel_with_the_flag_on(self, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setenv("BOSS_KERNEL_CHAT", "1")

        class OpenAIShaped:
            """The adapter face alone (no kernel `complete`), as OpenAIAdapter presents it."""

            def __init__(self):
                self.inner = ScriptedEngine([Say("answered on the kernel")])
                self._boss_choice = SimpleNamespace(engine="openrouter")

            def create_chat_completion(self, messages, tools=None, **kw):
                return self.inner.create_chat_completion(messages, tools=tools)

        agent = EngineAgent(ctx=object(), registry=registry([]))
        agent._adapter = OpenAIShaped()
        picked = agent._kernel_engine("p")
        assert picked is not None and type(picked[0]).__name__ == "OpenAIChatEngine"
        turn = agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=4)
        assert turn.reply == "answered on the kernel"


# ════ The engine is closed when the turn ends, success or failure ═════════════════════════════

class _ClosingEngine(ScriptedEngine):
    """A kernel engine with K7's close() contract: the proxy token must not outlive the turn."""

    def __init__(self, script):
        super().__init__(script)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class TestEngineClose:
    def test_the_engine_is_closed_when_the_turn_ends(self, monkeypatch):
        monkeypatch.setenv("BOSS_KERNEL_CHAT", "1")
        engine = _ClosingEngine([Say("done")])
        agent = EngineAgent(ctx=object(), registry=registry([]))
        agent._adapter = engine
        agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=4)
        assert engine.closed == 1

    def test_the_engine_is_closed_when_the_turn_fails(self, monkeypatch):
        from boss_engine.agent import transient

        monkeypatch.setenv("BOSS_KERNEL_CHAT", "1")
        monkeypatch.setattr(transient.time, "sleep", lambda s: None)
        engine = _ClosingEngine([Fail(RuntimeError("529 overloaded_error")) for _ in range(3)])
        agent = EngineAgent(ctx=object(), registry=registry([]))
        agent._adapter = engine
        with pytest.raises(RuntimeError):
            agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=4)
        assert engine.closed == 1, "a failed turn must still revoke its proxy token"


# ════ The flag off is the old loop, exactly ═══════════════════════════════════════════════════

def test_flag_off_never_touches_the_kernel(monkeypatch):
    import boss_engine.kernel.loop as kernel_loop

    monkeypatch.setenv("BOSS_KERNEL_CHAT", "0")
    monkeypatch.setattr(kernel_loop, "run",
                        lambda spec: pytest.fail("kernel.run reached with the flag off"))
    turn, _ = one_turn(monkeypatch, [Say("plain")], flag="0", max_steps=4)
    assert turn.reply == "plain"


# ════ The live cached-share proof (runs on a keyed host, by hand) ═════════════════════════════

@pytest.mark.skipif(
    not (os.environ.get("BOSS_LIVE_CHAT_CACHE") or "").strip(),
    reason=("the live proof spends real money on a real Anthropic key: set "
            "BOSS_LIVE_CHAT_CACHE=1 (and hold an Anthropic API key in the environment or the "
            "console vault) on a keyed host to run it"))
def test_live_a_chat_threads_second_turn_reads_mostly_from_the_cache():
    """§4.3's bar, measured on the real Messages API: one chat-shaped thread, two turns on
    AnthropicMessagesEngine, and turn 2's cached input share must be ≥70%. Prints the cost."""
    from boss_engine import kernel
    from boss_engine.agent.engine_agent import _SYSTEM_PROMPT
    from boss_engine.config import get_settings
    from boss_engine.kernel import presets
    from boss_engine.kernel.anthropic_engine import engine_for
    from boss_engine.kernel.toolset import FunctionToolSet, Tool
    from boss_engine.kernel.types import ToolSchema

    # A system prompt long enough to cross the cache's minimum, shaped like the router's: the
    # agent prompt plus a persona-sized block that does not change between turns.
    system = _SYSTEM_PROMPT + "\n\n" + ("The factory's standing rules, restated for the test. "
                                        * 120)
    tools = FunctionToolSet([Tool(
        ToolSchema("read_logs", "Read a run's logs.",
                   {"type": "object", "properties": {"run": {"type": "string"}},
                    "required": ["run"], "additionalProperties": False}),
        lambda args: {"ok": True, "lines": [f"run {args.get('run')}: clean"]})])
    engine = engine_for(get_settings())
    try:
        first = kernel.run(kernel.RunSpec(
            engine=engine, tools=tools, policies=presets.engine_agent(4),
            project_id="live-cache", system=system,
            messages=[{"role": "user", "content": "Say READY and nothing else."}]))
        assert first.status == "completed", first.error
        history = [{"role": "user", "content": "Say READY and nothing else."},
                   {"role": "assistant", "content": first.text}]
        second = kernel.run(kernel.RunSpec(
            engine=engine, tools=tools, policies=presets.engine_agent(4),
            project_id="live-cache", system=system,
            messages=history + [{"role": "user", "content": "Now say DONE and nothing else."}]))
        assert second.status == "completed", second.error
        usage = second.usage
        seen = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        share = (usage.cache_read_tokens / seen) if seen else 0.0
        cost = (first.usage.cost_usd_micros + second.usage.cost_usd_micros) / 1_000_000
        print(f"turn 2 cached input share: {share:.1%} "
              f"(read {usage.cache_read_tokens}, wrote {usage.cache_write_tokens}, "
              f"uncached {usage.input_tokens}); the two turns cost ${cost:.4f}")
        assert share >= 0.70, (
            f"turn 2 read only {share:.1%} of its input from the cache; §4.3 wants ≥70%")
    finally:
        engine.close()
