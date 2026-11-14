"""AP-P6 (BOSSAPP-1057): CONTEXT ENGINEERING AND THE MEMORY API — the acceptance tests of
docs/design/AGENT_PLATFORM.md §5 Phase 6, each on the scripted engine (no model spend), plus the
enforcing tests of every deliverable.

  (a) a long agent run whose results used to hit the 16k CUT loses nothing: each large result is
      offloaded to the run filesystem and the agent GREPS the file to answer a seeded needle;
  (b) a 300-message chat thread stays under the model window with no 12-message slice, and the
      agent recalls a decision from message 5 (the summary deliberately drops it: the agent
      finds it in the archived originals);
  (c) the cache-read share of a repeated agent step, measured in llm_calls against a wire-level
      simulation of Anthropic's prompt cache, is at least the pre-change baseline;
  (d) a lesson written in run A reaches run B of the same agent only after a person reviews
      it, never another agent, and decays per kg/learning.py.

Run (a)-(c) anywhere; (d), the memory API and the KG-in-chat tests need BOSS_TEST_DATABASE_URL.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from boss_engine import kernel
from boss_engine.kernel import presets, runfs
from boss_engine.kernel.anthropic_engine import AnthropicMessagesEngine
from boss_engine.kernel.checkpoint import MemoryStore
from boss_engine.kernel.context_policy import (ContextConfig, ContextPolicyMiddleware,
                                               SUMMARY_SYSTEM, thread_memo_clear)
from boss_engine.kernel.engine import Block, EngineTraits, ModelRequest, estimate_tokens
from boss_engine.kernel.scripted import Call, Reply, Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema
from boss_engine.services.tool_result_text import MARKER

from ._ambient_support import eng, project  # noqa: F401

CUT_WORDS = MARKER.split("{")[0].strip()  # "…[CUT: this tool result was"
OBJ = {"type": "object", "properties": {"page": {"type": "integer"}},
       "additionalProperties": False}


def _last_tool(messages: list) -> dict:
    return json.loads(next(m["content"] for m in reversed(messages) if m.get("role") == "tool"))


def _rows(page: int, *, needle_at: int = -1) -> dict:
    """One page of an audit log: ~30k characters, far past the 16,000-character clip."""
    rows = [{"seq": page * 1000 + i, "actor": "deploy-bot", "event": f"heartbeat {i} ok"}
            for i in range(600)]
    if 0 <= needle_at < len(rows):
        rows[needle_at]["event"] = "ROTATED signing key kid=K-7731 for tenant acme at 03:14Z"
    return {"page": page, "rows": rows}


def _audit_tools(needle_page: int = 3, needle_at: int = 512) -> FunctionToolSet:
    return FunctionToolSet([Tool(ToolSchema("read_audit_log", "Read one page of the audit log.",
                                            OBJ),
                                 lambda a: _rows(int(a.get("page") or 0), needle_at=needle_at
                                                 if int(a.get("page") or 0) == needle_page
                                                 else -1))])


# ════ (a) offload, not CUT: the needle survives a long run ════════════════════════════════════

class TestOffloadNotCut:
    def _script(self, pages: int = 6) -> list:
        script: list = [Call("read_audit_log", {"page": p}) for p in range(pages)]

        def grep(messages, tools):
            # The model reads the stub it was given and searches every offloaded result.
            stub = _last_tool(messages)
            assert stub["status"] == "offloaded" and stub["path"].startswith(runfs.LARGE)
            return Call("grep", {"pattern": r"ROTATED signing key", "path": runfs.LARGE})

        def answer(messages, tools):
            found = _last_tool(messages)["matches"]
            assert len(found) == 1, found
            kid = found[0]["text"].split("kid=")[1].split()[0]
            return Say(f"The key rotated was {kid} (found in {found[0]['path']}).")

        return script + [Reply(grep), Reply(answer)]

    def test_before_p6_the_needle_was_cut_away(self):
        """The pre-change behaviour, kept for every caller without the context policy: the
        16,000-character CUT, and the needle on page 3 row 512 is never seen."""
        engine = ScriptedEngine([Call("read_audit_log", {"page": 3}), Say("not found")])
        kernel.run(kernel.RunSpec(engine=engine, tools=_audit_tools(), project_id="p",
                                  policies=presets.agent_loop(4), system="s", sleep=lambda s: 0,
                                  messages=[{"role": "user", "content": "Which key rotated?"}]))
        seen = engine.requests[1]["messages"][-1]["content"]
        assert CUT_WORDS in seen and "ROTATED" not in seen

    def test_a_long_run_greps_the_offloaded_results_and_answers_the_needle(self):
        engine = ScriptedEngine(self._script())
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=_audit_tools(), policies=presets.agent_loop(12), project_id="p",
            system="You audit deploys.", sleep=lambda s: 0,
            messages=[{"role": "user", "content": "Which signing key was rotated, and when?"}],
            middleware=(ContextPolicyMiddleware(),)))
        assert result.status == "completed"
        assert "K-7731" in result.text
        # NOTHING WAS CUT on any call; each large result reached the model as a stub.
        for request in engine.requests:
            for message in request["messages"]:
                assert CUT_WORDS not in str(message.get("content") or "")
        stubs = [json.loads(m["content"]) for m in engine.requests[-1]["messages"]
                 if m.get("role") == "tool" and '"offloaded"' in str(m.get("content"))]
        assert len(stubs) == 6
        assert all(s["chars"] > 16_000 and len(s["preview"].split("\n")) <= 10 for s in stubs)
        # The trace and the ledger keep the whole result (only the model's view is a stub).
        assert len(result.trace[3]["result"]["rows"]) == 600
        kinds = [e.kind for e in result.events if e.kind == "context.compacted"]
        assert len(kinds) == 6

    def test_the_threshold_is_the_smaller_of_offload_tokens_and_the_presets_clip(self):
        from boss_engine.kernel.context_policy import offload_threshold_chars
        from boss_engine.kernel.policies import ClipContext

        assert offload_threshold_chars(ClipContext(clip=16_000), ContextConfig()) == 16_000
        assert offload_threshold_chars(ClipContext(clip=None), ContextConfig()) == 80_000
        assert offload_threshold_chars(ClipContext(clip=16_000),
                                       ContextConfig(offload_tokens=1_000)) == 4_000

    def test_a_small_result_is_shown_whole_and_fs_tools_are_never_offloaded(self):
        tools = FunctionToolSet([Tool(ToolSchema("small", "s", OBJ), lambda a: {"ok": 1})])
        engine = ScriptedEngine([Call("small", {}), Call("write_file", {
            "path": "/scratch/big.md", "content": "x" * 50_000}),
            Call("read_file", {"path": "/scratch/big.md"}), Say("done")])
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=tools, policies=presets.agent_loop(6), project_id="p",
            system="s", messages=[{"role": "user", "content": "go"}], sleep=lambda s: 0,
            middleware=(ContextPolicyMiddleware(),)))
        assert result.status == "completed"
        assert json.loads(engine.requests[1]["messages"][-1]["content"]) == {"ok": 1}
        page = json.loads(engine.requests[3]["messages"][-1]["content"])
        assert page["path"] == "/scratch/big.md" and "offloaded" not in page

    def test_the_files_ride_in_the_checkpoint(self):
        store = MemoryStore()
        engine = ScriptedEngine([Call("read_audit_log", {"page": 3}), Say("ok")])
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=_audit_tools(), policies=presets.agent_loop(4), project_id="p",
            system="s", messages=[{"role": "user", "content": "go"}], sleep=lambda s: 0,
            persist=True, store=store, middleware=(ContextPolicyMiddleware(),)))
        saved = store.load(result.run_id)
        restored = kernel.state.RunState.from_checkpoint(saved.to_checkpoint())
        hit = runfs.RunFS(restored).grep("kid=K-7731", runfs.LARGE)
        assert hit["matches"] and hit["matches"][0]["path"].startswith(runfs.LARGE)


# ════ The run filesystem ══════════════════════════════════════════════════════════════════════

class _State:
    def __init__(self):
        self.extra: dict = {}
        self.step = 0


class TestRunFS:
    def test_routes_paging_grep_and_read_only_roots(self):
        fs = runfs.RunFS(_State())
        assert [e["path"] for e in fs.ls("/")["entries"]] == list(runfs.ROUTES)
        fs.write("/scratch/notes.md", "\n".join(f"line {i}" for i in range(500)))
        first = fs.read("/scratch/notes.md", limit=100)
        assert first["total_lines"] == 500 and first["next_offset"] == 100
        assert first["content"].split("\n")[0].endswith("line 0")
        tail = fs.read("/scratch/notes.md", offset=450, limit=100)
        assert "next_offset" not in tail and tail["content"].split("\n")[-1].endswith("line 499")
        assert fs.grep("line 49[0-9]$", "/scratch")["matches"][0]["line"] == 491
        assert fs.grep("(unclosed", "/")["matches"] == []  # a bad regex is a literal
        for path in ("/large_tool_results/x", "/skills/x/SKILL.md", "/etc/passwd"):
            with pytest.raises(runfs.FSError):
                fs.write(path, "no")
        assert runfs.normalise("/scratch/../scratch/./a.md") == "/scratch/a.md"
        with pytest.raises(runfs.FSError, match="no memory store"):
            fs.read("/memories/project")

    def test_the_registry_catalogues_the_tools_and_mcp_never_lists_them(self):
        from boss_engine.tools.builtin import build_default_registry

        reg = build_default_registry()
        specs = {s.name: s for s in reg.specs() if s.name in runfs.RUN_FS_TOOLS}
        assert set(specs) == set(runfs.RUN_FS_TOOLS)
        assert specs["write_file"].kind == "write" and specs["write_file"].idempotency
        assert {s.permission for n, s in specs.items() if n != "write_file"} == {"system:read"}
        out = reg.invoke("ls", None, project_id="p", actor="operator", args={})
        assert out["error"] == "no_run_filesystem"
        from boss_engine.mcp import server as mcp_server

        assert mcp_server._RUN_FS_TOOLS == runfs.RUN_FS_TOOLS


# ════ Compaction: clear, then summarise, originals archived ═══════════════════════════════════

def _small_window_engine(script, window=8_000):
    return ScriptedEngine(script, traits=EngineTraits(family="scripted", context_window=window))


class TestCompaction:
    def test_old_results_are_cleared_once_and_kept_whole_on_the_fs(self):
        tools = FunctionToolSet([Tool(ToolSchema("read", "r", OBJ),
                                      lambda a: {"blob": "y" * 6_000, "page": a.get("page")})])
        script = [Call("read", {"page": i}) for i in range(7)] + [Say("done")]
        engine = _small_window_engine(script, window=16_000)
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=tools, policies=presets.agent_loop(10), project_id="p",
            system="s", messages=[{"role": "user", "content": "read them all"}],
            sleep=lambda s: 0, middleware=(ContextPolicyMiddleware(),)))
        assert result.status == "completed"
        last = engine.requests[-1]["messages"]
        tool_bodies = [json.loads(m["content"]) for m in last if m.get("role") == "tool"]
        cleared = [b for b in tool_bodies if b.get("status") == "cleared"]
        assert cleared and len(tool_bodies) - len(cleared) >= 4  # the newest four stay whole
        # CACHE-STABLE: clearing happens in sweeps when the threshold is crossed, never on
        # every call; between sweeps each request extends the previous one byte for byte, and
        # a cleared message never changes again.
        sweeps = [e for e in result.events if e.kind == "context.compacted"
                  and e.data.get("how") == "clear"]
        assert 1 <= len(sweeps) < len(engine.requests) // 2
        prefix_kept = 0
        for before, after in zip(engine.requests, engine.requests[1:]):
            old, new = before["messages"], after["messages"]
            if new[:len(old)] == old:
                prefix_kept += 1
            for a, b in zip(old, new):
                if '"cleared"' in str(a.get("content")):
                    assert a == b
        assert prefix_kept >= len(engine.requests) - 1 - len(sweeps)

    def test_summary_at_85_percent_keeps_the_task_and_archives_the_originals(self):
        """A governed summary call (its own receipt), the task pinned, alternation kept, every
        original message in /scratch/conversation_history, greppable."""
        tools = FunctionToolSet([Tool(ToolSchema("read", "r", OBJ),
                                      lambda a: {"note": f"finding {a.get('page')} " + "z" * 2_000})])

        def model(messages, tools_offered):
            if messages[0]["role"] == "system" and messages[0]["content"] == SUMMARY_SYSTEM:
                return Say("SUMMARY: pages were read; nothing unusual.")
            reads = sum(1 for m in messages if m.get("role") == "tool")
            if reads < 12:
                return Call("read", {"page": reads})
            return Say("finished")

        engine = _small_window_engine([Reply(model)] * 40, window=6_000)
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=tools, policies=presets.agent_loop(20), project_id="p",
            system="s", messages=[{"role": "user", "content": "TASK: read twelve pages"}],
            sleep=lambda s: 0, middleware=(ContextPolicyMiddleware(ContextConfig(
                clear_at=0.99)),)))
        assert result.status == "completed"
        summaries = [r for r in engine.requests if r["messages"][0]["content"] == SUMMARY_SYSTEM]
        assert summaries, "the summary is a model call"
        receipts = [e for e in result.events if e.kind == "model.response"
                    and e.data.get("purpose") == "context.summarise"]
        assert len(receipts) == len(summaries)
        turn = [r for r in engine.requests if r["messages"][0]["content"] != SUMMARY_SYSTEM][-1]
        convo = turn["messages"][1:]
        assert convo[0]["role"] == "user" and convo[0]["content"].startswith("TASK: read twelve")
        assert "SUMMARY: pages were read" in convo[0]["content"]
        roles = [m["role"] for m in convo if m["role"] != "tool"]
        assert all(a != b or a == "assistant" for a, b in zip(roles, roles[1:]))
        assert estimate_tokens(ModelRequest(system=[Block("s")], messages=convo)) < 6_000

    def test_a_summariser_that_fails_falls_back_to_the_digest(self):
        from boss_engine.kernel.scripted import Fail

        def model(messages, tools_offered):
            if messages[0]["content"] == SUMMARY_SYSTEM:
                return Fail(ValueError("400 invalid request"))
            if sum(1 for m in messages if m.get("role") == "tool") < 10:
                return Call("read", {"page": 1})
            return Say("done")

        tools = FunctionToolSet([Tool(ToolSchema("read", "r", OBJ), lambda a: {"n": "q" * 2_400})])
        engine = _small_window_engine([Reply(model)] * 40, window=6_000)
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=tools, policies=presets.agent_loop(14), project_id="p",
            system="s", messages=[{"role": "user", "content": "go"}], sleep=lambda s: 0,
            middleware=(ContextPolicyMiddleware(ContextConfig(clear_at=0.99)),)))
        assert result.status == "completed"
        compactions = [e.data for e in result.events if e.kind == "context.compacted"
                       and e.data.get("how") == "summary"]
        assert compactions and compactions[0]["summariser"] == "extractive"

    def test_the_protocols_compaction_is_no_longer_a_no_op(self):
        from boss_engine.kernel.policies import ClipContext
        from boss_engine.kernel.state import RunState

        state = RunState(run_id="r", project_id="p", lineage_id="r", system=[],
                         messages=[{"role": "user", "content": "task"}] + [
                             {"role": "assistant" if i % 2 else "user", "content": "w" * 2_000}
                             for i in range(60)])
        policy = ClipContext()
        assert policy.needs_compaction(state, 20_000)
        policy.compact(state, _small_window_engine([], window=20_000))
        assert not policy.needs_compaction(state, 20_000)
        assert runfs.RunFS(state).ls(runfs.SCRATCH)["entries"]


# ════ Skills on demand ════════════════════════════════════════════════════════════════════════

class TestSkillsOnDemand:
    def test_only_the_description_is_in_the_prompt_and_the_body_is_read_when_needed(self):
        body = "STEP 1: run the smoke suite.\n" + "detail " * 2_000
        skills = ({"name": "release_check", "description": "How to check a release is safe",
                   "body": body},)

        def model(messages, tools_offered):
            if not any(m.get("role") == "tool" for m in messages):
                return Call("read_file", {"path": "/skills/release_check/SKILL.md", "limit": 1})
            return Say(_last_tool(messages)["content"].split("\t", 1)[1])

        engine = ScriptedEngine([Reply(model), Reply(model)])
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=FunctionToolSet([]), policies=presets.agent_loop(4),
            project_id="p", system="PRIMER", messages=[{"role": "user", "content": "ship it"}],
            sleep=lambda s: 0, middleware=(ContextPolicyMiddleware(ContextConfig(
                skills=skills)),)))
        system = engine.requests[0]["messages"][0]["content"]
        assert system.startswith("PRIMER") and "release_check: How to check a release" in system
        assert "detail detail" not in system, "the body is not in the prompt"
        assert result.text == "STEP 1: run the smoke suite."

    def test_instructions_written_as_skill_md_are_left_out_of_an_on_demand_prompt(self,
                                                                                    monkeypatch):
        from boss_engine.services import agent_instructions as ai

        docs = [{"name": "Release", "sha256": "a", "body": "---\nname: release-check\n"
                 "description: How to check a release\n---\nSTEP 1"},
                {"name": "Tone", "sha256": "b", "body": "Be terse."}]
        monkeypatch.setattr(ai, "_for_prompt", lambda *a, **k: [dict(d) for d in docs])
        assert [d["name"] for d in ai.for_prompt(None, "p", "qa", "work")] == ["Release", "Tone"]
        assert [d["name"] for d in ai.for_prompt(None, "p", "qa", "work",
                                                 skills="on_demand")] == ["Tone"]
        assert ai.skills_for(None, "p", "qa") == [
            {"name": "release_check", "description": "How to check a release", "body": "STEP 1"}]


# ════ Scoped tools on every preset; cache markers on every Anthropic path ═════════════════════

def test_every_preset_with_tools_scopes_them(monkeypatch):
    from boss_engine.kernel.scoped_tools import ScopedTools

    monkeypatch.delenv("BOSS_SCOPED_TOOLS", raising=False)
    for policies in (presets.agent_loop(4), presets.engine_agent(4), presets.native_coder(4),
                     presets.meeting_seat()):
        assert isinstance(policies.tools, ScopedTools), policies.tools


def test_a_claude_model_behind_an_openai_gateway_gets_cache_markers():
    from boss_engine.kernel.engine import OpenAIChatEngine

    sent: list = []

    class Adapter:
        settings = type("S", (), {"openai_model": "anthropic/claude-sonnet-4.5"})()

        def create_chat_completion(self, messages, **kw):
            sent.append(messages)
            return {"content": "ok", "tool_calls": None}, {"prompt_tokens": 5}

    OpenAIChatEngine(Adapter()).complete(ModelRequest(
        system=[Block("stable", cache=True), Block("changing")],
        messages=[{"role": "user", "content": "hi"}]))
    system, user = sent[0][0], sent[0][-1]
    assert system["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system["content"][1]
    assert user["content"][0]["cache_control"] == {"type": "ephemeral"}

    class OpenAI(Adapter):
        settings = type("S", (), {"openai_model": "gpt-4o"})()

    sent.clear()
    OpenAIChatEngine(OpenAI()).complete(ModelRequest(system=[Block("stable", cache=True)],
                                                     messages=[{"role": "user", "content": "hi"}]))
    assert sent[0][0] == {"role": "system", "content": "stable"}


def test_the_gateways_cached_tokens_are_recorded_for_claude():
    from types import SimpleNamespace

    from boss_engine.services.openai_adapter import _usage_from_response

    usage = SimpleNamespace(prompt_tokens=1_000, completion_tokens=10, total_tokens=1_010,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=800))
    got = _usage_from_response(SimpleNamespace(usage=usage), "anthropic/claude-sonnet-4.5")
    assert (got["prompt_tokens"], got["cache_read_tokens"]) == (200, 800)
    plain = _usage_from_response(SimpleNamespace(usage=usage), "gpt-4o")
    assert plain["prompt_tokens"] == 1_000 and "cache_read_tokens" not in plain


def test_the_agent_spec_context_field_configures_the_policy():
    from boss_engine.agents.spec import load_spec

    spec = load_spec({"apiVersion": "boss.agent/v1", "kind": "Agent",
                      "metadata": {"key": "qa"},
                      "spec": {"context": {"offload_tokens": 5_000, "summarise_at": 0.7,
                                           "keep": 0.2, "clear_keep": 2}}})
    config = ContextConfig.from_spec(spec.context)
    assert (config.offload_tokens, config.summarise_at, config.keep, config.clear_keep,
            config.clear_at) == (5_000, 0.7, 0.2, 2, 0.5)
    assert ContextConfig() == ContextConfig.from_spec(None)
    assert (ContextConfig().offload_tokens, ContextConfig().summarise_at,
            ContextConfig().keep) == (20_000, 0.85, 0.10)


# ════ (b) a 300-message chat thread ═══════════════════════════════════════════════════════════

DECISION = "DECISION: we ship the billing migration on Thursday behind flag BILL-77"


def _thread(n: int = 300) -> list:
    out = []
    for i in range(1, n + 1):
        role = "user" if i % 2 else "assistant"
        words = f"message {i}: " + ("routine status chatter about the dashboards. " * 14)
        out.append({"role": role, "content": DECISION if i == 5 else words})
    return out


def _chat_model(summaries: list):
    def model(messages, tools_offered):
        if messages[0]["role"] == "system" and messages[0]["content"] == SUMMARY_SYSTEM:
            summaries.append(messages[-1]["content"])
            # A summary that DROPS the decision: recall must come from the archive.
            return Say("The team discussed dashboards at length.")
        last = messages[-1]
        if last.get("role") == "tool":
            hit = _last_tool(messages)["matches"][0]["text"]
            return Say(f"From the archived thread: {hit}")
        archive = next(re.search(r"(/scratch/conversation_history/\d+\.md)",
                                 str(m["content"])).group(1)
                       for m in messages if "archived word for word at" in str(m.get("content")))
        return Call("grep", {"pattern": "DECISION", "path": archive})

    return model


class TestThreeHundredMessageThread:
    def _turn(self, monkeypatch, history, engine):
        from boss_engine.agent import EngineAgent, transient
        from boss_engine.tools.registry import ToolRegistry

        monkeypatch.setenv("BOSS_KERNEL_CHAT", "1")
        monkeypatch.setattr(transient.time, "sleep", lambda s: None)
        agent = EngineAgent(ctx=object(), registry=ToolRegistry())
        agent._adapter = engine
        agent.ambient = {"_thread_id": "thr_p6"}
        return agent.chat(project_id="p", message="When do we ship the billing migration?",
                          system="You are BOSS.", history=history, max_steps=6)

    def test_no_12_message_slice_reaches_the_turn(self):
        from boss_engine.services.intent_router import conversation_history

        thread = _thread()
        assert conversation_history(thread) == thread

    def test_the_thread_stays_in_the_window_and_message_5_is_recalled(self, monkeypatch):
        thread_memo_clear()
        summaries: list = []
        engine = ScriptedEngine([Reply(_chat_model(summaries))] * 8)
        history = _thread()
        assert estimate_tokens(ModelRequest(system=[], messages=history)) > 48_000
        turn = self._turn(monkeypatch, history, engine)
        assert "Thursday" in turn.reply and "BILL-77" in turn.reply
        assert len(summaries) == 1
        for request in engine.requests:
            sent = ModelRequest(system=[], messages=request["messages"])
            assert estimate_tokens(sent) < 48_000, "every call fits the chat window"
        # The whole thread is accounted for: the archived originals + the kept tail = 300.
        sent = engine.requests[-1]["messages"]
        head = next(str(m["content"]) for m in sent if "archived word for word" in
                    str(m.get("content")))
        archived = int(re.search(r"the (\d+) original messages", head).group(1))
        kept = [m for m in sent if str(m.get("content") or "").startswith("message ")]
        assert archived + len(kept) == 300, (archived, len(kept))
        assert archived > 200 and kept, "most of the thread was folded, the newest kept"

    def test_the_next_turn_reuses_the_summary_with_no_second_call(self, monkeypatch):
        thread_memo_clear()
        summaries: list = []
        history = _thread()
        self._turn(monkeypatch, history, ScriptedEngine([Reply(_chat_model(summaries))] * 8))
        history += [{"role": "user", "content": "When do we ship the billing migration?"},
                    {"role": "assistant", "content": "Thursday, behind BILL-77."}]
        engine = ScriptedEngine([Reply(_chat_model(summaries))] * 8)
        turn = self._turn(monkeypatch, history, engine)
        assert len(summaries) == 1, "the thread's summary is reused, not paid for again"
        assert "BILL-77" in turn.reply


def test_the_legacy_loop_bounds_a_long_thread_without_a_fixed_slice():
    from boss_engine.agent.engine_agent import bounded_history

    history = _thread()
    bounded = bounded_history(history, 48_000)
    assert estimate_tokens(ModelRequest(system=[], messages=bounded)) < 48_000
    assert "CONDENSED" in bounded[0]["content"] and "[5]" in bounded[0]["content"]
    short = _thread(30)
    assert bounded_history(short, 48_000) == short


# ════ (c) the cache-read share of a repeated agent step ═══════════════════════════════════════

class CachingMessagesAPI:
    """Anthropic's prompt cache, simulated at the wire (the real `anthropic` client on an httpx
    MockTransport). The request is read in the provider's order — tools, system, then each
    message's content blocks — and a prefix is cached at every `cache_control` breakpoint that
    is at least `min_tokens` long. A later request reads the longest cached prefix found at one
    of its breakpoints or up to 20 blocks before it (the provider's lookback); what lies between
    that and its last breakpoint is written; the rest is uncached input. chars/4 tokens."""

    def __init__(self, turns, *, min_tokens: int = 1_024):
        self.turns = list(turns)
        self.min_tokens = min_tokens
        self.cache: set = set()

    @staticmethod
    def segments(body: dict) -> list:
        def bare(value):
            return {k: v for k, v in value.items() if k != "cache_control"}

        out = [(json.dumps(bare(t), sort_keys=True), "cache_control" in t)
               for t in body.get("tools") or []]
        out += [(json.dumps(bare(b), sort_keys=True), "cache_control" in b)
                for b in body.get("system") or []]
        for message in body.get("messages") or []:
            content = message["content"]
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            out += [(json.dumps({"role": message["role"], **bare(b)}, sort_keys=True),
                     "cache_control" in b) for b in blocks]
        return out

    def usage(self, body: dict) -> dict:
        segs = self.segments(body)
        cum, hashes, total, digest = [], [], 0, hashlib.sha256()
        for seg, _marked in segs:
            total += max(1, len(seg) // 4)
            digest.update(seg.encode())
            cum.append(total)
            hashes.append(digest.copy().hexdigest())
        marks = [i for i, (_s, marked) in enumerate(segs) if marked]
        read = 0
        for b in marks:
            for j in range(b, max(-1, b - 20), -1):
                if hashes[j] in self.cache:
                    read = max(read, cum[j])
                    break
        write = 0
        if marks and hashes[marks[-1]] not in self.cache and cum[marks[-1]] >= self.min_tokens:
            write = cum[marks[-1]] - read
        for b in marks:
            if cum[b] >= self.min_tokens:
                self.cache.add(hashes[b])
        return {"input_tokens": total - read - write, "output_tokens": 20,
                "cache_read_input_tokens": read, "cache_creation_input_tokens": write}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        turn = self.turns.pop(0)(body)
        return httpx.Response(200, json={"id": "msg", "type": "message", "role": "assistant",
                                         "model": "claude-haiku-4-5", "content": turn["content"],
                                         "stop_reason": turn["stop"], "stop_sequence": None,
                                         "usage": self.usage(body)})

    def client(self):
        import anthropic

        return anthropic.Anthropic(api_key="sk-fake", base_url="http://fake.local",
                                   max_retries=0,
                                   http_client=httpx.Client(transport=httpx.MockTransport(
                                       self.handler)))


class PreP6AnthropicEngine(AnthropicMessagesEngine):
    """THE BASELINE: main's request shaping before AP-P6 (c5750f933), verbatim — the tool list
    cached only when a system block was, up to three system breakpoints, no conversation
    breakpoint."""

    def _request_kwargs(self, req):
        from boss_engine.kernel import anthropic_engine as ae

        system, messages = ae._messages_param(req)
        marked = [i for i, b in enumerate(system) if "cache_control" in b]
        for i in marked[:max(0, len(marked) - 3)]:
            del system[i]["cache_control"]
        kwargs = {"model": self.model, "messages": messages,
                  "max_tokens": int(req.max_output_tokens or self.max_output_tokens)}
        if system:
            kwargs["system"] = system
        tools = ae._tools_param(req, strict=self.traits.strict_tools,
                                cache=self.traits.prompt_cache == "explicit"
                                and any(getattr(b, "cache", False) for b in req.system))
        if tools:
            kwargs["tools"] = tools
        return kwargs


def _step_turns(steps: int = 6) -> list:
    def call(n):
        return lambda body: {"stop": "tool_use", "content": [{
            "type": "tool_use", "id": f"toolu_{n}", "name": f"inspect_{n % 3}",
            "input": {"page": n}}]}

    return [call(n) for n in range(steps)] + [
        lambda body: {"stop": "end_turn", "content": [{"type": "text", "text": "All green."}]}]


def _step_tools() -> FunctionToolSet:
    return FunctionToolSet([Tool(ToolSchema(f"inspect_{k}", f"Inspect surface {k}. " * 20, OBJ),
                                 lambda a: {"page": a.get("page"), "lines": [
                                     f"line {i}: service healthy, p99 41ms" for i in range(120)]})
                            for k in range(3)])


PRIMER = ("You are a BOSS agent. " + "Follow the operating rules exactly. " * 400)


def _repeated_step(eng_, project_id, *, engine_cls, middleware) -> tuple:
    """The same agent step run twice (a workflow step on two runs), each call receipted into
    llm_calls by the kernel's ledger sink. Returns (cache-read share, run ids)."""
    from boss_engine.kernel.ledger import DbSink

    api = CachingMessagesAPI(_step_turns() + _step_turns())
    run_ids = []
    for _ in range(2):
        engine = engine_cls(api.client(), model="claude-haiku-4-5")
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=_step_tools(), policies=presets.agent_loop(10),
            project_id=project_id, system=PRIMER, sleep=lambda s: 0, sink=DbSink(eng_),
            messages=[{"role": "user", "content": "Check every surface and report."}],
            middleware=middleware))
        assert result.status == "completed", result.error
        run_ids.append(result.run_id)
    with eng_.connect() as c:
        row = c.execute(text(
            "SELECT sum(prompt_tokens) AS i, sum(cache_read_tokens) AS r, "
            "sum(cache_write_tokens) AS w, count(*) AS n FROM llm_calls WHERE run_id = ANY(:r)"),
            {"r": run_ids}).mappings().one()
    total = int(row["i"]) + int(row["r"]) + int(row["w"])
    return (int(row["r"]) / total if total else 0.0), int(row["n"]), run_ids


