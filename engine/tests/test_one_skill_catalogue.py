"""A project has ONE skill catalogue, and every consumer reads it.

THE INCIDENT. A project's skills live in three stores — the spec-pack YAML BOSS ships,
`project_skills` overrides, and `skill_definitions` an operator authors — and each consumer read
a different subset. An operator authored a skill, gave it to an agent and placed it in a
workflow step, and every screen agreed it was there:

* `held_skills` read the YAML only, so the agent was shown holding it and never offered it;
* `resolve_risk` read `project_skills` only, so the step was "not registered in this project"
  and every workflow placing it was refused at publish;
* the custom-step builder's "Your skills" read `project_skills`, which on boss-1 held five BOSS
  system skills and none of the operator's;
* `SkillRuntimeService.invoke` loaded the spec from the YAML and raised SkillNotFoundError;
* and an `mcp_tool` skill had no executor at all.

So the fix is the DESIGN, not five patches: `skill_catalogue` merges the three stores once, and
the tests below hold every consumer to it — the first one by enumerating every SQL read of the
two skill tables in the shipping code, so the next consumer cannot quietly grow its own subset.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import re
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_steps
from boss_engine.services import skill_catalogue as catalogue
from boss_engine.services import skill_definitions as sd
from boss_engine.services import workflow_custom_steps as custom_steps

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

#: The only modules allowed to read the skill tables, and why each one may.
_READERS = {
    # THE catalogue: the merge every consumer reads.
    "engine/boss_engine/services/skill_catalogue.py": "the catalogue itself",
    # The spec-pack half of the merge: YAML plus a project's imported SkillSpecs, which is
    # what SkillRuntimeService loads specs from. skill_catalogue calls it.
    "engine/boss_engine/services/entitlements.py": "the spec-pack half of the merge",
    # The stores' own owners, reading their own rows to write them.
    "engine/boss_engine/services/skill_definitions.py": "owner of skill_definitions",
    "engine/boss_engine/services/skill_registry.py": "owner of project_skills",
    # "What points at this server" — a reference check before a delete, not a catalogue read.
    "engine/boss_engine/services/mcp_registry.py": "reference check before deleting a server",
}

_SHIPPING = ("engine/boss_engine", "apps/api/app", "apps/worker")
_READ = re.compile(r"\b(FROM|JOIN)\s+(project_skills|skill_definitions)\b", re.IGNORECASE)


def _skill_table_reads() -> list[tuple[str, int, str]]:
    found = []
    for top in _SHIPPING:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for match in _READ.finditer(" ".join(node.value.split())):
                        found.append((rel, node.lineno, match.group(0)))
    return found


def test_every_read_of_the_skill_tables_is_the_catalogue_or_a_stores_owner():
    """Enumerates every SQL read of project_skills/skill_definitions in shipping code.

    A new consumer that reads one table directly is how this went wrong five times. It must go
    through skill_catalogue instead — or, if it really is a new store owner, be added to
    _READERS here with the reason, in front of a reviewer.
    """
    reads = _skill_table_reads()
    assert reads, "the scan found nothing — it is not looking where the code is"
    strays = [f"{rel}:{line}: {sql}" for rel, line, sql in reads if rel not in _READERS]
    assert not strays, (
        "these read a skill table directly instead of through skill_catalogue:\n  "
        + "\n  ".join(strays))


#: Every consumer named in the design, and the catalogue call it must make.
_CONSUMERS = [
    ("boss_engine.services.agent_steps", "held_skills", "from .skill_catalogue import entries"),
    ("boss_engine.services.workflow_custom_steps", "resolve_risk",
     "from .skill_catalogue import entry, risk_facts"),
    ("boss_engine.services.workflows", "_validate_bindings",
     "from .skill_catalogue import entry"),
    ("boss_engine.services.skill_definitions", "_skill_exists",
     "from .skill_catalogue import entry"),
    ("boss_engine.services.skill_definitions", "assignable_catalogue",
     "from .skill_catalogue import entries"),
    ("boss_engine.services.agent_studio", "AgentStudioService._enabled_project_skill_ids",
     "from .skill_catalogue import entries"),
    ("boss_engine.services.entitlements", "EntitlementBrokerService.list_skills",
     "from .skill_catalogue import authored_specs"),
]


@pytest.mark.parametrize("module,qualname,call", _CONSUMERS,
                         ids=[c[1] for c in _CONSUMERS])
def test_each_named_consumer_reads_the_catalogue(module, qualname, call):
    import importlib

    target = importlib.import_module(module)
    for part in qualname.split("."):
        target = getattr(target, part)
    assert call in inspect.getsource(target), f"{qualname} must read skill_catalogue"


def test_the_builder_and_the_chat_read_it_too():
    """The two consumers that live outside the services package: the custom-step builder's
    bindable list (an API route) and the agent's list_skills tool."""
    main = (ROOT / "apps/api/app/main.py").read_text(encoding="utf-8")
    route = main[main.index('def list_delivery_custom_steps('):]
    route = route[:route.index("\n@app.")]
    assert "catalogue_bindable(" in route and "FROM project_skills" not in route
    tools = (ROOT / "engine/boss_engine/tools/builtin.py").read_text(encoding="utf-8")
    block = tools[tools.index("def list_skills(ctx"):]
    assert "catalogue.entries(" in block[:block.index("@reg.tool")]
    # And the chat's early check of a drafted workflow, which made up a tier-1 stand-in for
    # every shipped skill.
    from boss_engine.tools import builtin

    assert "catalogue.entry(" in inspect.getsource(builtin._draft_problems)
    assert '"risk_tier": 1, "enabled": True' not in inspect.getsource(builtin._draft_problems)


