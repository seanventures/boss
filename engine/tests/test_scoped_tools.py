"""K5 (BOSSAPP-961): a kernel chat run carries ~20 schemas and finds the rest, behind
BOSS_SCOPED_TOOLS.

With the flag OFF nothing changes: every registry schema rides on every call, exactly today's
set. With it ON, a kernel chat run is offered the chat core (Exposure.core_for, ≤20 with
find_tools) and every other chat-exposed tool is deferred: `find_tools` searches name,
description, tags and hints, and what it returns is OFFERED from the next step on — the §4.3
cache rule (found tools are appended, never inserted) rides on the run's own trace, so a
resumed run keeps what it found.

The measure the flag exists for: the schema tokens (chars/4) a scripted kernel chat run sends,
flag on vs off, printed and asserted ≥60% lower. Old loops are untouched — only the kernel's
ToolPolicy (kernel/scoped_tools.ScopedTools) and RegistryToolSet read the flag.
"""
from __future__ import annotations

import json

import pytest

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.scoped_tools import FLAG, ScopedTools, enabled
from boss_engine.kernel.scripted import Call, Say, ScriptedEngine
from boss_engine.tools.builtin import FIND_TOOLS, build_discuss_read_registry, search_tools

ASK = "What tools do you have for deploys?"


@pytest.fixture(scope="module")
def registry():
    return build_discuss_read_registry()


class MeasuringEngine(ScriptedEngine):
    """ScriptedEngine that also records the byte size of the tool schemas each call carried."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema_chars: list = []

    def complete(self, req, **kwargs):
        self.schema_chars.append(
            len(json.dumps([t.as_openai() for t in req.tools], default=str)))
        return super().complete(req, **kwargs)


def chat_run(registry, script, run_id=None):
    engine = MeasuringEngine(list(script))
    toolset = kernel.RegistryToolSet(registry, None, project_id="p", actor="operator",
                                     surface="chat")
    result = kernel.run(kernel.RunSpec(
        engine=engine, tools=toolset, policies=presets.engine_agent(6), project_id="p",
        system="You are BOSS.", messages=[{"role": "user", "content": ASK}], run_id=run_id,
        sleep=lambda s: None))
    return result, engine


class TestTheFlag:
    def test_off_is_todays_full_set(self, registry, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        assert not enabled()
        _result, engine = chat_run(registry, [Say("hello")])
        offered = engine.requests[0]["tools"]
        assert len(offered) == len(registry.specs())
        assert FIND_TOOLS in offered

    def test_on_is_the_core_plus_find_tools(self, registry, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        _result, engine = chat_run(registry, [Say("hello")])
        offered = engine.requests[0]["tools"]
        assert len(offered) <= 20, offered
        assert FIND_TOOLS in offered
        assert "project_status" in offered and "diagnose_run" in offered
        assert "kg_search" not in offered, "a deferred tool is not offered before it is found"

    def test_find_tools_reveals_a_tool_from_the_next_step_on(self, registry, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        script = [Call(FIND_TOOLS, {"query": "search the knowledge graph"}), Say("found it")]
        result, engine = chat_run(registry, script)
        assert result.status == "completed"
        first, second = engine.requests[0]["tools"], engine.requests[1]["tools"]
        assert "kg_search" not in first
        assert "kg_search" in second, "what find_tools returned is offered next step"
        assert set(first) <= set(second), "found tools are APPENDED; nothing is taken away"
        found = [t for t in result.trace if t["tool"] == FIND_TOOLS][0]
        assert "kg_search" in found["result"]["names"]

    def test_search_ranks_name_hits_first_and_caps_at_five(self, registry):
        got = search_tools(registry, "deploy an app to a host", limit=9)
        assert got["ok"] and 1 <= len(got["names"]) <= 5
        assert "deploy_app" in got["names"]
        nothing = search_tools(registry, "zzzz qqqq", limit=3)
        assert nothing["names"] == [] and "No tool matches" in nothing["note"]

    def test_scoped_tools_off_equals_offer_all(self, monkeypatch):
        from boss_engine.kernel.policies import OfferAll
        from boss_engine.kernel.state import RunState
        from boss_engine.kernel.types import ToolSchema

        monkeypatch.delenv(FLAG, raising=False)
        available = [ToolSchema("a", "", {}, deferred=True), ToolSchema("b", "", {})]
        state = RunState(run_id="r", project_id="p", lineage_id="r", system=[], messages=[])
        assert ScopedTools().select(state, available) == \
            OfferAll().select(state, available)
        monkeypatch.setenv(FLAG, "1")
        assert [t.name for t in ScopedTools().select(state, available)] == ["b"]


class TestTheMeasure:
    def test_schema_tokens_fall_by_at_least_sixty_percent(self, registry, monkeypatch):
        """The number K5 exists for: a scripted kernel chat run's schema-token estimate
        (chars/4), flag on vs off — a find_tools step and the answer, the shape of a real
        turn."""
        script = [Call(FIND_TOOLS, {"query": "deploy an app"}), Say("deploys go via deploy_app")]

        monkeypatch.delenv(FLAG, raising=False)
        _r, engine_off = chat_run(registry, list(script))
        off_tokens = sum(engine_off.schema_chars) // 4

        monkeypatch.setenv(FLAG, "1")
        _r, engine_on = chat_run(registry, list(script))
        on_tokens = sum(engine_on.schema_chars) // 4

        reduction = 100 * (off_tokens - on_tokens) / max(off_tokens, 1)
        print(f"\nschema tokens for one scripted chat turn pair: "
              f"flag OFF {off_tokens}, flag ON {on_tokens}, reduction {reduction:.1f}%")
        assert on_tokens < off_tokens
        assert reduction >= 60.0, (
            f"BOSS_SCOPED_TOOLS must cut schema tokens by ≥60%; got {reduction:.1f}% "
            f"(off {off_tokens}, on {on_tokens})")
