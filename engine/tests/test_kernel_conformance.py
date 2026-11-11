"""THE KERNEL CONFORMANCE SUITE (BOSSAPP-961 K1; HARNESS_KERNEL_DESIGN.md §8.1, §8.4).

The same scripted model is played through an OLD loop and through the KERNEL with that loop's
preset, and the two runs must agree — on what the model was sent (the request stream: messages,
and which tools each call offered), and on the outcome. A kernel that "roughly" reproduces a
loop would change agent behaviour the day that loop moves onto it (K2-K6, K11); byte-equal
injected prompts are the proof it will not.

The scenarios, per the design: step cap; answer on the last step; refused tool call; malformed
call and its repair; empty completion; transient error; suspend for approval and resume; budget
wait; cancellation.

INTENDED DIFFERENCES (design §8.4) are asserted too — each one the stricter or safer behaviour,
and each one pinned so it cannot silently widen:

  D1 a transient provider error fails agent_loop, decisions and the pre-check today; the kernel
     retries it (≤3) everywhere, as chat always did;
  D2 an empty completion ends an agent_loop run with an empty answer today; the kernel presses
     twice, as chat does;
  D3 a call whose arguments break the tool's schema RUNS today (json errors become `{}`); the
     kernel refuses it before the tool runs, names the failing JSON pointer, and allows one
     repair per tool;
  D4 a run out of refused-call feedback raises out of agent_loop today; the kernel ends the run
     `failed` with the provider's words, so the caller reads a result, not a traceback;
  D5 the model call a budget defers fails an agent step today; the kernel parks the run
     (budget_wait) and resumes it, as tool-budget waits already park it;
  D6 the native coder's turn-cap marker ([boss] stopped at the N-turn cap … PARTIAL_MARKER) is
     the CALLER's summary decoration, not the loop's: the kernel reports stopped="steps" and the
     K11 ScaffoldCodeAgent writes the marker;
  D7 cancellation: no old loop can be stopped between calls; every kernel run can.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.checkpoint import MemoryStore
from boss_engine.kernel.scripted import Call, Calls, Empty, Fail, Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import Acceptance, ToolSchema

SYSTEM = "You are the test agent."
ASK = "Check the run."
REFUSED_400 = ("BadRequestError: 400 - tool call validation failed: parameters for tool "
               "read_logs did not match schema: errors: [missing properties: 'run']")

READ_SCHEMA = {"type": "object", "properties": {"run": {"type": "string"}},
               "required": ["run"], "additionalProperties": False}


def read_logs_result(args):
    return {"ok": True, "tool": "read_logs", "lines": [f"run {args.get('run')}: 3 errors"]}


def kernel_tools():
    return FunctionToolSet([Tool(ToolSchema("read_logs", "Read a run's logs.", READ_SCHEMA),
                                 read_logs_result)])


def kernel_run(script, policies, *, tools=None, messages=None, system=SYSTEM, sleep=None,
               engine=None, **spec_kw):
    eng = engine or ScriptedEngine(script)
    result = kernel.run(kernel.RunSpec(
        engine=eng, tools=tools or kernel_tools(), policies=policies, project_id="p",
        system=system, messages=messages or [{"role": "user", "content": ASK}],
        sleep=sleep or (lambda s: None), **spec_kw))
    return result, eng


def same_requests(old: ScriptedEngine, new: ScriptedEngine):
    """The two engines saw the same conversation: same tool names offered per call, same
    message roles and contents (assistant turns byte-equal; tool and user turns byte-equal)."""
    assert len(old.requests) == len(new.requests), (len(old.requests), len(new.requests))
    for n, (a, b) in enumerate(zip(old.requests, new.requests)):
        assert a["tools"] == b["tools"], (n, a["tools"], b["tools"])
        assert len(a["messages"]) == len(b["messages"]), (
            n, [m.get("role") for m in a["messages"]], [m.get("role") for m in b["messages"]])
        for i, (ma, mb) in enumerate(zip(a["messages"], b["messages"])):
            assert ma.get("role") == mb.get("role"), (n, i, ma, mb)
            assert ma.get("content") == mb.get("content"), (n, i, ma.get("content"),
                                                            mb.get("content"))


# ── Driving the old loops ─────────────────────────────────────────────────────────────────────

def drive_agent_loop(monkeypatch, adapter, max_steps, *, write_fns=None, raises=None,
                     dispatch=None):
    """agent_loop._drive_run in memory, as test_a_marked_payload_does_not_leave_on_its_own
    drives it: the loop is real; everything around it (store, router, MCP, skills) is stubbed."""
    from boss_engine.services import agent_loop

    run = {"project_id": "p", "task_id": None,
           "messages": [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": ASK}],
           "transcript": [], "usage": {}, "finalize_ctx": {}, "step": 0}
    saved = {}

    def save(_c, _rid, _pid, _tid, status, messages, transcript, usage, fctx, *, step=0,
             pending=None, output_text=None):
        saved.update(status=status, step=step, pending=pending, output=output_text)

    monkeypatch.setattr(agent_loop, "load_agent_run", lambda *_a, **_k: run)
    monkeypatch.setattr(agent_loop, "_save_run", save)
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(agent_loop, "governed", lambda a, *_a, **_k: a)
    monkeypatch.setattr(agent_loop, "_agent_purpose", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_loop, "MCPClient", lambda settings: None)
    monkeypatch.setattr(agent_loop, "list_mcp_servers", lambda *_a, **_k: [])
    monkeypatch.setattr(agent_loop, "_skill_resolution_for_run",
                        lambda *_a, **_k: {"eager": [], "on_demand": []})
    monkeypatch.setattr(agent_loop, "emit_event", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_loop, "create_blocking_decision", lambda *_a, **_k: "dec_1")
    monkeypatch.setattr(agent_loop, "_native_tool_schemas", lambda settings: [
        {"type": "function", "function": {"name": "read_logs",
                                          "description": "Read a run's logs.",
                                          "parameters": READ_SCHEMA}}])
    monkeypatch.setattr(agent_loop, "_WRITE_FNS", dict(write_fns or {}))
    monkeypatch.setattr(agent_loop, "_WRITE_SCHEMAS", [
        {"type": "function", "function": {"name": name, "description": name,
                                          "parameters": {"type": "object", "properties": {}}}}
        for name in (write_fns or {})])
    monkeypatch.setattr(agent_loop, "_dispatch",
                        dispatch or (lambda _c, _p, _s, name, args: read_logs_result(args)))
    settings = SimpleNamespace(boss_edition="community", agent_max_steps=max_steps,
                               agent_autonomous_risk_max=2)
    if raises is not None:
        with pytest.raises(raises):
            agent_loop._drive_run(object(), settings, "arun_conf", max_steps)
        return None, saved
    return agent_loop._drive_run(object(), settings, "arun_conf", max_steps), saved


def engine_agent(adapter, monkeypatch):
    """EngineAgent on a registry holding the same read tool, transient sleeps silenced. This
    drives the OLD loop on purpose (the kernel side is `kernel_run`), so BOSS_KERNEL_CHAT is
    pinned off whatever the ambient environment says."""
    from boss_engine.agent import EngineAgent, transient
    from boss_engine.tools.registry import ToolRegistry, ToolSpec

    monkeypatch.setenv("BOSS_KERNEL_CHAT", "")
    monkeypatch.setattr(transient.time, "sleep", lambda s: None)
    reg = ToolRegistry()
    reg.register(ToolSpec("read_logs", "Read a run's logs.", READ_SCHEMA,
                          lambda ctx, project_id, actor, args: read_logs_result(args),
                          kind="read", permission="system:read"))
    agent = EngineAgent(ctx=object(), registry=reg)
    agent._adapter = adapter
    return agent


def engine_agent_kernel_tools():
    """The same tool as the registry hands EngineAgent, result shape included (registry.invoke
    setdefaults ok and tool, which read_logs_result already carries)."""
    return kernel_tools()


def decide_reading(adapter, *, max_turns=4, max_usd=300_000, candidates=None, check=None):
    from boss_engine.services import agent_decision as ad

    candidates = candidates or [ad.Candidate("fix", "fix it"), ad.Candidate("ask", "ask")]
    read = ad.ReadPhase(
        tools=[{"type": "function", "function": {"name": "read_logs",
                                                 "description": "Read a run's logs.",
                                                 "parameters": READ_SCHEMA}}],
        run=lambda name, args: read_logs_result(args), max_turns=max_turns,
        max_usd_micros=max_usd)
    return ad._ask_reading(adapter, SYSTEM, ASK, read=read, candidates=candidates,
                           check=check, corpus="CORPUS")


def decision_policies(*, max_turns=4, max_usd=300_000, candidates=None, check=None):
    from boss_engine.services import agent_decision as ad

    candidates = candidates or [ad.Candidate("fix", "fix it"), ad.Candidate("ask", "ask")]

    def accept(raw, state):
        corpus = "\n".join(["CORPUS"] + list(state.rendered))
        answer, why = ad._accept(raw, candidates, check, corpus)
        return Acceptance(ok=answer is not None, why=why, structured=answer)

    return presets.decision(max_turns=max_turns, max_usd_micros=max_usd, accept=accept)


def run_precheck(adapter, *, max_turns=8, max_usd=500_000, max_wait=180.0, sleeps=None):
    from boss_engine.services.workflow_precheck_agent import run_agent

    sleeps = sleeps if sleeps is not None else []
    tick = {"t": 0.0}

    def clock():
        return tick["t"]

    def sleep(s):
        sleeps.append(s)
        tick["t"] += s

    return run_agent(adapter=adapter, raw_adapter=adapter, evidence={"steps": []},
                     tools=[{"type": "function",
                             "function": {"name": "read_logs", "description": "Read logs.",
                                          "parameters": READ_SCHEMA}}],
                     run_tool=lambda name, args: read_logs_result(args),
                     max_usd_micros=max_usd, max_turns=max_turns, step_ids=(),
                     max_wait_seconds=max_wait, sleep=sleep, clock=clock), sleeps


PRECHECK_EVIDENCE = "THE EVIDENCE (JSON):\n" + json.dumps({"steps": []}, default=str, indent=1)


def precheck_messages():
    from boss_engine.services.workflow_precheck_agent import SYSTEM_PROMPT

    return SYSTEM_PROMPT, [{"role": "user", "content": PRECHECK_EVIDENCE}]


# ════ Scenario: step cap ══════════════════════════════════════════════════════════════════════

FIX = json.dumps({"action": "fix", "confidence": 0.9, "reason": "logs say so",
                  "evidence": ["run r1: 3 errors"]})


class TestStepCap:
    def test_agent_loop_runs_out_of_steps_and_answers_without_tools(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Call("read_logs", {"run": "r2"}),
                  Say("Both runs are red.")]
        old = ScriptedEngine(list(script))
        got, _ = drive_agent_loop(monkeypatch, old, 2)
        new_result, new = kernel_run(script, presets.agent_loop(2))
        same_requests(old, new)
        assert got["status"] == new_result.status == "completed"
        assert got["output_text"] == new_result.text == "Both runs are red."
        assert [r["tools"] for r in new.requests] == [["read_logs"], ["read_logs"], None]
        assert new_result.exhausted == "steps"

    def test_engine_agent_reserves_the_last_step_for_the_answer(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Call("read_logs", {"run": "r2"}),
                  Say("r1 and r2 both fail on auth.")]
        old = ScriptedEngine(list(script))
        turn = engine_agent(old, monkeypatch).chat(project_id="p", message=ASK, system=SYSTEM,
                                                   max_steps=3)
        new_result, new = kernel_run(script, presets.engine_agent(3))
        same_requests(old, new)
        assert turn.reply == new_result.text
        assert turn.exhausted == new_result.exhausted == "steps"
        assert old.requests[-1]["tools"] is None

    def test_the_decision_and_the_precheck_answer_at_their_turn_caps(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Say(FIX)]
        old = ScriptedEngine(list(script))
        got = decide_reading(old, max_turns=2)
        new_result, new = kernel_run(script, decision_policies(max_turns=2))
        same_requests(old, new)
        assert got["answer"]["action"] == new_result.structured["action"] == "fix"
        assert got["turns"] == new_result.steps == 2
        assert old.requests[1]["tools"] is None
        assert old.requests[1]["messages"][-1]["content"] == \
            new.requests[1]["messages"][-1]["content"]  # ANSWER_NOW after a tool result

        pre_script = [Call("read_logs", {"run": "r1"}), Say("[]")]
        pre_old = ScriptedEngine(list(pre_script))
        report, _ = run_precheck(pre_old, max_turns=2)
        system, messages = precheck_messages()
        pre_result, pre_new = kernel_run(pre_script, presets.precheck(max_turns=2),
                                         system=system, messages=messages)
        same_requests(pre_old, pre_new)
        assert report["turns"] == pre_result.steps == 2 and report["stopped"] is None
        assert pre_result.structured == {"items": []}

    def test_the_native_coder_stops_at_the_cap_and_the_marker_is_the_callers(self, tmp_path,
                                                                             monkeypatch):
        """Intended difference D6: the old loop decorates its summary with the cap sentence and
        PARTIAL_MARKER; the kernel reports stopped='steps' and leaves the words to K11."""
        from boss_engine.config import get_settings
        from boss_engine.services.code_agent import NativeCodeAgent
        from boss_engine.services.code_agent_tools import SandboxToolset, resolve_autonomy
        from boss_engine.services.engine_contract import PARTIAL_MARKER

        (tmp_path / "a.txt").write_text("alpha\n")
        settings = get_settings()
        script = [Call("read_file", {"path": "a.txt"}), Call("read_file", {"path": "a.txt"}),
                  Say("never reached")]
        agent = NativeCodeAgent(settings)
        old = ScriptedEngine(list(script))
        agent._llm = old
        monkeypatch.setattr(NativeCodeAgent, "_repo_context", lambda *a, **k: "")
        loop_out = agent._run_tool_loop(ASK, tmp_path, read_only=True, model=None, max_turns=2)

        box = SandboxToolset(tmp_path, read_only=True, settings=settings,
                             autonomy=resolve_autonomy(settings, read_only=True))
        tools = FunctionToolSet([Tool(
            ToolSchema("read_file", "Read a file.",
                       {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False}),
            lambda args: box.call("read_file", args).text)])
        new_result, new = kernel_run(script, presets.native_coder(2), tools=tools)
        assert loop_out["turns"] == new_result.steps == 2
        assert loop_out["stop_reason"] == "turn_cap" and new_result.stopped == "steps"
        assert PARTIAL_MARKER in loop_out["summary"]
        assert PARTIAL_MARKER not in new_result.text  # D6: the caller writes the marker
        assert [t["status"] for t in new_result.trace] == ["ok", "ok"]

    def test_the_native_coder_finishing_early_matches(self, tmp_path, monkeypatch):
        from boss_engine.config import get_settings
        from boss_engine.services.code_agent import NativeCodeAgent
        from boss_engine.services.code_agent_tools import SandboxToolset, resolve_autonomy

        (tmp_path / "a.txt").write_text("alpha\n")
        settings = get_settings()
        script = [Call("read_file", {"path": "a.txt"}), Say("a.txt holds alpha.")]
        agent = NativeCodeAgent(settings)
        agent._llm = ScriptedEngine(list(script))
        monkeypatch.setattr(NativeCodeAgent, "_repo_context", lambda *a, **k: "")
        loop_out = agent._run_tool_loop(ASK, tmp_path, read_only=True, model=None, max_turns=6)
        box = SandboxToolset(tmp_path, read_only=True, settings=settings,
                             autonomy=resolve_autonomy(settings, read_only=True))
        tools = FunctionToolSet([Tool(
            ToolSchema("read_file", "Read a file.",
                       {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False}),
            lambda args: box.call("read_file", args).text)])
        new_result, _ = kernel_run(script, presets.native_coder(6), tools=tools)
        assert loop_out["summary"] == new_result.text == "a.txt holds alpha."
        assert loop_out["stop_reason"] == "done" and new_result.stopped == ""
        assert loop_out["turns"] == new_result.steps == 2


# ════ Scenario: answer on the last step ═══════════════════════════════════════════════════════

class TestAnswerOnTheLastStep:
    def test_the_forced_answer_prompts_are_byte_equal(self, monkeypatch):
        """agent_loop's extra answer call and EngineAgent's reserved last step both inject their
        instruction; the kernel injects the same bytes (presets copy them verbatim)."""
        script = [Call("read_logs", {"run": "r1"}), Say("answered")]
        old = ScriptedEngine(list(script))
        drive_agent_loop(monkeypatch, old, 1)
        new_result, new = kernel_run(script, presets.agent_loop(1))
        same_requests(old, new)
        assert old.requests[1]["messages"][-1]["content"].startswith(
            "You have used your tool budget for this turn.")

        old2 = ScriptedEngine(list(script))
        turn = engine_agent(old2, monkeypatch).chat(project_id="p", message=ASK, system=SYSTEM,
                                                    max_steps=2)
        new2_result, new2 = kernel_run(script, presets.engine_agent(2))
        same_requests(old2, new2)
        assert turn.reply == new2_result.text == "answered"
        assert old2.requests[1]["messages"][-1]["content"].startswith(
            "You are out of tool budget for this turn.")

    def test_a_turn_budget_forces_the_answer_with_the_closing_offer(self, monkeypatch):
        """EngineAgent out of TurnBudget calls: the next call is the answer, propose_action
        allowed in the same call. The kernel with the same budget behaves the same."""
        from boss_engine.services.turn_budget import TurnBudget
        from boss_engine.tools.registry import ToolSpec

        script = [Call("read_logs", {"run": "r1"}),
                  Say("The fix is to restart; offering it.")]
        old = ScriptedEngine(list(script))
        clock = {"t": 0.0}
        old_budget = TurnBudget(seconds=10_000, model_calls=2, clock=lambda: clock["t"])
        agent = engine_agent(old, monkeypatch)
        agent.registry.register(ToolSpec(
            "propose_action", "Offer a fix as a button.", {"type": "object", "properties": {}},
            lambda ctx, project_id, actor, args: {"ok": True, "proposed": True},
            kind="read", permission="system:read"))
        turn = agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=8,
                          budget=old_budget)
        new_budget = TurnBudget(seconds=10_000, model_calls=2, clock=lambda: clock["t"])
        offer_tool = Tool(ToolSchema("propose_action", "Offer a fix as a button.",
                                     {"type": "object", "properties": {}}),
                          lambda args: {"ok": True, "proposed": True})
        # The registry offers its schemas sorted by name; match that order.
        tools = FunctionToolSet([offer_tool,
                                 Tool(ToolSchema("read_logs", "Read a run's logs.", READ_SCHEMA),
                                      read_logs_result)])
        new_result, new = kernel_run(script, presets.engine_agent(8, budget=new_budget),
                                     tools=tools)
        same_requests(old, new)
        assert turn.reply == new_result.text
        assert turn.exhausted == new_result.exhausted == "budget"
        assert "propose_action ONCE in this same message" in \
            new.requests[-1]["messages"][-2]["content"] or \
            "propose_action ONCE in this same message" in \
            new.requests[-1]["messages"][-1]["content"]


# ════ Scenario: refused tool call (the provider's 400) ════════════════════════════════════════

class TestRefusedToolCall:
    def test_agent_loop_and_kernel_hand_back_the_same_feedback(self, monkeypatch):
        script = [Fail(RuntimeError(REFUSED_400)), Say("rewrote the call; done")]
        old = ScriptedEngine(list(script))
        got, _ = drive_agent_loop(monkeypatch, old, 4)
        new_result, new = kernel_run(script, presets.agent_loop(4))
        same_requests(old, new)
        assert got["output_text"] == new_result.text == "rewrote the call; done"
        feedback = old.requests[1]["messages"][-1]["content"]
        assert feedback.startswith("YOUR LAST TOOL CALL WAS REFUSED BEFORE IT RAN")
        assert feedback == new.requests[1]["messages"][-1]["content"]

    def test_a_model_that_cannot_get_it_right_still_stops(self, monkeypatch):
        """agent_loop raises past MAX_REFUSED_TOOL_CALLS; the kernel ends the run failed with
        the provider's words (intended difference D4: a result, not a traceback)."""
        script = [Fail(RuntimeError(REFUSED_400)) for _ in range(5)]
        old = ScriptedEngine(list(script))
        drive_agent_loop(monkeypatch, old, 8, raises=RuntimeError)
        new_result, new = kernel_run(script, presets.agent_loop(8))
        assert len(old.requests) == len(new.requests) == 4  # 1 first try + 3 feedbacks
        assert new_result.status == "failed" and "did not match schema" in new_result.error

    def test_chat_gains_the_feedback_it_never_had(self, monkeypatch):
        """EngineAgent today raises on the provider's 400 (only _looks_transient is retried);
        on the kernel the same model finishes (intended difference: agent_loop's recovery,
        everywhere)."""
        script = [Fail(RuntimeError(REFUSED_400)), Say("done after feedback")]
        old = ScriptedEngine(list(script))
        with pytest.raises(RuntimeError, match="validation failed"):
            engine_agent(old, monkeypatch).chat(project_id="p", message=ASK, system=SYSTEM)
        new_result, _ = kernel_run(script, presets.engine_agent(6))
        assert new_result.status == "completed" and new_result.text == "done after feedback"


