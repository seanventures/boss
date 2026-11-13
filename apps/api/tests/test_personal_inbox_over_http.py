"""The personal inbox over HTTP (BOSSAPP-875; routers/inbox.py): the signed-in person's feed,
their own read state and snoozes, and a read with nobody signed in refused."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

HOME = "boss-dogfood"


def test_my_inbox_is_ranked_and_my_read_state_is_mine(client, engine, monkeypatch):
    from apps.api.app import main as api_main

    did = f"dec_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, risk_tier, delegated_to, "
            "recommendation) VALUES (:i, :p, 'Pick a vendor?', 'delegated', 2, 'inbox_person', "
            "CAST(:r AS JSONB))"), {"i": did, "p": HOME, "r": json.dumps({"action": "x"})})
    monkeypatch.setattr(api_main, "read_actor", lambda fallback="": "inbox_person")
    feed = client.get("/v1/inbox", params={"project_id": HOME})
    assert feed.status_code == 200, feed.text
    entry = next(e for e in feed.json()["items"] if e["ref_id"] == did)
    assert entry["for_you"] and not entry["read"] and "for you" in entry["why"]
    assert feed.json()["person"] == "person:inbox_person"

    ok = client.post("/v1/inbox/read", json={"project_id": HOME, "item_keys": [f"decision:{did}"]})
    assert ok.status_code == 200, ok.text
    snoozed = client.post("/v1/inbox/snooze", json={"project_id": HOME,
                                                    "item_key": f"decision:{did}", "minutes": 30})
    assert snoozed.status_code == 200, snoozed.text
    after = client.get("/v1/inbox", params={"project_id": HOME}).json()
    assert did not in [e["ref_id"] for e in after["items"]] and after["snoozed"] >= 1
    shown = client.get("/v1/inbox", params={"project_id": HOME, "include_snoozed": True}).json()
    assert next(e for e in shown["items"] if e["ref_id"] == did)["read"] is True

    monkeypatch.setattr(api_main, "read_actor", lambda fallback="": "")
    assert client.get("/v1/inbox", params={"project_id": HOME}).status_code == 401