# --------------------------------------------------------------------------- #
# Behaviour, against a real database                                            #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT reachable FROM mcp_servers LIMIT 1"))
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
                       "VALUES (:i, :w, 'catalogue test', :s)"),
                  {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_custom_steps", "role_skills", "skill_definitions",
                      "project_skills", "skill_invocations", "receipts", "mcp_servers",
                      "policy_evaluations", "budget_reservations", "events"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this install lacks
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _settings() -> Settings:
    return Settings()


def _author(c, pid, key, *, behaviour="builtin", config=None, risk_tier=1, **extra):
    return sd.save_skill(
        c, project_id=pid, actor="test", skill_key=key, label=key.replace("_", " "),
        behaviour_kind=behaviour, config=config or {"executor_id": "model.generate_text"},
        risk_tier=risk_tier, **extra)


def _grant(c, pid, role, skill_id, source="authored"):
    c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                   "VALUES (:i, :p, :r, :s, :src)"),
              {"i": f"rs_{uuid.uuid4().hex[:10]}", "p": pid, "r": role, "s": skill_id,
               "src": source})


def _server(c, pid, name="tracker", *, risk_tier=1, enabled=True, trusted=True, tools=None):
    sid = f"mcp_{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO mcp_servers (id, project_id, name, url, risk_tier, enabled, "
                   "provenance, tools, reachable) VALUES (:i, :p, :n, 'http://tracker:9/mcp', "
                   ":r, :e, CAST(:pr AS JSONB), CAST(:t AS JSONB), :reach)"),
              {"i": sid, "p": pid, "n": name, "r": risk_tier, "e": enabled,
               "pr": json.dumps({"trusted": trusted}), "t": json.dumps(tools or []),
               "reach": True if tools else None})
    return sid