# ════ Scenario: malformed call and its repair ═════════════════════════════════════════════════

class TestMalformedCall:
    def test_the_kernel_refuses_before_the_tool_runs_and_allows_one_repair(self):
        """Intended difference D3. Old loops parse bad JSON into {} and RUN the tool; the
        kernel refuses, names the pointer, and the repaired call succeeds."""
        ran = []

        def recording(args):
            ran.append(dict(args))
            return read_logs_result(args)

        tools = FunctionToolSet([Tool(ToolSchema("read_logs", "Read logs.", READ_SCHEMA),
                                      recording)])
        script = [Call("read_logs", "{not json"), Call("read_logs", {"run": "r1"}), Say("ok")]
        new_result, new = kernel_run(script, presets.agent_loop(6), tools=tools)
        assert new_result.status == "completed"
        assert ran == [{"run": "r1"}], "the malformed call never reached the tool"
        refusal = json.loads(new.requests[1]["messages"][-1]["content"])
        assert refusal["status"] == "refused" and refusal["error"] == "invalid_arguments"
        kinds = [e.kind for e in new_result.events]
        assert "schema.invalid" in kinds and "schema.repaired" in kinds

    def test_the_old_loop_runs_the_tool_on_empty_arguments(self, monkeypatch):
        """What D3 replaces, pinned so the difference stays deliberate."""
        ran = []

        def spy(_c, _p, _s, name, args):
            ran.append(args)
            return read_logs_result(args)

        script = [Call("read_logs", "{not json"), Say("ok")]
        old = ScriptedEngine(list(script))
        drive_agent_loop(monkeypatch, old, 4, dispatch=spy)
        assert ran == [{}], "today the tool runs with {} — the kernel refuses instead"

    def test_the_second_bad_write_of_the_same_tool_fails_for_good(self):
        script = [Call("read_logs", {"run": 7}), Call("read_logs", {"run": True}), Say("gave up")]
        new_result, new = kernel_run(script, presets.engine_agent(6))
        second = json.loads(new.requests[2]["messages"][-1]["content"])
        assert "again" in second["message"] and "Do not call it that way again" in \
            second["message"]
        assert new_result.status == "completed"


