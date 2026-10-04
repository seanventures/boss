"""An agent's ask hangs where its envelope says, or nowhere (Agent World §2.3: never invent).

An agent with two runs and a panel seat open is in three pieces of work. The handoff used to be
parented to "the sender's newest open act" — a guess, which put the ask, its cost and its
accountable person on whichever run had started last. Now the parent is the act the envelope's
lineage names (only if it is in this project), or the run it names, or none.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _channel(client) -> str:
    made = client.post("/v1/a2a/channels", json={
        "project_id": PROJECT, "actor": "ceo", "channel_key": "aw_parents",
        "name": "Agent handoffs", "allowed_message_kinds": ["instruction"],
        "allowed_schema_versions": {"boss.a2a": [1]}})
    assert made.status_code == 200, made.text
    return made.json()["id"]


def _ask(client, channel_id: str, key: str, lineage: dict | None = None) -> str:
    body = {"project_id": PROJECT, "actor": "ceo", "channel_id": channel_id,
            "message_kind": "instruction", "schema_name": "boss.a2a", "schema_version": 1,
            "sender_kind": "agent", "sender_id": "qa",
            "recipient": {"kind": "agent", "id": "security_reviewer"},
            "payload": {"objective": "Threat-model the login change."}, "dedupe_key": key}
    if lineage is not None:
        body["lineage"] = lineage
    sent = client.post("/v1/a2a/messages", json=body)
    assert sent.status_code == 200, sent.text
    return sent.json()["id"]


def _handoff(engine, envelope_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text(
            "SELECT * FROM agent_acts WHERE kind = 'handoff' AND subject_id = :e"),
            {"e": envelope_id}).mappings().one())


def test_an_ask_is_parented_by_its_envelope_never_by_the_senders_newest_work(client, engine):
    from boss_engine.services import agent_acts

    with engine.begin() as c:
        # qa is busy in two pieces of work, for two different people.
        older = agent_acts.open_act(c, project_id=PROJECT, kind="step", role_key="qa",
                                    how="named", handed_by_kind="boss", accountable="ceo",
                                    label="Review · Run A")
        newest = agent_acts.open_act(c, project_id=PROJECT, kind="seat", role_key="qa",
                                     how="seated", handed_by_kind="boss", accountable="cfo",
                                     label="Panel · Pricing")
        foreign = agent_acts.open_act(c, project_id="another-project", kind="step",
                                      role_key="qa", how="named", handed_by_kind="boss",
                                      accountable="stranger", label="Review · elsewhere")
    channel = _channel(client)

    # No lineage: no parent, and the person who sent it is accountable — not cfo's panel.
    bare = _handoff(engine, _ask(client, channel, "aw:p:1"))
    assert (bare["parent_act_id"], bare["accountable"]) == (None, "ceo")

    # Lineage naming the act it was sent from: that act.
    named = _handoff(engine, _ask(client, channel, "aw:p:2", {"act_id": older}))
    assert (named["parent_act_id"], named["accountable"]) == (older, "ceo")

    # Lineage naming another project's act: not believed.
    elsewhere = _handoff(engine, _ask(client, channel, "aw:p:3", {"act_id": foreign}))
    assert (elsewhere["parent_act_id"], elsewhere["accountable"]) == (None, "ceo")
    assert newest not in {bare["parent_act_id"], named["parent_act_id"],
                          elsewhere["parent_act_id"]}
