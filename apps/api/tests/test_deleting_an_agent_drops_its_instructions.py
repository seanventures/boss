"""A deleted agent's instructions go; their bodies stay; a new agent under the same key starts
with none (spec §4.1, E9)."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent, stored


def test_delete_drops_rows_keeps_bodies_and_a_new_agent_starts_clean(client, engine):
    key = make_agent(client)
    sha = add(client, key, MARKER).json()["item"]["sha256"]
    gone = client.delete(f"/v1/agents/{key}", params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200, gone.text
    assert stored(engine, key) == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM agent_instruction_bodies WHERE "
                              "project_id = :p AND sha256 = :s"),
                         {"p": PROJECT, "s": sha}).scalar() == 1
    again = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                            "name": "Checklist Agent", "role_key": key})
    assert again.status_code == 201, again.text
    listed = client.get(f"/v1/agents/{key}/instructions", params={"project_id": PROJECT}).json()
    assert listed["items"] == []