# ════ Scenario: empty completion ══════════════════════════════════════════════════════════════

class TestEmptyCompletion:
    def test_chat_presses_twice_with_the_same_words(self, monkeypatch):
        script = [Empty(), Empty(), Say("third time lucky")]
        old = ScriptedEngine(list(script))
        turn = engine_agent(old, monkeypatch).chat(project_id="p", message=ASK, system=SYSTEM,
                                                   max_steps=6)
        new_result, new = kernel_run(script, presets.engine_agent(6))
        same_requests(old, new)
        assert turn.reply == new_result.text == "third time lucky"
        assert old.requests[1]["messages"][-1]["content"].startswith(
            "Your last message was empty.")

    def test_an_agent_step_no_longer_accepts_silence(self, monkeypatch):
        """Intended difference D2: agent_loop completes with an empty answer today; the kernel
        presses, as chat does."""
        script = [Empty(), Say("pressed answer")]
        old = ScriptedEngine(list(script))
        got, _ = drive_agent_loop(monkeypatch, old, 4)
        assert got["status"] == "completed" and got["output_text"] == ""
        new_result, _ = kernel_run(script, presets.agent_loop(4))
        assert new_result.text == "pressed answer"


# ════ Scenario: transient error ═══════════════════════════════════════════════════════════════