def test_c_the_cache_read_share_of_a_repeated_step_is_at_least_the_baseline(eng, project):  # noqa: F811
    before, calls_before, _ = _repeated_step(eng, project, engine_cls=PreP6AnthropicEngine,
                                             middleware=())
    after, calls_after, _ = _repeated_step(eng, project, engine_cls=AnthropicMessagesEngine,
                                           middleware=(ContextPolicyMiddleware(),))
    print(f"\ncache-read share of a repeated agent step (llm_calls, {calls_before} vs "
          f"{calls_after} calls): before AP-P6 {before:.1%}, after {after:.1%}")
    assert calls_before == calls_after == 14
    assert after >= before
    assert after >= 0.70, "the design's ≥70% cached-share bar (§4.3)"


# ════ (d) a lesson from run A reaches run B only after review, and decays ═════════════════════

def _lesson_run(eng_, project_id, role, lesson):
    """Run A: the agent records what it learned through its run filesystem's /memories."""
    from boss_engine.services import agent_loop

    engine = ScriptedEngine([Call("write_file", {"path": f"/memories/agent:{role}",
                                                 "content": lesson}), Say("noted")])
    with eng_.begin() as c:
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=FunctionToolSet([]), policies=presets.agent_loop(4),
            project_id=project_id, system="s", sleep=lambda s: 0, actor=f"agent:{role}",
            messages=[{"role": "user", "content": "deploy and report"}],
            middleware=agent_loop.context_middleware(c, project_id, role=role,
                                                     actor=f"agent:{role}")))
    assert result.status == "completed"
    return result.trace[0]["result"]


