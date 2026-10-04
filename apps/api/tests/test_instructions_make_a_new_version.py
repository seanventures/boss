"""Every change to an agent's instructions is a new agent version, and old versions keep the
text they carried (agent-loadout spec §4.3, E3)."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent, version_of


def _sealed(engine, key: str) -> dict:
    with engine.connect() as c:
        return c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND event_type = 'agent.sealed' "
            "AND data->>'role_key' = :r ORDER BY created_at DESC LIMIT 1"),
            {"p": PROJECT, "r": key}).scalar()


def test_add_bumps_once_resave_does_not_remove_bumps(client, engine):
    key = make_agent(client)
    v0, fp0 = version_of(engine, key)
    added = add(client, key, MARKER)
    assert added.status_code == 201, added.text
    v1, fp1 = version_of(engine, key)
    assert v1 == v0 + 1 and fp1 != fp0
    assert added.json()["version"] == v1 and added.json()["item"]["since_version"] == v1
    sealed = _sealed(engine, key)
    assert sealed["change_kind"] == "instructions"
    assert sealed["canonical"]["instructions"][0]["sha256"] == added.json()["item"]["sha256"]
    # The same body again changes nothing.
    item = added.json()["item"]["id"]
    same = client.patch(f"/v1/agents/{key}/instructions/{item}",
                        json={"project_id": PROJECT, "actor": "ceo", "body": MARKER})
    assert same.status_code == 200, same.text
    assert version_of(engine, key)[0] == v1 and same.json()["changed"] is False
    gone = client.delete(f"/v1/agents/{key}/instructions/{item}",
                         params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200, gone.text
    assert version_of(engine, key)[0] == v1 + 1


def test_an_agent_with_no_instructions_keeps_its_fingerprint(client, engine):
    """A4: the key is added only when non-empty, so the upgrade re-versions nobody: the document
    of an agent with no instructions is exactly the one the three-argument form computes."""
    from boss_engine.services import agent_identity

    key = make_agent(client)
    with engine.begin() as c:
        row = agent_identity._row(c, PROJECT, key)
        spec = row["spec"] if isinstance(row.get("spec"), dict) else {}
        before = agent_identity.canonical(
            row, agent_identity._grants(c, PROJECT, key),
            agent_identity._model_policy(c, PROJECT, key, spec))
        now = agent_identity._canonical_for(c, PROJECT, row)
    assert "instructions" not in now and now == before
    assert agent_identity.fingerprint_of(now) == version_of(engine, key)[1]
    assert "instructions" not in agent_identity.canonical({"role_key": "x"}, [], {}, [])


def test_an_old_version_still_shows_the_body_it_carried(client, engine):
    key = make_agent(client)
    first = add(client, key, MARKER).json()
    old_version = first["version"]
    edited = client.patch(f"/v1/agents/{key}/instructions/{first['item']['id']}",
                          json={"project_id": PROJECT, "actor": "ceo",
                                "body": "ZEBRA-CHECK-8: list risky migrations first"})
    assert edited.status_code == 200 and edited.json()["version"] == old_version + 1
    then = client.get(f"/v1/agents/{key}/versions/{old_version}/instructions",
                      params={"project_id": PROJECT}).json()
    assert [i["body"] for i in then["items"]] == [MARKER]
    now = client.get(f"/v1/agents/{key}/versions/{old_version + 1}/instructions",
                     params={"project_id": PROJECT}).json()
    assert "ZEBRA-CHECK-8" in now["items"][0]["body"]


def test_an_act_after_an_edit_carries_the_new_version(client, engine):
    from boss_engine.services import agent_acts

    key = make_agent(client)
    version = add(client, key, MARKER).json()["version"]
    with engine.begin() as c:
        act_id = agent_acts.open_act(c, project_id=PROJECT, kind="chat", role_key=key, how="asked_by_person",
                                       handed_by_kind="person", handed_by_id="ceo",
                                       accountable="ceo", label="A question")
        stamped = c.execute(text("SELECT identity_version FROM agent_acts WHERE act_id = :a"),
                            {"a": act_id}).scalar()
    assert stamped == version