class TestTransientError:
    def test_chat_and_kernel_retry_the_same_number_of_times(self, monkeypatch):
        script = [Fail(RuntimeError("529 overloaded_error")),
                  Fail(RuntimeError("502 bad gateway")), Say("third attempt answered")]
        old = ScriptedEngine(list(script))
        turn = engine_agent(old, monkeypatch).chat(project_id="p", message=ASK, system=SYSTEM)
        new_result, new = kernel_run(script, presets.engine_agent(6))
        same_requests(old, new)
        assert turn.reply == new_result.text
        assert [e.kind for e in new_result.events].count("model.retry") == 2

        exhausted = [Fail(RuntimeError("529 overloaded_error")) for _ in range(3)]
        old2 = ScriptedEngine(list(exhausted))
        with pytest.raises(RuntimeError):
            engine_agent(old2, monkeypatch).chat(project_id="p", message=ASK, system=SYSTEM)
        new2_result, new2 = kernel_run(exhausted, presets.engine_agent(6))
        assert len(old2.requests) == len(new2.requests) == 3
        assert new2_result.status == "failed" and "529" in new2_result.error

    def test_an_agent_step_gains_the_retry_it_never_had(self, monkeypatch):
        """Intended difference D1: agent_loop dies on a hiccup today; the kernel retries."""
        script = [Fail(RuntimeError("529 overloaded_error")), Say("done")]
        old = ScriptedEngine(list(script))
        drive_agent_loop(monkeypatch, old, 4, raises=RuntimeError)
        new_result, _ = kernel_run(script, presets.agent_loop(4))
        assert new_result.status == "completed" and new_result.text == "done"


