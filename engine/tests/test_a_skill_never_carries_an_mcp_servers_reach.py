"""An agent reaches exactly the MCP servers it holds — a skill that calls one does not carry it.

THE INCIDENT (independent review of milestone/integration, blocker). `assign_mcp_to_agent` was
made `grants_authority` — offered to the operator, never run by the chat model — because an MCP
grant hands an agent the server's credentials and every action it exposes. But `draft_skill`
(behaviour `mcp_tool`) and `assign_skill_to_agent` were both `safe`, so the model ran them
itself, and this branch's new `skill.mcp_tool` executor then really called the tool. Nothing on
that road asked whether the agent held the server: not the chat tool, not
`assign_skill_to_role`, not the runtime's permission gate, not the executor, not the step
auto-pick. POST /v1/role-skills was not held for a non-human caller either, while POST
/v1/role-mcp-servers was. "One MCP ownership model" was not true.

THE RULE, in one place (agents.mcp_reach_gap): a caller may run an `mcp_tool` skill only while
it holds that skill's server (and, when its grant lists tools, that tool). Every road asks it.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agents
from boss_engine.services import mcp_registry
from boss_engine.services import skill_definitions as sd
from boss_engine.services import step_agents
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services.mcp_client import MCPClient
from boss_engine.services.skill_executors import (
    SkillExecutorConfigurationError,
    SkillExecutorContext,
    execute_mcp_tool,
)
from boss_engine.services.skill_runtime import SkillRuntimeService
from boss_engine.tools.builtin import build_default_registry

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
TOOL = "post_message"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM role_mcp_servers LIMIT 1"))
            c.execute(text("SELECT 1 FROM skill_definitions LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'mcp reach', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE column_name='project_id' "
            "AND table_schema = current_schema() AND table_name <> 'projects'"))]
        for _ in range(4):
            for table in tables:
                try:
                    with c.begin_nested():
                        c.execute(text(f'DELETE FROM "{table}" WHERE project_id=:p'),
                                  {"p": pid})
                except Exception:  # noqa: BLE001 — still referenced; the next pass gets it
                    pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


@pytest.fixture()
def calls(monkeypatch):
    made: list[tuple[str, dict]] = []
    monkeypatch.setattr(MCPClient, "list_tools", lambda self, url, headers=None: [
        {"name": TOOL, "inputSchema": {"type": "object"}}, {"name": "delete_channel"}])
    monkeypatch.setattr(MCPClient, "call_tool",
                        lambda self, url, tool, args, headers=None: made.append((tool, args)) or {"ok": 1})
    return made


def _ctx(eng):
    class Ctx:
        settings = Settings()

        @contextmanager
        def begin(self):
            with eng.begin() as c:
                yield c

    return Ctx()


def _world(c, project) -> str:
    """A REVIEWED tier-0 server (so nothing but the grant can stop the call), an mcp_tool skill
    on it, and an agent `publisher` that holds nothing yet."""
    server = mcp_registry.register_mcp_server(
        c, project, "mattermost", "http://mm:9/mcp", risk_tier=0, actor="op", enabled=True)
    mcp_registry.update_mcp_server(c, project, server["id"], actor="op", trusted=True)
    mcp_registry.record_reachability(c, project, server["id"], reachable=True,
                                     tools=[{"name": TOOL}, {"name": "delete_channel"}])
    sd.save_skill(c, project_id=project, actor="op", skill_key="announce", label="Announce",
                  behaviour_kind="mcp_tool", risk_tier=0,
                  config={"mcp_server_id": server["id"], "tool_name": TOOL})
    agents.create_agent(c, project, actor="op", name="Publisher", role_key="publisher")
    return str(server["id"])


def _held_skill(c, project, role="publisher") -> bool:
    return c.execute(text("SELECT 1 FROM role_skills WHERE project_id=:p AND role_key=:r "
                          "AND skill_id='announce'"), {"p": project, "r": role}).first() is not None


def _held_server(c, project, role="publisher") -> bool:
    return c.execute(text("SELECT 1 FROM role_mcp_servers WHERE project_id=:p AND role_key=:r"),
                     {"p": project, "r": role}).first() is not None


def _invoke(c, project, caller="publisher") -> dict:
    return SkillRuntimeService(Settings()).invoke(
        c, project, "announce", caller, {"text": "hello"}, mock=False,
        executor_context=SkillExecutorContext(settings=Settings(), connection=c,
                                              project_id=project, actor=caller))


def test_the_chat_cannot_give_an_agent_a_server_by_giving_it_a_skill(eng, project, calls):
    """The blocker's chat road: the model presses assign_skill_to_agent for an mcp_tool skill.
    Nothing is written; the answer is the server grant to OFFER, carrying the skill with it."""
    with eng.begin() as c:
        sid = _world(c, project)
    registry = build_default_registry()
    answer = registry.invoke("assign_skill_to_agent", _ctx(eng), project_id=project,
                             actor="ceo", args={"agent": "publisher", "skill": "announce"})
    assert answer["ok"] is False and answer["status"] == "confirmation_required", answer
    assert answer["proposed_tool"] == "assign_mcp_to_agent"
    assert answer["proposed_args"] == {"agent": "publisher", "server": sid, "tools": [TOOL],
                                       "skill": "announce"}
    assert "mattermost" in answer["message"] and "Nothing was assigned" in answer["message"]
    with eng.begin() as c:
        assert not _held_skill(c, project) and not _held_server(c, project)

    # The proposed tool is itself offered, never pressed by the model ...
    pressed = registry.invoke("assign_mcp_to_agent", _ctx(eng), project_id=project,
                              actor="ceo", args=answer["proposed_args"])
    assert pressed["status"] == "confirmation_required", pressed
    with eng.begin() as c:
        assert not _held_skill(c, project) and not _held_server(c, project)

    # ... and the operator's one click gives both, after which the skill runs.
    clicked = registry.invoke("assign_mcp_to_agent", _ctx(eng), project_id=project,
                              actor="ceo", args=answer["proposed_args"],
                              operator_confirmed=True)
    assert clicked["assigned"] is True and clicked["skill"]["assigned"] is True, clicked
    with eng.begin() as c:
        assert _held_skill(c, project) and _held_server(c, project)
        assert _invoke(c, project)["status"] == "succeeded"
    assert [tool for tool, _ in calls] == [TOOL]


def test_once_the_agent_holds_the_server_the_chat_gives_the_skill_itself(eng, project, calls):
    """Keep what worked: a skill grant that adds no reach is still `safe` and runs at once."""
    with eng.begin() as c:
        sid = _world(c, project)
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid)
    answer = build_default_registry().invoke(
        "assign_skill_to_agent", _ctx(eng), project_id=project, actor="ceo",
        args={"agent": "publisher", "skill": "announce"})
    assert answer.get("assigned") is True, answer
    assert "warning" not in answer


def test_an_agent_holding_the_skill_but_not_the_server_is_refused_when_it_runs(eng, project,
                                                                              calls):
    """The API road (POST /v1/role-skills, the console) can still record the skill grant; it
    says the server is missing, and the run is refused with what to do — the server untouched."""
    with eng.begin() as c:
        _world(c, project)
        given = sd.assign_skill_to_role(c, project_id=project, actor="op",
                                        role_key="publisher", skill_id="announce")
        assert given["assigned"] is True
        assert given["needs_mcp_server"]["server"] == "mattermost"
        assert "Agents page" in given["warning"]
        result = _invoke(c, project)
    assert result["status"] == "denied", result
    assert "does not hold that server" in result["reason"] and "Agents page" in result["reason"]
    assert calls == []


def test_a_grant_limited_to_other_tools_does_not_reach_this_one(eng, project, calls):
    with eng.begin() as c:
        sid = _world(c, project)
        sd.assign_skill_to_role(c, project_id=project, actor="op", role_key="publisher",
                                skill_id="announce")
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid,
                         tools=["delete_channel"])
        result = _invoke(c, project)
        assert result["status"] == "denied" and f"'{TOOL}'" in result["reason"], result
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid,
                         tools=["delete_channel", TOOL])
        assert _invoke(c, project)["status"] == "succeeded"
    assert [tool for tool, _ in calls] == [TOOL]


def test_the_executor_refuses_a_caller_without_the_server_whatever_road_reached_it(
        eng, project, calls):
    """The backstop where the effect happens: no grant, or no caller at all, is a sentence."""
    with eng.begin() as c:
        _world(c, project)
        for caller in ("publisher", None):
            context = SkillExecutorContext(settings=Settings(), connection=c,
                                           project_id=project, actor="op", caller=caller)
            with pytest.raises(SkillExecutorConfigurationError) as refused:
                execute_mcp_tool(context, {"_skill_key": "announce", "text": "x"})
            assert "Agents page" in str(refused.value)
    assert calls == []


def test_a_step_is_not_handed_to_a_holder_that_lacks_the_server(eng, project, calls):
    """R4's auto-pick asks the same rule, so it never picks an agent the gate then denies, and
    the refusal names the agent and the server it still needs."""
    with eng.begin() as c:
        sid = _world(c, project)
        sd.assign_skill_to_role(c, project_id=project, actor="op", role_key="publisher",
                                skill_id="announce")
        custom_steps.create(c, project_id=project, label="Announce it", actor="op",
                            step_key="announce_it", binding_kind="skill", skill_id="announce")
        step = {"id": "s1", "kind": "capability", "binding": {"custom_step": "announce_it"},
                "approval": {"mode": "auto"}}
        with pytest.raises(step_agents.StepAgentError) as refused:
            step_agents.resolve(c, Settings(), project, step, actor="ceo")
        assert "publisher" in str(refused.value) and "mattermost" in str(refused.value)
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid)
        ran_as = step_agents.resolve(c, Settings(), project, step, actor="ceo")
        assert ran_as["agent"] == "publisher", ran_as
