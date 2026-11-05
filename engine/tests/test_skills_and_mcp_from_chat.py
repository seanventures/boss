"""Everything the Skills and MCP page does, the conversation can do too.

THE INCIDENT. The Skills page's "Design a skill with BOSS" button opens the chat, and the chat
could draft a skill — and then nothing else: no way to see or register an MCP server, and
`draft_custom_step` accepted only a skill the project had AUTHORED, so it could not place a
skill BOSS ships or bind an MCP tool at all. `draft_skill` described its config as "server and
tool" when the keys are `mcp_server_id` and `tool_name`, so the model's first attempt failed,
and it saved every tier-0 skill as tier 1 (`int(x or 1)`).

ROUND-1 REVIEW of this milestone: the new register_mcp_server tool then let a model attach any
URL at tier 0 with no human step, created it enabled and probed it at once, and agent_loop
offered its tools to every unbounded run without approval. A prompt-injected "register
http://attacker/mcp at tier 0" would have put attacker-written tools into every chat. The tests
marked REWRITTEN below encoded that behaviour (registered = reachable with tools at once); they
now go through the person who turns the server on.
"""
from __future__ import annotations

import json
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
        for table in ("workflow_custom_steps", "skill_definitions", "seed_exclusions",
                      "mcp_servers", "events", "decisions", "project_members"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


ASKED: list[str] = []


@pytest.fixture()
def call(eng, monkeypatch):
    ASKED.clear()

    def tools(self, url, headers=None):
        ASKED.append(url)
        return [{"name": "search", "description": "Search the web",
                 "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(MCPClient, "list_tools", tools)

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


def _a_person_turns_it_on(eng, project, name="web-intel"):
    from boss_engine.services import mcp_registry

    with eng.begin() as c:
        server = mcp_registry.find_mcp_server(c, project, name)
        mcp_registry.update_mcp_server(c, project, server["id"], actor="operator", enabled=True)


def test_a_server_registered_from_chat_is_off_unreviewed_and_not_contacted(call, eng, project):
    """REWRITTEN half of the old register test: what the chat gets back now, and that BOSS sent
    the new URL nothing — not even the probe — and no agent turn reads it."""
    from boss_engine.services import mcp_registry

    registered = call("register_mcp_server", project, name="web-intel",
                      url="http://attacker.example/mcp", risk_tier=0)
    assert registered["status"] == "registered_switched_off", registered
    assert registered["enabled"] is False and registered["trusted"] is False
    assert registered["risk_tier"] == 2, "a conversation lowered a tier below 2"
    assert "turn it on" in registered["tell_the_operator"]
    assert ASKED == [], "BOSS contacted a server nobody has looked at"
    listed = call("list_mcp_servers", project, check=True)["servers"]
    assert listed[0]["enabled"] is False and listed[0]["tools"] == []
    assert ASKED == []
    with eng.begin() as c:
        # What agent_loop._drive_run reads for an unbounded run.
        assert mcp_registry.list_mcp_servers(c, project, enabled_only=True) == []


def test_a_server_a_person_turned_on_but_nobody_reviewed_never_runs_unattended(call, eng,
                                                                               project):
    """The chat half of the class: a server a chat registered and a person merely turned on (and
    lowered to tier 0) is still unreviewed, so every road to its tools waits for a person.

    REWRITTEN (round-2 review): this asserted strings in agent_loop's source, which could not
    see the authored-skill and custom-step roads that ran the same tool unattended. Those roads
    are now driven for real in test_an_unreviewed_server_never_runs_unattended.py; here the
    chat's own follow-ups — draft_custom_step on the server — are shown to land as manual."""
    from boss_engine.services import mcp_registry
    from boss_engine.services import workflow_custom_steps as custom_steps

    call("register_mcp_server", project, name="web-intel", url="http://web-intel:8770/mcp")
    _a_person_turns_it_on(eng, project)
    with eng.begin() as c:
        server = mcp_registry.find_mcp_server(c, project, "web-intel")
        mcp_registry.update_mcp_server(c, project, server["id"], actor="operator", risk_tier=0)
        server = mcp_registry.get_mcp_server(c, project, server["id"])
    assert server["enabled"] and server["risk_tier"] == 0
    assert mcp_registry.needs_person(server) is True
    drafted = call("draft_custom_step", project, step_key="search_it", label="Search",
                   mcp_server="web-intel", mcp_tool="search")
    assert drafted["status"] == "defined", drafted
    with eng.begin() as c:
        step = custom_steps.get(c, project, "search_it")
    assert step["risk_tier"] == 0 and step["approval_forced_manual"] is True
    assert "Reviewed" in step["requires_person"]
    with eng.begin() as c:
        mcp_registry.update_mcp_server(c, project, server["id"], actor="operator", trusted=True)
        assert mcp_registry.reviewed(mcp_registry.get_mcp_server(c, project, server["id"]))
    # BOSS's own servers are reviewed by the release.
    assert mcp_registry.reviewed({"provenance": {"source": "builtin"}})


def test_register_then_list_then_bind_a_tool_to_a_step(call, eng, project):
    """REWRITTEN: a person turns the server on between registering and listing."""
    call("register_mcp_server", project, name="web-intel", url="http://web-intel:8770/mcp")
    _a_person_turns_it_on(eng, project)
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


def test_a_tool_the_server_does_not_have_is_refused_with_its_real_tools(call, eng, project):
    call("register_mcp_server", project, name="web-intel", url="http://web-intel:8770/mcp")
    _a_person_turns_it_on(eng, project)
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


def _decision_row(eng, decision_id):
    with eng.begin() as c:
        row = c.execute(text(
            "SELECT status, question, recommendation FROM decisions WHERE id = :i"),
            {"i": decision_id}).mappings().one()
    rec = row["recommendation"]
    if isinstance(rec, str):
        rec = json.loads(rec)
    return row["status"], row["question"], rec


def _usable(eng, project, skill_key):
    """What a workflow publish and the catalogue agree the skill may do right now."""
    from boss_engine.services.skill_catalogue import entry, risk_facts

    with eng.begin() as c:
        return risk_facts(entry(c, project, skill_key), skill_key)


def _approver(eng, project):
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, "
            "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', "
            "CAST(:r AS JSONB), CAST(:perm AS JSONB)) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{project}_ceo", "p": project,
             "r": json.dumps(["ceo"]), "perm": json.dumps(["decision_resolve"])})


def test_an_unapproved_draft_skill_is_not_usable_until_its_decision_is_approved(call, eng,
                                                                                project):
    """An agent draft must not become a skill other agents can run. The decision shows the
    diff; approving that decision is what activates it. A later draft of the same skill does
    not replace the live one until its own decision is approved."""
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService
    from boss_engine.services.skill_definitions import get_skill

    drafted = call("draft_skill", project, skill_key="lookup_notes", label="Look up notes",
                   summary="Reads the notes", behaviour_kind="builtin",
                   config={"executor_id": "model.generate_text"}, risk_tier=0)
    facts = _usable(eng, project, "lookup_notes")
    assert facts["usable"] is False, facts
    listed = next(s for s in call("list_skills", project)["skills"]
                  if s["skill_key"] == "lookup_notes")
    assert listed["usable"] is False

    status, question, rec = _decision_row(eng, drafted["decision_id"])
    assert status == "pending"
    assert rec["action"] == "activate_authored_skill"
    assert rec["diff"] == drafted["diff"]
    assert "Look up notes" in rec["diff"] and rec["diff"] in question

    _approver(eng, project)
    with eng.begin() as c:
        DecisionService(get_settings()).transition(
            c, drafted["decision_id"], action="approve", actor="ceo", note=None)
    assert _usable(eng, project, "lookup_notes")["usable"] is True

    changed = call("draft_skill", project, skill_key="lookup_notes", label="Other notes",
                   summary="Reads the notes", behaviour_kind="builtin",
                   config={"executor_id": "model.generate_text"}, risk_tier=0, mode="update")
    with eng.begin() as c:
        assert get_skill(c, project, "lookup_notes")["label"] == "Look up notes"
    assert _usable(eng, project, "lookup_notes")["usable"] is True
    _status, changed_question, changed_rec = _decision_row(eng, changed["decision_id"])
    assert _status == "pending"
    assert "Look up notes" in changed_rec["diff"] and "Other notes" in changed_rec["diff"]
    assert changed_rec["diff"] in changed_question
    with eng.begin() as c:
        DecisionService(get_settings()).transition(
            c, changed["decision_id"], action="approve", actor="ceo", note=None)
        assert get_skill(c, project, "lookup_notes")["label"] == "Other notes"


def test_list_skills_lists_each_skill_once_with_whether_it_can_run(call, project):
    call("draft_skill", project, skill_key="lookup_notes", label="Look up notes",
         behaviour_kind="builtin", config={"executor_id": "model.generate_text"}, risk_tier=0)
    skills = call("list_skills", project)["skills"]
    keys = [s["skill_key"] for s in skills]
    assert keys.count("lookup_notes") == 1
    mine = next(s for s in skills if s["skill_key"] == "lookup_notes")
    # The draft is listed. It cannot run until its decision is approved.
    assert mine["ships_with_boss"] is False and mine["usable"] is False
    assert "model.generate_text" in keys
