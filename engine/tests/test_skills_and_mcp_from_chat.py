"""Everything the Skills and MCP page does, the conversation can do too.

THE INCIDENT. The Skills page's "Design a skill with BOSS" button opens the chat, and the chat
could draft a skill — and then nothing else: no way to see or register an MCP server, and
`draft_custom_step` accepted only a skill the project had AUTHORED, so it could not place a
skill BOSS ships or bind an MCP tool at all. `draft_skill` described its config as "server and
tool" when the keys are `mcp_server_id` and `tool_name`, so the model's first attempt failed,
and it saved every tier-0 skill as tier 1 (`int(x or 1)`).
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.mcp_client import MCPClient
from boss_engine.tools.builtin import build_default_registry

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT reachable FROM mcp_servers LIMIT 1"))
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
                       "VALUES (:i, :w, 'chat test', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_custom_steps", "skill_definitions", "mcp_servers", "events"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


@pytest.fixture()
def call(eng, monkeypatch):
    monkeypatch.setattr(MCPClient, "list_tools",
                        lambda self, url, headers=None: [
                            {"name": "search", "description": "Search the web",
                             "inputSchema": {"type": "object"}}])

    class Ctx:
        settings = Settings()

        @contextmanager
        def begin(self):
            with eng.begin() as c:
                yield c

    registry = build_default_registry()

    def _call(tool, project, **args):
        return registry.get(tool).handler(Ctx(), project_id=project, actor="ceo", args=args)

    return _call


def test_register_then_list_then_bind_a_tool_to_a_step(call, project):
    registered = call("register_mcp_server", project, name="web-intel",
                      url="http://web-intel:8770/mcp")
    assert registered["status"] == "registered" and registered["tools"] == ["search"]
    listed = call("list_mcp_servers", project)["servers"]
    assert [s["name"] for s in listed] == ["web-intel"]
    # Registered from a conversation: unreviewed, and not tier 0 until a person says so.
    assert listed[0]["trusted"] is False and listed[0]["risk_tier"] == 2
    assert listed[0]["tools"][0] == {"name": "search", "description": "Search the web",
                                     "risk_tier": 2}

    step = call("draft_custom_step", project, step_key="search_the_web", label="Search",
                mcp_server="web-intel", mcp_tool="search")
    assert step["status"] == "defined", step
    assert step["mcp_tool"] == "search"


def test_a_tool_the_server_does_not_have_is_refused_with_its_real_tools(call, project):
    call("register_mcp_server", project, name="web-intel", url="http://web-intel:8770/mcp")
    out = call("draft_custom_step", project, step_key="x_step", label="X",
               mcp_server="web-intel", mcp_tool="delete_everything")
    assert out["ok"] is False and out["tools"] == ["search"]


def test_a_duplicate_registration_is_a_sentence(call, project):
    call("register_mcp_server", project, name="web-intel", url="http://web-intel:8770/mcp")
    out = call("register_mcp_server", project, name="Web-Intel", url="http://other:1/mcp")
    assert out["ok"] is False and "already registered" in out["error"]


def test_a_step_can_run_a_skill_boss_ships(call, project):
    """draft_custom_step accepted only authored skills."""
    out = call("draft_custom_step", project, step_key="write_copy", label="Write copy",
               skill="model.generate_text")
    assert out["status"] == "defined", out


def test_draft_skill_keeps_tier_zero_and_does_not_overwrite(call, project):
    first = call("draft_skill", project, skill_key="lookup_notes", label="Look up notes",
                 behaviour_kind="builtin", config={"executor_id": "model.generate_text"},
                 risk_tier=0)
    again = call("draft_skill", project, skill_key="lookup_notes", label="Other",
                 behaviour_kind="builtin", config={"executor_id": "model.generate_text"})
    assert first["risk_tier"] == 0
    assert again["ok"] is False and "already exists" in again["error"]


def test_list_skills_lists_each_skill_once_with_whether_it_can_run(call, project):
    call("draft_skill", project, skill_key="lookup_notes", label="Look up notes",
         behaviour_kind="builtin", config={"executor_id": "model.generate_text"}, risk_tier=0)
    skills = call("list_skills", project)["skills"]
    keys = [s["skill_key"] for s in skills]
    assert keys.count("lookup_notes") == 1
    mine = next(s for s in skills if s["skill_key"] == "lookup_notes")
    assert mine["ships_with_boss"] is False and mine["usable"] is True
    assert "model.generate_text" in keys
