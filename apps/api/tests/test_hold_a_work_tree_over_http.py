"""Holding a whole work tree over HTTP (BOSSAPP-878; routers/work_holds.py): a dry run plans,
the hold shows on the item, restore puts it back, and a second restore is a 409."""
from __future__ import annotations

HOME = "boss-dogfood"


def test_pause_all_previews_holds_and_restores(client):
    epic = client.post("/v1/work-items", json={"project_id": HOME, "actor": "ceo",
                                               "title": "Runaway epic"}).json()["item"]
    child = client.post("/v1/work-items", json={"project_id": HOME, "actor": "ceo", "title": "Story",
                                                "parent_id": epic["id"]}).json()["item"]
    body = {"project_id": HOME, "actor": "ceo", "root_kind": "work_item", "root_id": epic["id"]}
    preview = client.post("/v1/work-holds", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["hold"]["dry_run"] is True
    assert {m["member_id"] for m in preview.json()["hold"]["members"]} >= {epic["id"], child["id"]}
    listed = client.get("/v1/work-holds", params={"project_id": HOME}).json()["holds"]
    assert all(h["root_id"] != epic["id"] for h in listed)          # a dry run holds nothing

    held = client.post("/v1/work-holds", json={**body, "reason": "spend spike"})
    assert held.status_code == 200, held.text
    hold_id = held.json()["hold"]["id"]
    item = client.get(f"/v1/work-items/{child['id']}", params={"project_id": HOME}).json()["item"]
    assert item["hold"]["id"] == hold_id and item["hold"]["reason"] == "spend spike"
    assert client.post("/v1/work-holds", json=body).status_code == 409

    restored = client.post(f"/v1/work-holds/{hold_id}/restore", json={"project_id": HOME, "actor": "ceo"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["hold"]["status"] == "restored"
    again = client.post(f"/v1/work-holds/{hold_id}/restore", json={"project_id": HOME, "actor": "ceo"})
    assert again.status_code == 409
    assert client.get(f"/v1/work-holds/{hold_id}", params={"project_id": HOME}).json()["hold"]["restored_by"]
