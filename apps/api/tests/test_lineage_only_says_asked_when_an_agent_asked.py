"""`Asked by <agent>` appears only when an agent truly asked (Agent World §2.3, §12.4 test 5).

Every other way work moves between agents is BOSS (named, picked), a captain, or a person, and
is labelled so. The one proof an agent initiated something is an A2A envelope it sent with
sender_kind=agent. No envelope, no such edge anywhere; one envelope, exactly one.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _asked_by_agent(engine) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT act_id, role_key, handed_by_id, artifacts FROM agent_acts "
            "WHERE project_id = :p AND how = 'asked_by_agent'"), {"p": PROJECT}).mappings()]


def _channel(client) -> str:
    made = client.post("/v1/a2a/channels", json={
        "project_id": PROJECT, "actor": "ceo", "channel_key": "aw_handoffs",
        "name": "Agent handoffs", "allowed_message_kinds": ["instruction", "status"],
        "allowed_schema_versions": {"boss.a2a": [1]}})
    assert made.status_code == 200, made.text
    return made.json()["id"]


def _send(client, channel_id: str, *, sender_kind: str, sender_id: str, recipient: dict,
          key: str):
    sent = client.post("/v1/a2a/messages", json={
        "project_id": PROJECT, "actor": "ceo", "channel_id": channel_id,
        "message_kind": "instruction", "schema_name": "boss.a2a", "schema_version": 1,
        "sender_kind": sender_kind, "sender_id": sender_id, "recipient": recipient,
        "payload": {"objective": "Threat-model the login change."}, "dedupe_key": key})
    assert sent.status_code == 200, sent.text
    return sent.json()


def test_no_envelope_no_asked_by_agent_and_one_envelope_exactly_one(client, engine):
    channel = _channel(client)

    # Messages that are not an agent asking an agent sign nothing as "asked by an agent":
    # an operator's instruction, and an agent addressing a person.
    _send(client, channel, sender_kind="operator", sender_id="ceo",
          recipient={"kind": "agent", "id": "security_reviewer"}, key="aw:op:1")
    _send(client, channel, sender_kind="agent", sender_id="qa",
          recipient={"kind": "operator", "id": "console"}, key="aw:to-person:1")
    assert _asked_by_agent(engine) == []

    envelope = _send(client, channel, sender_kind="agent", sender_id="qa",
                     recipient={"kind": "agent", "id": "security_reviewer"}, key="aw:ask:1")
    asked = _asked_by_agent(engine)
    assert len(asked) == 1
    assert (asked[0]["role_key"], asked[0]["handed_by_id"]) == ("security_reviewer", "qa")
    assert {"kind": "a2a", "ref": envelope["id"]} in asked[0]["artifacts"]

    # The same envelope again is a duplicate, not a second ask.
    _send(client, channel, sender_kind="agent", sender_id="qa",
          recipient={"kind": "agent", "id": "security_reviewer"}, key="aw:ask:1")
    assert len(_asked_by_agent(engine)) == 1

    graph = client.get("/v1/agents/security_reviewer/lineage",
                       params={"project_id": PROJECT}).json()
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    edge = edges[("agent:qa", "agent:security_reviewer")]
    assert (edge["how_top"], edge["label"], edge["reconstructed"]) == (
        "asked_by_agent", "Asked by qa", False)

    feed = client.get("/v1/agents/security_reviewer/acts",
                      params={"project_id": PROJECT, "kind": "handoff"}).json()["items"]
    assert [(a["edge_label"], a["from"]["role_key"]) for a in feed] == [("Asked by qa", "qa")]
    # Across the whole project, that is the only one.
    assert sum(1 for a in _asked_by_agent(engine)) == 1
