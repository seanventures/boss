"""BOSS's drafting agent can write every step the builder can draw.

THE INCIDENT. `draft_workflow` could only emit a built-in capability or a skill-bound custom
step. Asked for "an agent step", it refused 'agent_work' as "not a capability"; asked for an
MCP-bound step, it resolved the step key as a skill and refused it as "attaches skill <key>,
which this project does not have"; it could not stop for a person or wait; and it wrote the
start form as `{name: "string"}`, so a drafted workflow showed bare boxes named after variables
and never said which values a run cannot start without. A workflow the agent drafted could
therefore never leave the choice of agent to BOSS — the one thing R4 asks for.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_capabilities as caps
from boss_engine.services.workflows import validate_workflow_spec
from boss_engine.tools.builtin import _spec_from_steps


def _one(step, **args):
    spec = _spec_from_steps([step], args)
    assert spec.get("ok") is not False, spec
    return spec, spec["steps"][0]


def test_an_agent_step_with_its_scope_and_no_agent_named():
    spec, step = _one({"id": "edit", "agent": True, "skills": ["video.trim"],
                       "mcp": ["wiki"], "produces": ["title"]})
    assert step["kind"] == "capability" and "captain" not in step
    binding = step["binding"]
    assert binding["agent_work"] is True
    assert binding["inputs"]["instructions"] == {"from_input": "instructions"}
    assert (binding["skills"], binding["mcp"], binding["produces"]) == (
        ["video.trim"], ["wiki"], ["title"])
    # The instruction is a paragraph the operator types at the start.
    schema = spec["input_schema"]
    assert schema["required"] == ["instructions"]
    assert schema["properties"]["instructions"]["format"] == "textarea"
    validate_workflow_spec(spec)


def test_the_word_agent_work_is_an_agent_step_not_an_unknown_capability():
    _, step = _one({"capability": "agent_work", "instructions": "tidy the backlog",
                    "captain": "pm"})
    assert step["binding"]["agent_work"] is True
    assert step["binding"]["inputs"]["instructions"] == "tidy the backlog"
    assert step["captain"] == "pm"


def test_a_gate_stops_for_a_person_with_its_question():
    spec, step = _one({"id": "check", "gate": {"question": "Ship it?",
                                               "guidance": "Watch it end to end."}})
    assert step["kind"] == "decision"
    assert (step["question"], step["guidance"], step["captain"]) == (
        "Ship it?", "Watch it end to end.", "human")
    validate_workflow_spec(spec)


def test_a_wait_pauses():
    spec, step = _one({"id": "cool_off", "wait_seconds": 600})
    assert (step["kind"], step["wait_seconds"]) == ("timer", 600)
    validate_workflow_spec(spec)


def test_another_workflow_is_named_by_key_and_resolved_later():
    _, step = _one({"id": "child", "run_workflow": "weekly_newsletter"})
    assert step["kind"] == "subworkflow"
    assert step["_workflow_key"] == "weekly_newsletter"


def test_inputs_carry_the_label_and_hint_the_start_form_shows():
    spec, _ = _one({"agent": True}, inputs=["instructions"],
                   input_details={"instructions": {"title": "What to do",
                                                   "description": "In plain words."}})
    prop = spec["input_schema"]["properties"]["instructions"]
    assert (prop["title"], prop["description"]) == ("What to do", "In plain words.")


def test_an_input_marked_optional_is_offered_not_demanded():
    spec, _ = _one({"agent": True}, input_details={"instructions": {"required": False}})
    assert spec["input_schema"]["required"] == []
    assert "instructions" in spec["input_schema"]["properties"]


def test_every_block_the_builder_can_place_can_be_drafted():
    """ENUMERATED from the palette, so a block added to the builder without a drafting shape
    fails here rather than in a conversation where the agent keeps refusing it."""
    shapes = {"timer": {"wait_seconds": 60},
              "decision": {"gate": {"question": "q"}},
              "subworkflow": {"run_workflow": "x"}}
    placeable = {b["kind"] for b in caps.CONTROL_BLOCKS if not b["canvas_required"]}
    assert placeable <= set(shapes), f"no drafting shape for: {placeable - set(shapes)}"
    for kind in placeable:
        _, step = _one(shapes[kind])
        assert step["kind"] == kind


# ── With a database: the MCP step and the agent check ────────────────────────────────────────

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            if c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar() is None:
                pytest.skip("boss_test has no workspace")
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'drafting', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_custom_steps", "workflow_mappings", "workflow_versions",
                      "workflow_definitions", "mcp_servers", "events", "role_skills", "roles"):
            with c.begin_nested():
                c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


class _Ctx:
    def __init__(self, engine):
        self._engine = engine

    def begin(self):
        return self._engine.begin()


def _draft(eng, project, **args):
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["draft_workflow"]
    return tool.handler(_Ctx(eng), project_id=project, actor="ceo", args=args)


def _agent(c, project_id, role_key, *skills):
    c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                   "VALUES (:i, :p, :r, :r)"),
              {"i": f"role_{uuid.uuid4().hex[:8]}", "p": project_id, "r": role_key})
    for skill in skills:
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                       "VALUES (:i, :p, :r, :s, 'builtin')"),
                  {"i": f"rs_{uuid.uuid4().hex[:8]}", "p": project_id, "r": role_key,
                   "s": skill})


def test_an_mcp_tool_step_is_drafted_as_a_custom_step_and_published(eng, project):
    with eng.begin() as c:
        c.execute(text("INSERT INTO mcp_servers (id, project_id, name, url, risk_tier, enabled, "
                       "provenance, tool_risk_overrides) VALUES ('mcp_w1', :p, 'wiki', "
                       "'http://127.0.0.1:1/', 0, true, '{\"trusted\": true}', '{}')"),
                  {"p": project})
    result = _draft(eng, project, workflow_key="wiki_lookup", name="Wiki lookup",
                    steps=[{"id": "look", "mcp_tool": {"server": "wiki", "tool": "search"}}])
    assert result.get("status") == "published", result
    with eng.begin() as c:
        row = c.execute(text("SELECT binding_kind, mcp_tool_name FROM workflow_custom_steps "
                             "WHERE project_id=:p"), {"p": project}).mappings().one()
    assert (row["binding_kind"], row["mcp_tool_name"]) == ("mcp_tool", "search")


def test_an_mcp_server_that_is_not_registered_is_named_in_the_refusal(eng, project):
    result = _draft(eng, project, workflow_key="no_server", name="x",
                    steps=[{"mcp_tool": {"server": "ghost", "tool": "search"}}])
    assert result["ok"] is False
    assert "MCP server 'ghost'" in result["error"] and "Skills and MCP" in result["error"]


def test_an_agent_step_left_to_boss_is_published_when_an_agent_holds_its_skills(eng, project):
    with eng.begin() as c:
        _agent(c, project, "editor", "drive.list_videos")
    result = _draft(eng, project, workflow_key="agentic", name="Agentic",
                    steps=[{"id": "edit", "agent": True, "skills": ["drive.list_videos"]}])
    assert result.get("status") == "published", result


def test_an_agent_the_project_does_not_have_is_refused_before_anything_is_written(eng, project):
    with eng.begin() as c:
        _agent(c, project, "editor", "drive.list_videos")
    result = _draft(eng, project, workflow_key="ghost_agent", name="x",
                    steps=[{"id": "edit", "agent": True, "captain": "nobody_here"}])
    assert result["ok"] is False
    assert "which this project does not have" in result["error"]
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM workflow_definitions WHERE project_id=:p"),
                         {"p": project}).scalar() == 0
