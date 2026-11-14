"""When a step names no agent, BOSS asks a model which one — and now it really asks.

THE INCIDENT. `choose_agent` called `adapter.complete(...)`. No chat adapter has a `complete`
method, the AttributeError was swallowed, and so the model was never once consulted: every pick
was the word-overlap fallback, and the run card told the operator "no model was available to
judge properly" on installs whose model was working. The existing tests accepted any `how`, so
they passed throughout. These pin `how == "model"` when the model answers — the pick is a turn
through the one agent pipeline (BOSSAPP-759) — and an honest reason when it does not.

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
    from boss_engine.services import claude_cli_adapter, model_routing  # noqa: F401

    adapter = _Answers(reply)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: adapter)
    return adapter


def _picks(role: str, why: str = "") -> str:
    """The one pipeline's decision contract (agent_decision): the pick is an action."""
    return json.dumps({"action": role, "confidence": 0.9, "reason": why, "question": ""})


def test_the_model_is_asked_and_its_pick_is_used(monkeypatch, pool):
    adapter = _adapter(monkeypatch, _picks("publisher", "it holds the upload"))
    chosen = agent_steps.choose_agent(None, Settings(), "p", instructions="Upload the cut")
    assert adapter.asked, "the model was never asked"
    assert chosen == {"role_key": "publisher", "why": "it holds the upload", "how": "model"}


def test_a_model_that_fails_is_reported_as_failing_not_as_absent(monkeypatch, pool):
    _adapter(monkeypatch, RuntimeError("usage limit reached"))
    with pytest.raises(agent_steps.AgentStepError) as failed:
        agent_steps.choose_agent(None, Settings(), "p", instructions="trim and reframe the video")
    assert "usage limit reached" in str(failed.value)
    assert "no model was available" not in str(failed.value)


def test_a_model_naming_somebody_outside_the_pool_is_not_obeyed(monkeypatch, pool):
    _adapter(monkeypatch, _picks("cto", "seniority"))
    with pytest.raises(agent_steps.AgentStepError, match="'cto'"):
        agent_steps.choose_agent(None, Settings(), "p", instructions="post the notes")


def test_no_rule_breaks_a_tie_or_picks_without_a_model(monkeypatch, pool):
    """REWRITTEN FOR BOSSAPP-759. This pinned the word-overlap fallback's tie-break (the most
    specialised agent, then its key). The pick is now a turn through the one pipeline with no
    rule behind it: with no model, nobody is picked by shared words, and the step says what to
    do instead."""
    _adapter(monkeypatch, RuntimeError("down"))
    with pytest.raises(agent_steps.AgentStepError, match="Name the agent on the step"):
        agent_steps.choose_agent(None, Settings(), "p", instructions="nothing matches")


def test_the_code_never_calls_a_method_adapters_do_not_have():
    import inspect

    # Code only: the comment explaining the old call names it. The pick asks the one pipeline
    # (AgentService.decide), whose decision calls the adapter's create_text_response.
    from boss_engine.services import agent_decision

    source = "\n".join(line for fn in (agent_steps.choose_agent, agent_decision._ask_model)
                       for line in inspect.getsource(fn).splitlines()
                       if not line.strip().startswith("#"))
    assert ".complete(" not in source
    assert "create_text_response(" in source
    assert ".decide(" in inspect.getsource(agent_steps.choose_agent)


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

    # The assembly is the agent step's toolset (K4), shared by both loops.
    source = inspect.getsource(agent_loop.AgentStepToolSet)
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