# ════ Scenario: suspend for approval, and resume ══════════════════════════════════════════════

def deferral(name="publish_document", risk=3):
    return {"status": "deferred_for_approval", "decision_id": "dec_1",
            "message": (f"'{name}' (risk tier {risk}) requires human approval; the run is "
                        "paused and resumes on approval.")}


class TestSuspendAndResume:
    def hold_tools(self, ran):
        def hold(args):
            return deferral()

        def approved(args):
            ran.append(dict(args))
            return {"ok": True, "published": True}

        return FunctionToolSet([
            Tool(ToolSchema("publish_document", "publish", {"type": "object", "properties": {}},
                            kind="write"), hold, run_approved=approved),
            Tool(ToolSchema("read_logs", "Read logs.", READ_SCHEMA), read_logs_result)])

    def test_the_suspension_and_the_skipped_sibling_match_agent_loop(self, monkeypatch):
        script = [Calls(Call("publish_document", {}), Call("read_logs", {"run": "r1"})),
                  Say("done")]
        old = ScriptedEngine(list(script))
        got, saved = drive_agent_loop(
            monkeypatch, old, 4,
            write_fns={"publish_document": (lambda *a, **k: {"ok": True}, 3)})
        new_result, new = kernel_run(script, presets.agent_loop(4), tools=self.hold_tools([]),
                                     store=MemoryStore(), run_id="krun_hold")
        assert got["status"] == "suspended" and new_result.status == "suspended"
        assert got["decision_id"] == new_result.pending["decision_id"] == "dec_1"
        assert saved["status"] == "awaiting_approval"
        # Both told the model the same two things: the deferral, and why its sibling waited.
        old_tool_msgs = [m["content"] for m in old.requests[0]["messages"]]  # captured pre-call
        new_msgs = [m for m in new_result.events if m.kind in ("tool.deferred", "tool.result")]
        assert {m.kind for m in new_msgs} == {"tool.deferred", "tool.result"}
        sibling = [t for t in new_result.trace if t["tool"] == "read_logs"][0]
        assert sibling["status"] == "skipped"
        assert sibling["result"]["note"] == "a prior action this turn awaits approval"
        assert old_tool_msgs is not None  # the request stream itself is compared on resume

    def test_resume_runs_the_held_call_once_and_replaces_the_deferral(self):
        ran = []
        store = MemoryStore()
        script = [Call("publish_document", {}), Say("published, all done")]
        engine = ScriptedEngine(script)
        spec = kernel.RunSpec(engine=engine, tools=self.hold_tools(ran),
                              policies=presets.agent_loop(4), project_id="p", system=SYSTEM,
                              messages=[{"role": "user", "content": ASK}], run_id="krun_res",
                              store=store, persist=True,
                              resume_note=presets.agent_loop_resume_note)
        first = kernel.run(spec)
        assert first.status == "suspended" and ran == []

        denied = kernel.resume(spec, decision={"approved": True, "decision_id": "dec_other"})
        assert denied.status == "denied" and ran == []

        done = kernel.resume(spec, decision={"approved": True, "decision_id": "dec_1"})
        assert done.status == "completed" and done.text == "published, all done"
        assert ran == [{}], "the approved call ran exactly once"
        resumed_msgs = engine.requests[-1]["messages"]
        note = [m for m in resumed_msgs if str(m.get("content", "")).startswith(
            "APPROVED & EXECUTED")]
        assert note, "the model is told the approved result, in agent_loop's words"
        deferred_left = [m for m in resumed_msgs if m.get("role") == "tool"
                         and "deferred_for_approval" in str(m.get("content"))]
        assert not deferred_left, "the deferral payload was replaced by the real result"

        again = kernel.resume(spec, decision={"approved": True, "decision_id": "dec_1"})
        assert again.status == "completed" and ran == [{}], "resuming twice runs nothing twice"

    def test_the_resume_note_is_agent_loops_own_wording(self):
        import inspect

        from boss_engine.services import agent_loop

        source = inspect.getsource(agent_loop.resume_agent_run)
        rendered = presets.agent_loop_resume_note({"tool": "publish_document"}, "{}")
        assert "APPROVED & EXECUTED: a human approved the deferred action" in source
        assert rendered.startswith("APPROVED & EXECUTED: a human approved the deferred action "
                                   "'publish_document'.")


