"""When a step names no agent, BOSS asks a model which one — and now it really asks.

THE INCIDENT. `choose_agent` called `adapter.complete(...)`. No chat adapter has a `complete`
method, the AttributeError was swallowed, and so the model was never once consulted: every pick
was the word-overlap fallback, and the run card told the operator "no model was available to
judge properly" on installs whose model was working. The existing tests accepted any `how`, so
they passed throughout. These pin `how == "model"` when the model answers, and an honest reason
when it does not.

Also here, without a database: a bounded agent run is offered exactly the MCP servers its agent
holds, pinned at step start.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.config import Settings
from boss_engine.services import agent_loop, agent_steps

POOL = [
    {"role_key": "video_editor", "name": "Video Editor", "mission": "edits video",
     "holds": ["video.trim", "video.reframe"]},
    {"role_key": "publisher", "name": "Publisher", "mission": "publishes",
     "holds": ["youtube.upload", "mcp:mattermost"]},
]


class _Answers:
    """A chat adapter with ONLY the interface every real adapter has."""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.asked: list[str] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.asked.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return ("ref", self.reply, {})


@pytest.fixture()
def pool(monkeypatch):
    monkeypatch.setattr(agent_steps, "candidates", lambda *_a, **_k: [dict(c) for c in POOL])
    from boss_engine.services import setup_service

    monkeypatch.setattr(setup_service, "chat_engine_for", lambda *_a, **_k: "")


def _adapter(monkeypatch, reply):
    from boss_engine.services import claude_cli_adapter

    adapter = _Answers(reply)
    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter", lambda _s: adapter)
    return adapter


def test_the_model_is_asked_and_its_pick_is_used(monkeypatch, pool):
    adapter = _adapter(monkeypatch, json.dumps(
        {"role_key": "publisher", "why": "it holds the upload"}))
    chosen = agent_steps.choose_agent(None, Settings(), "p", instructions="Upload the cut")
    assert adapter.asked, "the model was never asked"
    assert chosen == {"role_key": "publisher", "why": "it holds the upload", "how": "model"}


def test_a_model_that_fails_is_reported_as_failing_not_as_absent(monkeypatch, pool):
    _adapter(monkeypatch, RuntimeError("usage limit reached"))
    chosen = agent_steps.choose_agent(None, Settings(), "p",
                                      instructions="trim and reframe the video")
    assert chosen["how"] == "word_overlap"
    assert chosen["role_key"] == "video_editor"
    assert "usage limit reached" in chosen["why"]
    assert "no model was available" not in chosen["why"]


def test_a_model_naming_somebody_outside_the_pool_is_not_obeyed(monkeypatch, pool):
    _adapter(monkeypatch, json.dumps({"role_key": "cto", "why": "seniority"}))
    chosen = agent_steps.choose_agent(None, Settings(), "p", instructions="post the notes")
    assert chosen["how"] == "word_overlap"
    assert "'cto'" in chosen["why"]


def test_a_tie_goes_to_the_most_specialised_agent_then_to_its_key(monkeypatch, pool):
    """REWRITTEN AT INTEGRATION. The agents stream broke a tie toward the agent holding MORE; the
    steps stream, which owns who runs a step (R4), toward the one holding FEWEST — the most
    specialised, the least authority handed to work nobody's words matched — and
    test_who_runs_a_step pins that. One rule is kept (the steps stream's); what this test
    protects is unchanged: a tie is broken deterministically, the same pool in any order gives
    the same agent, and the key decides between equals."""
    _adapter(monkeypatch, RuntimeError("down"))
    tie = [{"role_key": "b_agent", "name": "", "mission": "", "holds": ["x.one"]},
           {"role_key": "a_agent", "name": "", "mission": "", "holds": ["x.one"]},
           {"role_key": "c_agent", "name": "", "mission": "", "holds": ["x.one", "x.two"]}]
    monkeypatch.setattr(agent_steps, "candidates", lambda *_a, **_k: tie)
    assert agent_steps.choose_agent(None, Settings(), "p",
                                    instructions="nothing matches")["role_key"] == "a_agent"
    monkeypatch.setattr(agent_steps, "candidates", lambda *_a, **_k: list(reversed(tie)))
    assert agent_steps.choose_agent(None, Settings(), "p",
                                    instructions="nothing matches")["role_key"] == "a_agent"
    monkeypatch.setattr(agent_steps, "candidates", lambda *_a, **_k: [tie[2], tie[0]])
    assert agent_steps.choose_agent(None, Settings(), "p",
                                    instructions="nothing matches")["role_key"] == "b_agent"


def test_the_code_never_calls_a_method_adapters_do_not_have():
    import inspect

    # Code only: the comment explaining the old call names it.
    # choose_agent asks through `_ask_the_model` since the steps stream; both are read.
    source = "\n".join(line for fn in (agent_steps.choose_agent, agent_steps._ask_the_model)
                       for line in inspect.getsource(fn).splitlines()
                       if not line.strip().startswith("#"))
    assert ".complete(" not in source
    assert "create_text_response(" in source


def test_a_bounded_run_gets_the_mcp_servers_its_agent_holds_pinned_at_start(monkeypatch):
    from boss_engine.services import agents

    held = [{"id": "mcp_a", "name": "mattermost", "url": "http://a", "granted_tools": ["post"]},
            {"id": "mcp_b", "name": "github", "url": "http://b", "granted_tools": []}]
    monkeypatch.setattr(agents, "held_mcp_servers", lambda *_a, **_k: [dict(s) for s in held])

    pinned = agent_loop._granted_mcp_servers(
        None, "p", {"role_key": "publisher", "mcp_server_ids": ["mcp_a"]})
    assert [s["id"] for s in pinned] == ["mcp_a"], "a server granted mid-run was picked up"
    unpinned = agent_loop._granted_mcp_servers(None, "p", {"role_key": "publisher"})
    assert {s["id"] for s in unpinned} == {"mcp_a", "mcp_b"}
    assert agent_loop._granted_mcp_servers(None, "p", {}) == []


def test_a_bounded_run_offers_only_the_granted_tools_of_a_server():
    import inspect

    source = inspect.getsource(agent_loop._drive_run)
    assert 'allow = set(server.get("granted_tools") or [])' in source
    assert "if allow and tool_name not in allow:" in source


def test_the_chat_can_give_an_agent_a_skill_or_an_mcp_server():
    from boss_engine.tools import build_default_registry

    reg = build_default_registry()
    for name in ("assign_skill_to_agent", "assign_mcp_to_agent"):
        spec = reg.get(name)
        assert spec.kind == "write"
        assert "remove" in spec.parameters["properties"]
    # REWRITTEN: this asserted both were `safe`. A skill grant is; an MCP grant hands an agent a
    # server's credentials and every action it exposes, so the chat OFFERS it and a person's
    # click runs it (review of milestone/agents, round 1).
    assert reg.get("assign_skill_to_agent").self_executable
    assert reg.get("assign_mcp_to_agent").reversibility == "grants_authority"
    assert not reg.get("assign_mcp_to_agent").self_executable