def test_an_authored_skill_is_in_the_catalogue_exactly_once(eng, project):
    """Authored and shipped skills in one list, no duplicates — list_skills merged them by hand
    once, and listed every authored skill twice when the spec pack started carrying them."""
    with eng.begin() as c:
        _author(c, project, "summarise_notes", risk_tier=0)
        items = catalogue.entries(c, project, settings=_settings())
        from boss_engine.services.entitlements import EntitlementBrokerService

        ids = [s["id"] for s in EntitlementBrokerService(_settings()).list_skills(
            project, connection=c)]
    assert items["summarise_notes"]["source"] == "authored"
    assert ids.count("summarise_notes") == 1
    # A shipped skill with no project_skills row is in it too — the ones agents actually hold.
    assert items["model.generate_text"]["source"] == "builtin"


def test_tier_zero_is_saved_as_zero(eng, project):
    """`int(risk_tier or 1)` saved every tier-0 skill as tier 1 in three places."""
    with eng.begin() as c:
        saved = _author(c, project, "read_only_lookup", risk_tier=0)
    assert saved["risk_tier"] == 0


def test_wrapping_a_dangerous_skill_does_not_launder_its_tier(eng, project):
    """An authored `builtin` wrapper is never below what it wraps — otherwise wrapping a tier-3
    publish as a tier-0 skill would be the documented way past every approval."""
    with eng.begin() as c:
        _author(c, project, "post_the_video", config={"executor_id": "publish_youtube"},
                risk_tier=0)
        item = catalogue.entry(c, project, "post_the_video", settings=_settings())
    assert item["risk_tier"] == 3


def test_an_agent_given_an_authored_skill_is_offered_it(eng, project):
    """held_skills dropped authored skills as 'not in the catalogue'."""
    with eng.begin() as c:
        _author(c, project, "summarise_notes")
        _grant(c, project, "pm", "summarise_notes")
        held = agent_steps.held_skills(c, _settings(), project, "pm")
    assert [s["id"] for s in held] == ["summarise_notes"]
    assert held[0]["caller"] == "pm"


def test_a_turned_off_skill_is_not_offered_and_a_step_on_it_says_so(eng, project):
    with eng.begin() as c:
        _author(c, project, "summarise_notes")
        _grant(c, project, "pm", "summarise_notes")
        sd.set_skill_enabled(c, project_id=project, actor="test", skill_key="summarise_notes",
                             enabled=False)
        held = agent_steps.held_skills(c, _settings(), project, "pm")
        risk = custom_steps.resolve_risk(
            c, project, {"binding_kind": "skill", "skill_id": "summarise_notes"})
    assert held == []
    assert risk["usable"] is False and "Turn it on" in risk["reason"]


def test_a_step_bound_to_an_authored_skill_is_usable(eng, project):
    """resolve_risk read project_skills only: 'skill X is not registered in this project'."""
    with eng.begin() as c:
        _author(c, project, "summarise_notes", risk_tier=0, trusted=True)
        step = custom_steps.create(c, project_id=project, label="Summarise",
                                   binding_kind="skill", skill_id="summarise_notes",
                                   actor="test")
        risk = custom_steps.resolve_risk(c, project, {"binding_kind": "skill",
                                                      "skill_id": "summarise_notes"})
    assert risk["usable"] is True, risk
    assert risk["risk_tier"] == 0
    assert step["step_key"] == "summarise"


def test_a_shipped_skill_without_a_project_row_is_bindable(eng, project):
    """The builder offered project_skills rows only — none of the skills agents hold."""
    with eng.begin() as c:
        offered = {s["skill_id"]: s for s in catalogue.bindable(c, project,
                                                                 settings=_settings())}
    assert offered["model.generate_text"]["usable"] is True
    assert offered["publish_youtube"]["requires_approval"] is True


def test_a_skill_that_cannot_run_says_why(eng, project):
    """An mcp_tool skill whose server was turned off is listed, marked, and not offered."""
    with eng.begin() as c:
        sid = _server(c, project, enabled=False)
        _author(c, project, "file_ticket", behaviour="mcp_tool",
                config={"mcp_server_id": sid, "tool_name": "create_issue"})
        _grant(c, project, "pm", "file_ticket")
        item = catalogue.entry(c, project, "file_ticket", settings=_settings())
        held = agent_steps.held_skills(c, _settings(), project, "pm")
    assert item["usable"] is False and "turned off" not in (item["reason"] or "")
    assert "disabled" in item["reason"] and "Enable it" in item["reason"]
    assert held == []


