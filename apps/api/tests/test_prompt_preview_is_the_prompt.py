"""`See its prompt` shows exactly what the model is given, by layer (spec §3.3, E10)."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import agent_persona
from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent


def test_the_preview_is_the_composed_prompt(client, engine):
    from boss_engine.services.agent_instructions import for_prompt

    key = make_agent(client)
    assert add(client, key, MARKER).status_code == 201
    shown = client.get(f"/v1/agents/{key}/prompt",
                       params={"project_id": PROJECT, "mode": "work", "actor": "ceo"}).json()
    with engine.begin() as c:
        row = dict(c.execute(text("SELECT role_key, display_name, spec, agent_id, "
                                  "identity_version FROM roles WHERE project_id = :p AND "
                                  "role_key = :r"), {"p": PROJECT, "r": key}).mappings().one())
        blocks = agent_persona.compose_agent_prompt(
            row, mode="work", instructions=for_prompt(c, PROJECT, key, "work"))
    assert [b["text"] for b in shown["blocks"]] == [b["text"] for b in blocks if b["text"]]
    labels = [b["label"] for b in shown["blocks"]]
    # A new agent has no remit yet, so that layer is empty and not shown.
    assert labels[:4] == ["Rules", "Identity", "Instructions", "Voice"]


def test_a_bad_mode_is_refused(client):
    key = make_agent(client)
    assert client.get(f"/v1/agents/{key}/prompt", params={
        "project_id": PROJECT, "mode": "admin", "actor": "ceo"}).status_code == 422
