"""Documents over HTTP (BOSSAPP-874; routers/documents.py): a person opens a document on a work
item, comments on a passage, saves from a base, and a save from a stale base answers 409 with
the revision that won — and writes nothing."""
from __future__ import annotations

from sqlalchemy import text

HOME = "boss-dogfood"


def test_a_document_is_saved_from_its_base_and_a_stale_save_is_a_409(client, engine):
    item = client.post("/v1/work-items", json={"project_id": HOME, "actor": "ceo",
                                               "title": "Checkout plan"}).json()["item"]
    made = client.post("/v1/documents", json={
        "project_id": HOME, "actor": "ceo", "title": "Plan", "kind": "plan",
        "body": "Step one.\n\nStep two.\n", "work_item_id": item["id"]})
    assert made.status_code == 201, made.text
    doc = made.json()["document"]
    r1 = doc["current_revision_id"]
    listed = client.get("/v1/documents", params={"project_id": HOME, "work_item_id": item["id"]})
    assert [d["id"] for d in listed.json()["documents"]] == [doc["id"]]

    thread = client.post(f"/v1/documents/{doc['id']}/threads", json={
        "project_id": HOME, "actor": "ceo", "revision_id": r1, "start": 11, "end": 20,
        "body": "Say who owns this."})
    assert thread.status_code == 201, thread.text
    assert thread.json()["thread"]["quote"] == "Step two."

    ok = client.post(f"/v1/documents/{doc['id']}/revisions", json={
        "project_id": HOME, "actor": "ceo", "body": "Intro.\n\nStep one.\n\nStep two.\n",
        "base_revision_id": r1})
    assert ok.status_code == 200, ok.text
    assert ok.json()["document"]["current_revision_no"] == 2
    assert ok.json()["document"]["threads"][0]["anchor_state"] == "moved"

    stale = client.post(f"/v1/documents/{doc['id']}/revisions", json={
        "project_id": HOME, "actor": "ceo", "body": "overwrite", "base_revision_id": r1})
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["current"]["revision_no"] == 2 and "Intro." in detail["current"]["body"]
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM document_revisions WHERE document_id = :d"),
                         {"d": doc["id"]}).scalar() == 2

    diff = client.get(f"/v1/documents/{doc['id']}/diff",
                      params={"project_id": HOME, "from": 1, "to": 2}).json()
    assert diff["added"] == 2 and "+Intro." in diff["lines"]
    assert client.get(f"/v1/documents/nope", params={"project_id": HOME}).status_code == 404
