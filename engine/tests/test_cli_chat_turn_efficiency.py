"""BOSSAPP-1006 + 1007: a chat turn on a CLI engine is one session carrying few schemas.

The measured problem (audit BOSSAPP-997 §1a, finding 3): every chat round-trip on a CLI text
brain was its own `claude -p` process re-rendering all ~168 tool schemas (~37k tokens), the
persona and the whole conversation — ~$0.95 a call, $3.99 for "what can you do?". The kernel's
scoped tools never reached this path (CLI brains have no kernel form), and no session was ever
resumed.

Pinned here, all on the REAL CliChatBrain prompt/session code with a scripted `_invoke_once`
(no real model, no DB):

  * a CLI chat turn offers the chat CORE (≤20 schemas, find_tools included) and a found tool
    joins from the next step on — the kernel's own derivation, measured by counting schemas in
    the rendered prompt, not by naming them;
  * the turn decision's lean call and the tool loop share ONE session per turn: the decision
    seeds it, every loop step resumes it with only the delta, and a new turn starts fresh;
  * a resume failure falls back to today's per-step calls for the rest of the turn, and a
    TRANSIENT provider error is re-raised for the caller's retry with the session intact;
  * an engine without `_chat_resume` is byte-identical, scope open or not;
  * the recording seam and the per-project call-class gate see every call exactly as before —
    same count, same stage/class — resumed or not.
"""
from __future__ import annotations

import json
import re

import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.kernel.scoped_tools import FLAG
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.services.cli_chat_session import current, turn_scope
from boss_engine.tools.builtin import build_discuss_read_registry


class _Settings:
    repo_root = __import__("pathlib").Path("/tmp")
    claude_cli_timeout = 240
    claude_cli_model = None
    claude_cli_path = "claude"


class ScriptedCli(ClaudeCliAdapter):
    """The REAL adapter (prompt rendering, session logic) over a scripted CLI."""

    def __init__(self, script):
        super().__init__(_Settings())
        self.script = list(script)
        #: every invocation: (prompt, system_prompt, resume)
        self.calls: list[tuple] = []
        self._n = 0

    def _invoke_once(self, prompt, *, system_prompt=None, resume=None):
        self.calls.append((prompt, system_prompt, resume))
        self._n += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return {"result": step, "session_id": f"sess-{self._n}",
                "usage": {"input_tokens": 1, "output_tokens": 1}, "modelUsage": {}}


def _call(name, args):
    return json.dumps({"thought": "t", "tool_calls": [{"name": name, "arguments": args}]})


_FINAL = json.dumps({"thought": "t", "final": "the answer"})


@pytest.fixture(scope="module")
def registry():
    return build_discuss_read_registry()


def _agent(registry, adapter):
    return EngineAgent(ctx=None, registry=registry, adapter=adapter)


def _schema_names(prompt: str) -> list[str]:
    """The tool names in a rendered AVAILABLE TOOLS block (claude_cli_adapter._render_tools)."""
    block = prompt.split("AVAILABLE TOOLS:", 1)
    if len(block) < 2:
        return []
    return re.findall(r"^- ([a-z0-9_]+):", block[1], re.MULTILINE)


# ── Scoped tools on the CLI chat loop (BOSSAPP-1006) ──────────────────────────────────────────