def test_an_mcp_tool_skill_is_never_below_its_servers_tier_or_its_review(eng, project):
    with eng.begin() as c:
        sid = _server(c, project, risk_tier=2, trusted=False)
        _author(c, project, "file_ticket", behaviour="mcp_tool", risk_tier=0, trusted=True,
                config={"mcp_server_id": sid, "tool_name": "create_issue"})
        item = catalogue.entry(c, project, "file_ticket", settings=_settings())
    assert item["risk_tier"] == 2
    # A reviewed skill on an unreviewed server is an unreviewed integration.
    assert item["trusted"] is False


# --------------------------------------------------------------------------- #
# The editor's store: create vs update, on/off, delete with references          #
# --------------------------------------------------------------------------- #

def test_create_refuses_a_taken_key_and_update_refuses_a_missing_one(eng, project):
    """Saving silently replaced whatever skill already had the key."""
    with eng.begin() as c:
        _author(c, project, "summarise_notes", mode="create")
        with pytest.raises(sd.SkillExistsError) as taken:
            _author(c, project, "summarise_notes", mode="create")
        with pytest.raises(sd.SkillDefinitionError) as missing:
            _author(c, project, "never_written", mode="update")
        updated = _author(c, project, "summarise_notes", mode="update", risk_tier=0)
    assert "Open it with Edit" in str(taken.value)
    assert "Save it as a new skill" in str(missing.value)
    assert updated["risk_tier"] == 0


def test_a_mistyped_timeout_is_a_sentence_not_a_500(eng, project):
    with eng.begin() as c, pytest.raises(sd.SkillDefinitionError) as error:
        sd.save_skill(c, project_id=project, actor="test", skill_key="run_tool",
                      label="Run", behaviour_kind="sandboxed_command",
                      config={"image": "x@sha256:" + "a" * 64, "command": "echo",
                              "timeout_seconds": "five minutes"})
    assert "whole number" in str(error.value)


def test_delete_refuses_while_a_step_runs_the_skill(eng, project):
    with eng.begin() as c:
        _author(c, project, "summarise_notes")
        custom_steps.create(c, project_id=project, label="Summarise", binding_kind="skill",
                            skill_id="summarise_notes", actor="test")
        with pytest.raises(sd.SkillInUseError) as error:
            sd.delete_skill(c, project_id=project, actor="test", skill_key="summarise_notes",
                            release_grants=True)
    assert "Summarise" in str(error.value) and "turn the skill off" in str(error.value)


def test_delete_takes_a_skill_off_its_agents_only_when_asked(eng, project):
    """Deleting used to leave the grant behind: 'no such skill here any more', forever."""
    with eng.begin() as c:
        _author(c, project, "summarise_notes")
        _grant(c, project, "pm", "summarise_notes")
        refs = sd.list_skills(c, project, with_references=True)[0]["references"]
        with pytest.raises(sd.SkillInUseError) as held:
            sd.delete_skill(c, project_id=project, actor="test", skill_key="summarise_notes")
        done = sd.delete_skill(c, project_id=project, actor="test",
                               skill_key="summarise_notes", release_grants=True)
        left = c.execute(text("SELECT count(*) FROM role_skills WHERE project_id=:p"),
                         {"p": project}).scalar()
    assert refs["agents"] == ["pm"]
    assert "held by pm" in str(held.value)
    assert done["released_from"] == ["pm"] and left == 0


