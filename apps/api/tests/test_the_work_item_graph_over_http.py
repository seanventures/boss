"""The work-item graph and its typed questions over HTTP (BOSSAPP-830/831/832;
routers/work_items.py, the decision card's form).

* A person builds a tree (create, parent, labels), relates items (blocked_by), sees the
  blocked-by badge, and completing the blocker wakes the blocked item.
* Completing an item that carries an execution policy answers in_review with the stage decision.
* A form decision's card carries its form; Send answer records the typed answer, and an answer
  that does not fit is refused (409) with nothing recorded.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

HOME = "boss-dogfood"


def _create(client, title: str, **extra) -> dict:
    r = client.post("/v1/work-items", json={"project_id": HOME, "actor": "ceo", "title": title,
                                            **extra})
    assert r.status_code == 201, r.text
    return r.json()["item"]


def test_a_person_builds_the_tree_relates_items_and_a_blocker_finishing_wakes_its_item(client):
    tag = f"t{uuid4().hex[:6]}"
    epic = _create(client, "Checkout", labels=[tag])
    child = _create(client, "Card form", parent_id=epic["id"], labels=[tag],
                    assignee="agent:frontend")
    blocker = _create(client, "Payments API", labels=[tag])
    r = client.post(f"/v1/work-items/{child['id']}/relations",
                    json={"project_id": HOME, "actor": "ceo", "kind": "blocked_by",
                          "other_id": blocker["id"]})
    assert r.status_code == 200, r.text
    tree = client.get("/v1/work-items/tree", params={"project_id": HOME, "label": tag}).json()
    roots = {n["id"]: n for n in tree["items"]}
    assert set(roots) == {epic["id"], blocker["id"]}
    kid = roots[epic["id"]]["children"][0]
    assert kid["status"] == "blocked" and kid["blocked_by"][0]["id"] == blocker["id"]
    assert kid["human_key"] and kid["assignee"] == "agent:frontend"
    done = client.post(f"/v1/work-items/{blocker['id']}/complete",
                       json={"project_id": HOME, "actor": "ceo"})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done" and done.json()["woke"][0]["id"] == child["id"]
    got = client.get(f"/v1/work-items/{child['id']}", params={"project_id": HOME}).json()["item"]
    assert got["status"] == "todo" and got["blocked_by"] == []
    # Bad input says why.
    assert client.post(f"/v1/work-items/{epic['id']}/relations",
                       json={"project_id": HOME, "actor": "ceo", "kind": "blocks",
                             "other_id": epic["id"]}).status_code == 422
    assert client.patch(f"/v1/work-items/{epic['id']}",
                        json={"project_id": HOME, "actor": "ceo",
                              "parent_id": child["id"]}).status_code == 422
    edited = client.patch(f"/v1/work-items/{epic['id']}",
                          json={"project_id": HOME, "actor": "ceo", "title": "Checkout v2"})
    assert edited.status_code == 200 and edited.json()["item"]["title"] == "Checkout v2"
    assert client.delete(f"/v1/work-items/{child['id']}",
                         params={"project_id": HOME, "actor": "ceo"}).status_code == 200


def test_done_on_an_item_with_a_policy_goes_to_review(client):
    item = _create(client, "Board deck", execution_policy={"stages": [
        {"kind": "approval", "participants": ["person:ceo"]}]})
    r = client.post(f"/v1/work-items/{item['id']}/complete",
                    json={"project_id": HOME, "actor": "ceo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "in_review" and body["item"]["status"] == "in_review"
    card = client.get(f"/v1/decisions/{body['decision_id']}").json()
    assert card["recommendation"]["action"] == "execution_stage"
    assert [a["action"] for a in card["allowed_actions"]][:2] == ["approve", "revise"]
    approved = client.post(f"/v1/decisions/{body['decision_id']}/approve",
                           json={"actor": "ceo", "note": "good"})
    assert approved.status_code == 200, approved.text
    got = client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).json()["item"]
    assert got["status"] == "done"
    assert [s["event"] for s in got["stages"]] == ["raised", "approved", "completed"]


def test_a_form_card_shows_its_form_and_takes_a_typed_answer(client, engine):
    from boss_engine.services import typed_forms

    with engine.begin() as c:
        asked = typed_forms.ask_human(c, project_id=HOME, question="Which region?", asker="person:x",
                                      form={"fields": [{"key": "region", "type": "select",
                                                        "label": "Region",
                                                        "options": ["eu", "us"]}]})
    did = asked["decision_id"]
    card = client.get(f"/v1/decisions/{did}").json()
    assert card["form_schema"]["fields"][0]["key"] == "region"
    assert {a["action"]: a["needs"] for a in card["allowed_actions"]}["approve"] == "form"
    bad = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo",
                                                            "answer": {"region": "mars"}})
    assert bad.status_code == 409, bad.text
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                         {"d": did}).scalar() == "pending"
    ok = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo",
                                                           "answer": {"region": "eu"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["answer"] == {"region": "eu"} and ok.json()["answered_by"] == "ceo"
