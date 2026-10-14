"""The shipped cast holds what its role needs, so putting one on a step changes what it can do.

Agent World §0.1: the default agents are roles that really act like their role — a voice, the
skills the role needs (role_skills) and the MCP servers it should hold (role_mcp_servers). The
seed grants both from the role YAMLs; these tests read the seeded project the way the runtime
does (step_agents.project_agents, agents.held_mcp_servers), check that a step scoped to a
server is accepted for the agent that holds it and refused for one that does not, that the
catalogue names these agents on its steps, and that a server an operator takes away stays away.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

PROJECT = "boss-dogfood"
ROLES = Path(__file__).resolve().parents[3] / "packages" / "specpack" / "roles"
CAST = ("engineering_captain", "backend_engineer", "frontend_engineer", "operations_engineer",
        "sre", "chief_architect", "pm", "qa", "release_manager", "design_critic",
        "docs_writer", "finops_sentinel", "security_reviewer", "software_engineer")


@pytest.fixture(autouse=True)
def _granted(engine):
    """The suite's reset empties role_mcp_servers between tests (conftest.clean_tables), so the
    seed's MCP half is run again here — the same function the install seed runs."""
    from boss_engine.services.agents import sync_role_mcp

    with engine.begin() as c:
        sync_role_mcp(c, project_id=PROJECT, actor="test-seed")


def _yaml(key: str) -> dict:
    return yaml.safe_load((ROLES / f"{key}.yaml").read_text(encoding="utf-8"))


def _agents(engine) -> dict[str, dict]:
    from boss_engine.services import step_agents

    with engine.begin() as c:
        return {a["role_key"]: a for a in step_agents.project_agents(c, PROJECT)}


@pytest.mark.parametrize("key", CAST)
def test_each_cast_role_holds_the_mcp_servers_its_yaml_names(engine, key):
    from boss_engine.services import agents

    from boss_engine.config import get_settings
    from boss_engine.services import kit
    from boss_engine.services.mcp_registry import list_mcp_servers

    # WHAT THE ROLE NAMES: its `mcp_servers`, plus what the Kit abilities it is granted expand
    # to (skills-mcp §6.2) — for the servers this project has registered.
    permissions = _yaml(key)["permissions"]
    wanted: dict[str, set] = {g["server"]: set(g["tools"])
                              for g in permissions.get("mcp_servers") or []}
    for entry in kit.expand(get_settings(), permissions.get("abilities") or [])["mcp"]:
        wanted.setdefault(entry["server"], set()).update(entry["tools"])
    with engine.begin() as c:
        registered = {s["name"] for s in list_mcp_servers(c, PROJECT)}
        held = {s["name"]: sorted(s["granted_tools"])
                for s in agents.held_mcp_servers(c, PROJECT, key, enabled_only=False)}
    wanted_sorted = {name: sorted(tools) for name, tools in wanted.items() if name in registered}
    assert held == wanted_sorted, f"{key} holds {held}, its role says {wanted_sorted}"


@pytest.mark.parametrize("key", ("engineering_captain", "backend_engineer", "frontend_engineer",
                                 "operations_engineer", "sre", "chief_architect"))
def test_each_new_cast_role_holds_every_skill_its_role_names(engine, key):
    """Every skill its role names that this edition HAS — and on every edition enough to work
    with (the coding pipeline's skills are not in Community; the generic ones are)."""
    from boss_engine.services.skill_catalogue import entries

    with engine.begin() as c:
        held = set(c.execute(text("SELECT skill_id FROM role_skills WHERE project_id = :p "
                                  "AND role_key = :r"), {"p": PROJECT, "r": key}).scalars())
        # Assignable: a bare registered row with no spec cannot be given to an agent.
        available = {k for k, e in entries(c, PROJECT).items() if e["source"] != "registered"}
    wanted = set(_yaml(key)["permissions"]["allowed_skills"]) & available
    assert wanted <= held, f"{key} is missing {sorted(wanted - held)}"
    assert len(held) >= 2, f"{key} holds only {sorted(held)} on this edition"
    agent = _agents(engine)[key]
    assert agent["lifecycle"] == "active" and agent["callsign"]