# ════ Scenario: budget wait ═══════════════════════════════════════════════════════════════════

class TestBudgetWait:
    def test_the_precheck_and_the_kernel_wait_out_the_same_deferral(self):
        from boss_engine.services.model_routing import ModelSpendDeferred

        script = [Fail(ModelSpendDeferred("the per-minute budget is busy",
                                          retry_after_seconds=7)), Say("[]")]
        old = ScriptedEngine(list(script))
        report, old_sleeps = run_precheck(old, max_turns=4)
        system, messages = precheck_messages()
        new_sleeps = []
        new_result, new = kernel_run(script, presets.precheck(max_turns=4), system=system,
                                     messages=messages, sleep=new_sleeps.append)
        same_requests(old, new)
        assert report["waits"] == 1 and old_sleeps == new_sleeps
        assert new_result.status == "completed"
        assert [e.kind for e in new_result.events].count("budget.wait") == 1

    def test_past_the_wait_bound_both_stop_and_say_so(self):
        from boss_engine.services.model_routing import ModelSpendDeferred

        deferrals = [Fail(ModelSpendDeferred("busy", retry_after_seconds=120)) for _ in range(4)]
        old = ScriptedEngine(list(deferrals))
        report, old_sleeps = run_precheck(old, max_turns=8, max_wait=180.0)
        system, messages = precheck_messages()
        new_sleeps = []
        new_result, new = kernel_run(deferrals, presets.precheck(max_turns=8), system=system,
                                     messages=messages, sleep=new_sleeps.append)
        assert report["stopped"] == "budget_wait_limit" == new_result.stopped
        assert old_sleeps == new_sleeps
        assert len(old.requests) == len(new.requests)

    def test_an_agent_step_parks_instead_of_failing_and_resumes(self):
        """Intended difference D5: agent_loop fails on a deferred MODEL call today (only its
        tool calls waited); a kernel agent step parks as budget_wait and resumes."""
        from boss_engine.services.model_routing import ModelSpendDeferred

        store = MemoryStore()
        engine = ScriptedEngine([Fail(ModelSpendDeferred("busy", retry_after_seconds=9)),
                                 Say("after the budget freed")])
        spec = kernel.RunSpec(engine=engine, tools=kernel_tools(),
                              policies=presets.agent_loop(4), project_id="p", system=SYSTEM,
                              messages=[{"role": "user", "content": ASK}], run_id="krun_bw",
                              store=store, persist=True)
        parked = kernel.run(spec)
        assert parked.status == "budget_wait"
        assert parked.pending["budget"]["retry_after_seconds"] == 9
        woke = kernel.resume(spec)
        assert woke.status == "completed" and woke.text == "after the budget freed"

    def test_a_decisions_deferral_is_an_error_result_on_both_sides(self):
        from boss_engine.services.model_routing import ModelSpendDeferred

        script = [Fail(ModelSpendDeferred("busy", retry_after_seconds=3))]
        old = ScriptedEngine(list(script))
        got = decide_reading(old)
        assert got["answer"] is None and "the model budget deferred the turn" in got["error"]
        new_result, _ = kernel_run(script, decision_policies())
        assert new_result.status == "failed" and "busy" in new_result.error


