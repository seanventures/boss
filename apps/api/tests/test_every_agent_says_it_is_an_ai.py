"""Every agent says it is an AI — shipped, custom, or written to sound like someone famous.

The defaults are roles and "thinks like" archetypes, never real people (agent-world spec §0,
§0.1.3), and whatever an operator writes the disclosure line stays. Checked here on the prompts
the agents actually get on a seeded project: the Ask dock, a panel seat and the composer, for
every shipped cast member and for a custom persona that names a famous person.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import agent_persona
from boss_engine.services.agent_world_types import AI_DISCLOSURE

PROJECT = "boss-dogfood"

#: The 14 §8 defaults plus the §0.1 cast that leads them.
DEFAULTS = ("pm", "chief_architect", "software_engineer", "adversarial_reviewer",
            "security_reviewer", "qa", "release_manager", "design_critic", "docs_writer",
            "finops_sentinel", "first_principles_researcher", "visionary_founder",
            "relentless_operator", "teacher_who_simplifies", "engineering_captain",
            "backend_engineer", "frontend_engineer", "operations_engineer", "sre")


def _row(engine, key: str) -> dict:
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT role_key, display_name, spec, agent_id, identity_version FROM roles "
            "WHERE project_id = :p AND role_key = :r"), {"p": PROJECT, "r": key}).mappings().first()
    # Archetypes ship as role YAMLs offered under From archetype; not installed by default.
    return dict(row) if row else agent_persona.shipped_role(key)


@pytest.mark.parametrize("key", DEFAULTS)
def test_every_default_agent_discloses_in_chat_work_and_panel(engine, key):
    row = _row(engine, key)
    assert row is not None, key
    callsign = row["spec"]["persona"]["callsign"]
    for mode in ("chat", "work", "panel"):
        prompt = agent_persona.render(agent_persona.compose_agent_prompt(row, mode=mode))
        assert AI_DISCLOSURE in prompt, (key, mode)
        assert f"You are {callsign}," in prompt, (key, mode)


def test_the_ask_dock_prompt_carries_the_identity_line(engine):
    """The dock path (intent_router._role_identity, with the project at hand)."""
    from boss_engine.services.intent_router import _role_identity

    with engine.begin() as c:
        for asked in ("QA Lead", "qa", "Vera", "QA / Test"):
            block = _role_identity(asked, c, PROJECT)
            assert "You are Vera, the QA Lead, an AI agent in BOSS (agt_" in block, asked
            assert AI_DISCLOSURE in block


def test_a_panel_seat_speaks_as_the_agent_behind_it(engine):
    """Seat prompts carry the persona, and colleagues are named by callsign."""
    from boss_engine.services.agent_persona import seat_voices
    from boss_engine.services.meeting_service import (
        _panel_blob, _persona_system, load_role_map, resolve_roles)

    with engine.begin() as c:
        seats = seat_voices(c, PROJECT, resolve_roles(
            ["qa", "engineer", "product_manager"], load_role_map(c, PROJECT)))
    by_key = {s["key"]: s for s in seats}
    assert by_key["qa"]["role_key"] == "qa" and by_key["qa"]["callsign"] == "Vera"
    assert by_key["engineer"]["role_key"] == "software_engineer"
    assert by_key["product_manager"]["role_key"] == "pm"
    system = _persona_system(by_key["qa"])
    assert "You are Vera, the QA Lead" in system and AI_DISCLOSURE in system
    assert "Forge (Engineer / Architect)" in system and "Juno (Product Manager)" in system
    # Round two shows the panel by name, so a reply can address Forge rather than "Engineer".
    blob = _panel_blob(seats, {"QA / Test": "Where is the failing test?"})
    assert "--- Vera (QA / Test) ---" in blob


def test_the_meeting_roles_payload_carries_the_resolved_role_key(client):
    """API payloads that carry seat titles also carry the agent behind each seat."""
    roles = client.get("/v1/meeting-roles", params={"project_id": PROJECT})
    assert roles.status_code == 200, roles.text
    body = roles.json()
    items = body.get("roles") or body.get("items")
    by_key = {r["key"]: r for r in items}
    assert by_key["qa"]["role_key"] == "qa"
    assert by_key["security"]["role_key"] == "security_reviewer"
    assert by_key["engineer"]["role_key"] == "software_engineer"


def test_a_persona_naming_a_famous_person_saves_with_a_warning_and_still_discloses(
        client, engine):
    key = f"famous_{uuid4().hex[:8]}"
    made = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                           "name": "Rocket Thinker", "role_key": key})
    assert made.status_code == 201, made.text
    saved = client.patch(f"/v1/agents/{key}", json={
        "project_id": PROJECT, "actor": "ceo",
        "persona": {"callsign": "Rocket",
                    "about_me": "I think about manufacturing the way Elon Musk talks about it."}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["warnings"] == [agent_persona.REAL_NAME_WARNING]
    prompt = agent_persona.render(agent_persona.compose_agent_prompt(_row(engine, key),
                                                                     mode="chat"))
    assert AI_DISCLOSURE in prompt
    assert prompt.index(AI_DISCLOSURE) < prompt.index("Elon Musk"), (
        "the disclosure must come before anything the operator wrote")