def test_an_mcp_tool_skill_names_a_server_that_exists(eng, project):
    """The form asked for a free-text server id no screen showed; a typo saved fine."""
    with eng.begin() as c:
        sid = _server(c, project, "Tracker", tools=[{"name": "create_issue"}])
        with pytest.raises(sd.SkillDefinitionError) as unknown:
            _author(c, project, "file_ticket", behaviour="mcp_tool",
                    config={"mcp_server_id": "trackr", "tool_name": "create_issue"})
        with pytest.raises(sd.SkillDefinitionError) as no_tool:
            _author(c, project, "file_ticket", behaviour="mcp_tool",
                    config={"mcp_server_id": sid, "tool_name": "delete_everything"})
        by_name = _author(c, project, "file_ticket", behaviour="mcp_tool",
                          config={"mcp_server_id": "tracker", "tool_name": "create_issue"})
    assert "Registered servers: Tracker" in str(unknown.value)
    assert "Its tools are: create_issue" in str(no_tool.value)
    # Stored by id, because a name can be edited.
    assert by_name["config"]["mcp_server_id"] == sid


# --------------------------------------------------------------------------- #
# The mcp_tool executor                                                         #
# --------------------------------------------------------------------------- #

def test_an_agent_holding_an_mcp_tool_skill_can_run_it(eng, project, monkeypatch):
    """There was no `skill.mcp_tool` executor: the documented way to give an agent an MCP
    integration failed on its first run with 'No server-side executor is registered'."""
    from boss_engine.services.mcp_client import MCPClient
    from boss_engine.services.skill_executors import SkillExecutorContext, resolve_executor
    from boss_engine.services.skill_runtime import SkillRuntimeService

    calls = []
    monkeypatch.setattr(MCPClient, "call_tool",
                        lambda self, url, tool, args: calls.append((url, tool, args))
                        or {"issue": 42})
    with eng.begin() as c:
        sid = _server(c, project, tools=[{"name": "create_issue"}])
        _author(c, project, "file_ticket", behaviour="mcp_tool", risk_tier=0,
                config={"mcp_server_id": sid, "tool_name": "create_issue"})
        _grant(c, project, "pm", "file_ticket")
        context = SkillExecutorContext(settings=_settings(), connection=c, project_id=project,
                                       actor="pm")
        assert resolve_executor("file_ticket", context=context) is not None
        result = SkillRuntimeService(_settings()).invoke(
            c, project, "file_ticket", "pm", {"title": "Broken build"}, mock=False,
            executor_context=context, actor="pm")
        receipts = c.execute(text("SELECT status, effect_kind FROM receipts "
                                  "WHERE project_id=:p"), {"p": project}).mappings().all()
    assert result["status"] == "succeeded", result
    assert calls == [("http://tracker:9/mcp", "create_issue", {"title": "Broken build"})]
    assert result["result"]["issue"] == 42
    # Same governance as an agent's direct MCP call: an effect receipt, settled.
    assert [dict(r) for r in receipts] == [{"status": "succeeded",
                                            "effect_kind": "mcp.tool.call"}]


def test_the_executor_refuses_a_server_turned_off_since_publishing(eng, project, monkeypatch):
    from boss_engine.services.skill_executors import (
        SkillExecutorConfigurationError, SkillExecutorContext, execute_mcp_tool)

    with eng.begin() as c:
        sid = _server(c, project, tools=[{"name": "create_issue"}])
        _author(c, project, "file_ticket", behaviour="mcp_tool",
                config={"mcp_server_id": sid, "tool_name": "create_issue"})
        c.execute(text("UPDATE mcp_servers SET enabled=false WHERE id=:i"), {"i": sid})
        context = SkillExecutorContext(settings=_settings(), connection=c, project_id=project,
                                       actor="pm")
        with pytest.raises(SkillExecutorConfigurationError) as error:
            execute_mcp_tool(context, {"_skill_key": "file_ticket"})
    assert "turned off" in str(error.value) and "Skills and MCP" in str(error.value)