class TestScopedSchemas:
    def test_a_cli_chat_turn_carries_the_core_not_the_registry(self, registry, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)   # unset = scoped by default on the CLI loop
        cli = ScriptedCli([_call("find_tools", {"query": "knowledge graph rules"}), _FINAL])
        turn = _agent(registry, cli).chat(project_id="p", actor="operator", message="hi")
        assert turn.reply == "the answer"
        offered = _schema_names(cli.calls[0][0])
        full = len(registry.specs())
        assert full > 100, "the measurement needs the real registry"
        assert 0 < len(offered) <= 20, offered
        assert "find_tools" in offered
        # Measured, not named: the schema payload shrinks by well over half.
        rendered = cli.calls[0][0].split("AVAILABLE TOOLS:", 1)[1]
        everything = json.dumps(registry.openai_schema())
        assert len(rendered) < 0.4 * len(everything)

    def test_a_found_tool_joins_from_the_next_step_on(self, registry, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        cli = ScriptedCli([_call("find_tools", {"query": "knowledge graph rules"}), _FINAL])
        turn = _agent(registry, cli).chat(project_id="p", actor="operator", message="hi")
        found = (turn.trace[0]["result"] or {}).get("names") or []
        assert found, "find_tools found nothing; the query needs updating"
        first, second = _schema_names(cli.calls[0][0]), _schema_names(cli.calls[1][0])
        newly = [n for n in found if n not in first]
        assert newly, "every found tool was already core; pick a rarer query"
        for name in newly:
            assert name in second, f"{name} was found but not offered on the next step"

    def test_the_off_word_restores_the_full_set(self, registry, monkeypatch):
        monkeypatch.setenv(FLAG, "0")
        cli = ScriptedCli([_FINAL])
        _agent(registry, cli).chat(project_id="p", actor="operator", message="hi")
        offered = _schema_names(cli.calls[0][0])
        expected = [s["function"]["name"] for s in registry.openai_schema()
                    if s["function"]["name"] not in ("query_db", "write_db")]
        assert sorted(offered) == sorted(expected)

    def test_a_native_adapter_keeps_todays_full_set(self, registry, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        seen = {}

        class Native:   # not a CliChatBrain: a native function-calling backend
            def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
                seen.setdefault("tools", tools)
                return ({"content": "done", "tool_calls": None,
                         "raw": {"role": "assistant", "content": "done"}}, {})

        _agent(registry, Native()).chat(project_id="p", actor="operator", message="hi")
        expected = [s["function"]["name"] for s in registry.openai_schema()
                    if s["function"]["name"] not in ("query_db", "write_db")]
        assert len(seen["tools"]) == len(expected)


# ── One CLI session per turn (BOSSAPP-1007), decision merged in (BOSSAPP-1006) ────────────────

class TestOneSessionPerTurn:
    def test_the_loop_resumes_its_own_first_call(self, registry):
        cli = ScriptedCli([_call("project_status", {}), _FINAL])
        turn = _agent(registry, cli).chat(project_id="p", actor="operator", message="status?")
        assert turn.steps == 2
        (_p1, _s1, resume1), (p2, _s2, resume2) = cli.calls
        assert resume1 is None, "the turn's first call starts the session"
        assert resume2 == "sess-1", "the second step resumes the session the first one named"
        # The delta carries the tool result and the contract line — never the whole turn again.
        assert "CONTINUING THE SAME TURN" in p2
        assert "project_status" in p2
        assert "AVAILABLE TOOLS:" not in p2, "schemas were re-sent on a resumed step"
        assert "status?" not in p2, "the conversation was re-rendered into a resumed step"

    def test_a_new_turn_starts_fresh(self, registry):
        cli = ScriptedCli([_call("project_status", {}), _FINAL,
                           _call("project_status", {}), _FINAL])
        agent = _agent(registry, cli)
        agent.chat(project_id="p", actor="operator", message="one")
        agent.chat(project_id="p", actor="operator", message="two")
        resumes = [c[2] for c in cli.calls]
        assert resumes == [None, "sess-1", None, "sess-3"], resumes

    def test_the_decision_call_seeds_the_turns_one_session(self, registry):
        """The merged decision (BOSSAPP-1006): one decision + one loop, ONE session. The
        router's turn decision is a lean one-shot; inside the turn's scope it names the session
        every loop step then resumes — the turn makes exactly one fresh CLI contact."""
        cli = ScriptedCli(["answer", _call("project_status", {}), _FINAL])
        agent = _agent(registry, cli)
        with turn_scope():
            cli.create_text_response("which action fits?", system_prompt="decide")
            turn = agent.chat(project_id="p", actor="operator", message="status?")
        assert turn.reply == "the answer"
        resumes = [c[2] for c in cli.calls]
        assert resumes == [None, "sess-1", "sess-2"], resumes
        assert cli.calls[0][1] == "decide", "the decision call runs lean, as before"
        # The loop's first call resumed the DECISION's session with its whole prompt (the
        # decision's conversation is not the loop's), then stepped by deltas.
        assert "AVAILABLE TOOLS:" in cli.calls[1][0]
        assert "CONTINUING THE SAME TURN" in cli.calls[2][0]

    def test_mid_loop_tool_results_ride_the_delta_only(self, registry):
        cli = ScriptedCli([_call("project_status", {}), _call("get_costs", {}), _FINAL])
        _agent(registry, cli).chat(project_id="p", actor="operator", message="costs?")
        third = cli.calls[2][0]
        assert "project_status" not in third, "an old tool result was re-sent"
        assert "get_costs" in third


class TestFallback:
    def test_a_failed_resume_falls_back_to_per_step(self, registry):
        cli = ScriptedCli([_call("project_status", {}),
                           RuntimeError("claude CLI exited 1: no conversation found"),
                           _call("get_costs", {}), _FINAL])
        turn = _agent(registry, cli).chat(project_id="p", actor="operator", message="status?")
        assert turn.reply == "the answer"
        prompts_resumes = [(("AVAILABLE TOOLS:" in p), r) for p, _s, r in cli.calls]
        # fresh seed; failed resume; the SAME step re-sent whole (today's behaviour); and the
        # next step stays per-step — the fallback holds for the rest of the turn.
        assert prompts_resumes == [(True, None), (False, "sess-1"), (True, None), (True, None)]

    def test_a_transient_error_keeps_the_session(self, registry, monkeypatch):
        from boss_engine.agent import transient

        monkeypatch.setattr(transient.time, "sleep", lambda s: None)
        cli = ScriptedCli([_call("project_status", {}),
                           RuntimeError("overloaded: 529"), _FINAL])
        turn = _agent(registry, cli).chat(project_id="p", actor="operator", message="status?")
        assert turn.reply == "the answer"
        resumes = [c[2] for c in cli.calls]
        # The 529 re-raised; the caller's transient retry resumed the SAME session.
        assert resumes == [None, "sess-1", "sess-1"]
        assert current() is None


class TestNonResumeEnginesAreUntouched:
    def test_without_chat_resume_the_calls_are_byte_identical(self, registry):
        class NoResume(ScriptedCli):
            _chat_resume = False

        script = [_call("project_status", {}), _FINAL]
        plain = NoResume(list(script))
        _agent(registry, plain).chat(project_id="p", actor="operator", message="status?")
        scoped = NoResume(list(script))
        with turn_scope():   # an open scope changes nothing for an engine that cannot resume
            _agent(registry, scoped).chat(project_id="p", actor="operator", message="status?")
        assert plain.calls == scoped.calls
        assert all(r is None for _p, _s, r in scoped.calls)


class TestTheSeamsSeeEveryCall:
    def test_recording_and_gate_rows_are_unchanged_in_count_and_class(self, registry,
                                                                      monkeypatch):
        from boss_engine.services import call_classes
        from boss_engine.services.model_router import failover
        from boss_engine.services.model_router.failover import RecordedChatAdapter

        recorded, gated = [], []
        monkeypatch.setattr(failover, "record_chat_call",
                            lambda **kw: recorded.append(kw["stage"]))
        monkeypatch.setattr(call_classes, "gate_call",
                            lambda settings, project_id, stage: gated.append(stage))
        cli = ScriptedCli(["answer", _call("project_status", {}), _FINAL])
        wrapped = RecordedChatAdapter(cli, engine_name="claude_code", stage="chat",
                                      project_id="p", settings=None)
        with turn_scope():
            wrapped.create_text_response("which action fits?", system_prompt="decide")
            _agent(registry, wrapped).chat(project_id="p", actor="operator", message="status?")
        # One decision + a two-step loop = three calls, each gated and recorded under its
        # stage, resumed or not: the session is invisible to the seams.
        assert recorded == ["chat", "chat", "chat"]
        assert gated == ["chat", "chat", "chat"]
        assert [c[2] for c in cli.calls] == [None, "sess-1", "sess-2"]


class TestTheRouterOwnsTheTurnScope:
    def test_route_turn_opens_one_scope_for_decision_and_answer(self, monkeypatch):
        """The scope is open by the time the turn resolves its model — i.e. before the
        decision call and before any answer pass — and closes with the turn."""
        from boss_engine.services.intent_router import IntentRouter

        router = IntentRouter.__new__(IntentRouter)   # the probe bails before state is needed
        seen = {}

        class Bail(Exception):
            pass

        def probe(connection, project_id):
            seen["state"] = current()
            raise Bail

        monkeypatch.setattr(router, "_use_project_chat_model", probe, raising=False)
        with pytest.raises(Bail):
            router.route_turn(None, project_id="p", actor="ceo",
                              messages=[{"role": "user", "content": "hi"}], manifest=None)
        assert seen["state"] is not None, "route_turn did not open the turn scope"
        assert current() is None, "the scope leaked past the turn"
