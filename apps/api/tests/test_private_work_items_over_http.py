"""Private work items over HTTP (BOSSAPP-876): another viewer gets the 404 a missing item gets —
from the item, the tree, a decision about it and a run of it — a grant lets them in, and the
break-glass read is one audited read whose notice the item's people see."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

HOME = "boss-dogfood"


def test_a_private_item_is_invisible_until_granted_and_break_glass_is_audited(client, engine,
                                                                            monkeypatch):
    from apps.api.app import main as api_main

    viewer = {"who": "ceo"}
    monkeypatch.setattr(api_main, "read_actor", lambda fallback="": viewer["who"])
    tag = f"t{uuid.uuid4().hex[:6]}"
    made = client.post("/v1/work-items", json={"project_id": HOME, "actor": "ceo", "title": "HR case",
                                               "visibility": "private", "labels": [tag]})
    assert made.status_code == 201, made.text
    item = made.json()["item"]
    assert item["visibility"] == "private" and item["access"][0]["party"] == "person:ceo"
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, recommendation) VALUES "
            "(:i, :p, 'Approve: HR case', 'pending', CAST(:r AS JSONB))"),
            {"i": did, "p": HOME, "r": json.dumps({"work_item_id": item["id"]})})

    viewer["who"] = "outsider"
    assert client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).status_code == 404
    tree = client.get("/v1/work-items/tree", params={"project_id": HOME, "label": tag}).json()
    assert tree["count"] == 0
    assert client.get(f"/v1/decisions/{did}").status_code == 404
    feed = client.get("/v1/decisions/inbox", params={"project_id": HOME}).json()
    assert did not in [d["id"] for d in feed["items"]]

    viewer["who"] = "ceo"
    granted = client.post(f"/v1/work-items/{item['id']}/access",
                          json={"project_id": HOME, "actor": "ceo", "party": "person:outsider"})
    assert granted.status_code == 200, granted.text
    viewer["who"] = "outsider"
    assert client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).status_code == 200
    viewer["who"] = "ceo"
    client.post(f"/v1/work-items/{item['id']}/access",
                json={"project_id": HOME, "actor": "ceo", "party": "person:outsider", "revoke": True})

    viewer["who"] = "auditor_x"
    assert client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).status_code == 404
    short = client.post(f"/v1/work-items/{item['id']}/break-glass",
                        json={"project_id": HOME, "actor": "ceo", "reason": "why"})
    assert short.status_code == 422
    read = client.post(f"/v1/work-items/{item['id']}/break-glass",
                       json={"project_id": HOME, "actor": "ceo", "reason": "Legal hold request 42"})
    assert read.status_code == 200, read.text
    assert read.json()["item"]["title"] == "HR case"
    assert client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).status_code == 404
    viewer["who"] = "ceo"
    notice = client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).json()["item"]
    assert notice["break_glass_reads"][0]["reason"] == "Legal hold request 42"