# ════ Scenario: cancellation ══════════════════════════════════════════════════════════════════

class TestCancellation:
    def test_a_cancelled_run_stops_before_its_next_call_and_is_resumable_to_read(self):
        """Intended difference D7: no old loop stops between calls; every kernel run does, and
        the checkpoint keeps what it had."""
        token = kernel.CancelToken()
        seen = []

        def ticking(args):
            seen.append(1)
            token.cancel(by="operator", reason="stop requested")
            return {"ok": True}

        store = MemoryStore()
        tools = FunctionToolSet([Tool(
            ToolSchema("tick", "tick", {"type": "object", "properties": {},
                                        "additionalProperties": False}), ticking)])
        engine = ScriptedEngine([Call("tick", {}), Call("tick", {}), Say("never")])
        result = kernel.run(kernel.RunSpec(engine=engine, tools=tools,
                                           policies=presets.agent_loop(6), project_id="p",
                                           system=SYSTEM,
                                           messages=[{"role": "user", "content": ASK}],
                                           run_id="krun_cancel", store=store, cancel=token))
        assert result.status == "cancelled" and seen == [1]
        assert engine.calls == 1, "no model call after the stop"
        saved = store.load("krun_cancel")
        assert saved is not None and saved.status == "cancelled"
        assert [e.kind for e in result.events][-1] == "run.cancelled"

    def test_cancel_by_run_id_reaches_a_live_run(self):
        import threading

        from boss_engine.kernel import loop as kernel_loop

        release = threading.Event()
        cancelled = threading.Event()

        def slow(args):
            release.set()
            cancelled.wait(timeout=10)
            return {"ok": True}

        tools = FunctionToolSet([Tool(
            ToolSchema("slow", "slow", {"type": "object", "properties": {},
                                        "additionalProperties": False}), slow)])
        engine = ScriptedEngine([Call("slow", {}), Say("never")])
        out = {}

        def drive():
            out["result"] = kernel.run(kernel.RunSpec(
                engine=engine, tools=tools, policies=presets.agent_loop(6), project_id="p",
                system=SYSTEM, messages=[{"role": "user", "content": ASK}],
                run_id="krun_live", store=MemoryStore()))

        thread = threading.Thread(target=drive)
        thread.start()
        assert release.wait(timeout=10)
        assert kernel_loop.cancel("krun_live", by="test")
        cancelled.set()
        thread.join(timeout=10)
        assert out["result"].status == "cancelled"
        assert not kernel_loop.cancel("krun_live", by="test"), "the run is no longer live"