def _run_b_prompt(eng_, project_id, role):
    from boss_engine.services import agent_loop

    with eng_.begin() as c:
        return agent_loop._with_memory(c, project_id, "BRIEF", "deploy the release and report",
                                       "arun_b", {"role_key": role}) or ""


def test_d_a_lesson_from_run_a_reaches_run_b_only_after_review_and_decays(eng, project):  # noqa: F811
    from boss_engine.services import memory_api

    lesson = "Always run the smoke suite before reporting a deploy done."
    written = _lesson_run(eng, project, "qa", lesson)
    assert written["status"] == "proposed" and "PROPOSED" in written["note"]
    assert lesson not in _run_b_prompt(eng, project, "qa"), "unreviewed: never injected"

    with eng.begin() as c:
        memory_api.review(c, project_id=project, memory_id=written["id"], actor="person:ops",
                          decision="active")
    assert lesson in _run_b_prompt(eng, project, "qa")
    assert lesson not in _run_b_prompt(eng, project, "pm"), "another agent's lesson stays its own"

    # DECAY (kg/learning.py): unused for 200 days, its weight halves past the KG's drop line.
    with eng.begin() as c:
        c.execute(text("UPDATE agent_memory SET created_at = now() - interval '200 days', "
                       "reviewed_at = now() - interval '200 days', last_used_at = NULL "
                       "WHERE id = :i"), {"i": written["id"]})
        rows = memory_api.recall(c, project_id=project, role="qa")
    assert lesson not in _run_b_prompt(eng, project, "qa")
    mine = next(r for r in rows if r["id"] == written["id"])
    assert mine["dropped"] and mine["weight"] < 0.3