def test_a_step_scoped_to_a_server_is_the_holders_and_refused_for_anyone_else(engine):
    """Putting Prism (frontend) on a step that renders mockups works; Strata (backend) does
    not hold web-intel, so the same step naming Strata is refused at publish."""
    from boss_engine.services import step_agents

    def step(captain: str) -> dict:
        return {"id": "look", "kind": "capability", "captain": captain,
                "binding": {"agent_work": True, "mcp": ["web-intel"], "inputs": {}}}

    with engine.begin() as c:
        ok = step_agents.assignment_problems(c, PROJECT, {"steps": [step("frontend_engineer")]})
        refused = step_agents.assignment_problems(c, PROJECT,
                                                  {"steps": [step("backend_engineer")]})
    assert ok == []
    assert refused and "backend_engineer" in refused[0] and "web-intel" in refused[0]


def test_left_to_auto_a_server_scoped_step_goes_to_an_agent_that_holds_it(engine):
    from boss_engine.services import step_agents

    agents = _agents(engine)
    holders = sorted(k for k, a in agents.items() if "doc-render" in (a.get("mcp") or []))
    assert {"docs_writer", "pm", "release_manager"} <= set(holders)
    pool = step_agents._agent_work_pool(
        {"id": "pdf", "kind": "capability",
         "binding": {"agent_work": True, "mcp": ["doc-render"], "inputs": {}}},
        list(agents.values()))
    assert sorted(a["role_key"] for a in pool) == holders


def test_the_catalogue_on_this_project_names_its_agents(engine):
    from boss_engine.services import workflow_catalogue

    with engine.begin() as c:
        workflow_catalogue.seed(c, PROJECT, actor="test-seed")
    with engine.connect() as c:
        spec = c.execute(text(
            "SELECT v.spec FROM workflow_versions v JOIN workflow_definitions d "
            "ON d.id = v.workflow_definition_id WHERE d.project_id = :p "
            "AND d.workflow_key = 'standard_product_delivery' AND v.status = 'published' "
            "ORDER BY v.created_at DESC LIMIT 1"), {"p": PROJECT}).scalar()
    captains = {s["id"]: s.get("captain") for s in spec["steps"]}
    assert captains["meeting"] == "pm" and captains["build"] == "engineering_captain"
    assert captains["preview"] == "engineering_captain" and captains["plrr"] == "sre"
    assert captains["monitor"] == "sre"
    assert captains["deploy"] is None  # the gate names Pilot as its approver instead
    deploy = next(s for s in spec["steps"] if s["id"] == "deploy")
    assert deploy["approval"]["approver_role"] == "release_manager"


def test_a_server_taken_off_an_agent_stays_off_after_a_reseed(client, engine):
    from boss_engine.services import agents

    from apps.api.tests.test_one_agent_model import _reseed

    with engine.begin() as c:
        server = next(s for s in agents.held_mcp_servers(c, PROJECT, "frontend_engineer",
                                                         enabled_only=False)
                      if s["name"] == "web-intel")
    revoked = client.delete("/v1/role-mcp-servers", params={
        "project_id": PROJECT, "role_key": "frontend_engineer",
        "mcp_server_id": server["id"], "actor": "ceo"})
    assert revoked.status_code == 200, revoked.text
    try:
        _reseed(engine)
        with engine.begin() as c:
            names = {s["name"] for s in agents.held_mcp_servers(c, PROJECT, "frontend_engineer",
                                                                enabled_only=False)}
        assert "web-intel" not in names, "the seed put back a server an operator took away"
    finally:
        restored = client.post("/v1/role-mcp-servers", json={
            "project_id": PROJECT, "role_key": "frontend_engineer", "actor": "ceo",
            "mcp_server_id": server["id"], "tools": ["fetch_page", "render_mockup",
                                                     "study_site"]})
        assert restored.status_code == 201, restored.text


def test_a_reseed_of_an_unchanged_project_seals_nothing(engine):
    from apps.api.tests.test_one_agent_model import _reseed

    def sealed() -> int:
        with engine.connect() as c:
            return c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                                  "AND event_type = 'agent.sealed'"), {"p": PROJECT}).scalar()

    _reseed(engine)
    before = sealed()
    _reseed(engine)
    assert sealed() == before, "a seed that changed nothing moved an agent's version"