# ════ The re-asked decision (the verify policy) ═══════════════════════════════════════════════

class TestDecisionReask:
    def test_a_refused_answer_is_asked_once_more_in_the_same_words(self):
        bad = json.dumps({"action": "panic", "confidence": 0.9})
        script = [Say(bad), Say(FIX)]
        old = ScriptedEngine(list(script))
        got = decide_reading(old)
        new_result, new = kernel_run(script, decision_policies())
        same_requests(old, new)
        assert got["answer"]["action"] == new_result.structured["action"] == "fix"
        assert old.requests[1]["messages"][-1]["content"].startswith("Refused: 'panic' is not")

    def test_twice_refused_is_the_end_on_both_sides(self):
        bad = json.dumps({"action": "panic"})
        script = [Say(bad), Say(bad), Say(FIX)]
        old = ScriptedEngine(list(script))
        got = decide_reading(old)
        new_result, new = kernel_run(script, decision_policies())
        assert got["answer"] is None and "panic" in got["refused"]
        assert new_result.status == "failed" and "panic" in new_result.error
        assert len(old.requests) == len(new.requests) == 2

    def test_the_prechecks_malformed_answer_gets_its_one_format_retry(self):
        script = [Say("not json at all"), Say("[]")]
        old = ScriptedEngine(list(script))
        report, _ = run_precheck(old)
        system, messages = precheck_messages()
        new_result, new = kernel_run(script, presets.precheck(), system=system,
                                     messages=messages)
        same_requests(old, new)
        assert report["stopped"] is None and new_result.status == "completed"
        assert old.requests[1]["messages"][-1]["content"] == \
            new.requests[1]["messages"][-1]["content"]  # FORMAT_AGAIN


# ════ The same-tool nudge ═════════════════════════════════════════════════════════════════════

class TestSameToolNudge:
    def test_three_failures_of_one_tool_draw_the_same_words(self, monkeypatch):
        from boss_engine.tools.registry import ToolRegistry, ToolSpec

        def failing(ctx, project_id, actor, args):
            return {"ok": False, "error": "no such column", "tool": "probe_db"}

        reg = ToolRegistry()
        reg.register(ToolSpec("probe_db", "Query.", {"type": "object", "properties": {}},
                              failing, kind="read", permission="system:read"))
        script = [Call("probe_db", {}), Call("probe_db", {}), Call("probe_db", {}),
                  Say("I stopped retrying.")]
        old = ScriptedEngine(list(script))
        from boss_engine.agent import EngineAgent, transient

        monkeypatch.setenv("BOSS_KERNEL_CHAT", "")  # this drives the OLD loop on purpose
        monkeypatch.setattr(transient.time, "sleep", lambda s: None)
        agent = EngineAgent(ctx=object(), registry=reg)
        agent._adapter = old
        turn = agent.chat(project_id="p", message=ASK, system=SYSTEM, max_steps=8)

        tools = FunctionToolSet([Tool(
            ToolSchema("probe_db", "Query.", {"type": "object", "properties": {}}),
            lambda args: {"ok": False, "error": "no such column", "tool": "probe_db"})])
        new_result, new = kernel_run(script, presets.engine_agent(8), tools=tools)
        same_requests(old, new)
        assert turn.reply == new_result.text
        nudge = new.requests[3]["messages"][-1]["content"]
        assert nudge.startswith("SYSTEM: `probe_db` has now failed 3 times")
        assert any(f.source == "tool" and f.severity == "major"
                   for f in new_result.findings)