def test_the_decay_rule_is_the_kg_learning_rule():
    from boss_engine.services.knowledge.learning import decay, usefulness
    from boss_engine.services.memory_api import weight_of

    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    fresh = weight_of({"use_count": 0, "created_at": now}, now=now)
    old = weight_of({"use_count": 0, "created_at": now - timedelta(days=180)}, now=now)
    used = weight_of({"use_count": 3, "created_at": now - timedelta(days=400),
                      "last_used_at": now - timedelta(days=10)}, now=now)
    assert fresh["weight"] == usefulness(0, 0) and not fresh["dropped"]
    assert abs(old["weight"] - decay(usefulness(0, 0), 180)) < 1e-9 and old["dropped"]
    assert not used["dropped"]


# ════ The memory API: one interface, the tools routed through it, KG in chat ══════════════════

def test_namespaces_parse_and_the_stores_keep_their_tiers(eng, project):  # noqa: F811
    from boss_engine.services import memory_api as m

    assert str(m.parse("agent:self", defaults={"agent": "qa"})) == "agent:qa"
    assert m.parse("org:priors").sub == m.PRIORS and m.parse("project:kg").sub == "kg"
    with pytest.raises(m.MemoryAPIError):
        m.parse("kg")
    with eng.begin() as c:
        pref = m.write(c, "user:alice", "Answers in bullet points.", project_id=project,
                       actor="person:alice")
        ctxt = m.write(c, "project", "The billing service lives in svc-billing.",
                       project_id=project, actor="agent:qa")
        led = m.write(c, "thread:thr_1", "find why deploys stall", project_id=project,
                      actor="agent:qa")
        with pytest.raises(m.MemoryAPIError):
            m.write(c, "org:priors", "x", project_id=project, actor="agent:qa")
        both = m.read(c, ["project", "user:alice", "thread:thr_1"], project_id=project)
        other = m.read(c, ["project", "user:bob"], project_id=project)
    assert pref["status"] == ctxt["status"] == "active" and led["status"] == "active"
    assert "bullet points" in both["text"] and "svc-billing" in both["text"]
    assert "GOAL: find why deploys stall" in both["text"]
    assert "bullet points" not in other["text"], "a person's memory is theirs"


