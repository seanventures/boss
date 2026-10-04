"""A secret is refused, named by line only, and never echoed or put in an event (E5)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent, stored

FAKE = "Zq9" + "x7Lm2Pw4Rt6Yu8Io0As3Df5Gh1Jk"  # not a real credential; built so no scanner trips

SECRETS = [
    f"GITHUB_TOKEN=ghp_{FAKE}1234567",
    f"api_key: {FAKE}",
    f"password = '{FAKE}'",
    f"Authorization: Bearer {FAKE}{FAKE}",
    "aws AKIA" + "ABCDEFGHIJKLMNOP",
    f"client_secret={FAKE}",
    f"slack xoxb-{FAKE}",
    "-----BEGIN RSA PRIVATE KEY-----",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_a_secret_is_refused_by_line_and_never_echoed(client, engine, secret):
    key = make_agent(client)
    refused = add(client, key, f"# Notes\n- Be careful.\n- {secret}\n")
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["message"] == "Looks like a secret" and detail["line"] == 3
    # No piece of the secret anywhere in the answer.
    for piece in (FAKE[:8], FAKE[-8:], "AKIAABCD", "ghp_"):
        if piece in secret:
            assert piece not in refused.text, piece
    assert stored(engine, key) == 0


def test_no_event_ever_carries_a_body(client, engine):
    key = make_agent(client)
    added = add(client, key, MARKER).json()
    client.patch(f"/v1/agents/{key}/instructions/{added['item']['id']}",
                 json={"project_id": PROJECT, "actor": "ceo", "body": MARKER + " now"})
    client.delete(f"/v1/agents/{key}/instructions/{added['item']['id']}",
                  params={"project_id": PROJECT, "actor": "ceo"})
    with engine.connect() as c:
        rows = c.execute(text("SELECT event_type, data FROM events WHERE project_id = :p AND "
                              "(event_type LIKE 'agent.instructions.%' OR event_type = "
                              "'agent.sealed') AND data::text LIKE :k"),
                         {"p": PROJECT, "k": f"%{key}%"}).all()
    kinds = {r[0] for r in rows}
    assert {"agent.instructions.added", "agent.instructions.updated",
            "agent.instructions.removed", "agent.sealed"} <= kinds
    for _, data in rows:
        assert "ZEBRA-CHECK-7" not in json.dumps(data)