def test_remember_and_recall_go_through_the_memory_api(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import memory_api
    from boss_engine.tools.builtin import build_default_registry

    calls: list = []
    real_write = memory_api.write

    def spy(*a, **k):
        calls.append(a[1])
        return real_write(*a, **k)

    monkeypatch.setattr(memory_api, "write", spy)

    class Ctx:
        def begin(self):
            return eng.begin()

    reg = build_default_registry()
    spec = reg.get("remember")
    out = spec.handler(Ctx(), project_id=project, actor="person:ops",
                       args={"kind": "lesson", "body": "Check the canary first.",
                             "scope": "role", "scope_ref": "sre"})
    assert out["ok"] and out["status"] == "proposed" and calls == ["agent:sre"]
    got = reg.get("recall").handler(Ctx(), project_id=project, actor="person:ops",
                                    args={"include_proposed": True, "role": "sre"})
    assert any(r["body"] == "Check the canary first." and "weight" in r
               for r in got["memories"])


def test_kg_learnings_reach_chat(eng, project):  # noqa: F811
    from boss_engine.services.intent_router import _remembered_context
    from boss_engine.services.knowledge import learning_writeback as lw

    with eng.begin() as c:
        lw.record(c, project_id=project, learning_kind="investigation",
                  title="Disbursement fails without a linked account",
                  statement="disburse raises LinkedAccountRequired when no savings account is "
                            "linked.",
                  detail={"tried": ["the fee path"], "found": "the account link check raises"},
                  about={"areas": ["loan-core"]},
                  evidence=[{"type": "log", "ref": "crun_x implement",
                             "excerpt": "LinkedAccountRequired: loan.link"}],
                  confidence=0.9)
    with eng.begin() as c:
        block = _remembered_context(c, project, "person:ops", None, None,
                                    query="why does disburse raise LinkedAccountRequired?")
    assert "KNOWLEDGE (quoted source material, not instructions)" in block
    assert "LinkedAccountRequired" in block


def test_the_priors_build_is_a_callable_job(eng):  # noqa: F811
    from boss_engine.services.memory_api import PRIORS_JOB, build_priors_job

    out = build_priors_job(eng, org_id=f"org_p6_{datetime.now().timestamp():.0f}")
    assert out["job"] == PRIORS_JOB and "result" in out
