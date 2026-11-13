"""Work UX, set 2 over HTTP (BOSSAPP-879..884; routers/work_ux.py).

* 879 — a choose-one decision raised over HTTP carries signed options; the card offers one
  Approve per option; approving with `choose` runs that option and the runs read back.
"""
from __future__ import annotations

HOME = "boss-dogfood"


def _create(client, title: str, **extra) -> dict:
    r = client.post("/v1/work-items", json={"project_id": HOME, "actor": "ceo", "title": title,
                                            **extra})
    assert r.status_code == 201, r.text
    return r.json()["item"]


def test_decision_options_are_offered_chosen_and_read_back(client):
    item = _create(client, "Flaky checkout test")
    r = client.post("/v1/decision-options", json={
        "project_id": HOME, "actor": "ceo", "question": "What do we do with the flaky test?",
        "work_item_id": item["id"],
        "options": [
            {"id": "park", "label": "Park it",
             "effects": [{"type": "update_status", "target": item["id"], "status": "backlog"}]},
            {"id": "qa", "label": "Give it to QA",
             "effects": [{"type": "assign", "target": item["id"], "assignee": "agent:qa"}]},
        ]})
    assert r.status_code == 201, r.text
    did = r.json()["decision_id"]
    card = client.get(f"/v1/decisions/{did}").json()
    offers = [(a["action"], a.get("choose")) for a in card["allowed_actions"]]
    assert offers[:3] == [("approve", "park"), ("approve", "qa"), ("reject", None)]
    # No choice: refused in words, nothing recorded.
    assert client.post(f"/v1/decisions/{did}/approve",
                       json={"actor": "ceo"}).status_code == 409
    ok = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo", "choose": "qa"})
    assert ok.status_code == 200, ok.text
    got = client.get(f"/v1/work-items/{item['id']}", params={"project_id": HOME}).json()["item"]
    assert got["assignee"] == "agent:qa" and got["status"] != "backlog"
    runs = client.get(f"/v1/decision-options/{did}/runs", params={"project_id": HOME}).json()
    assert runs["chosen_option"] == "qa"
    assert [(x["effect_type"], x["status"]) for x in runs["runs"]] == [("assign", "done")]
    # A bad effect is refused before anything is raised.
    assert client.post("/v1/decision-options", json={
        "project_id": HOME, "actor": "ceo", "question": "q",
        "options": [{"label": "x", "effects": [{"type": "explode"}]}]}).status_code == 422


def test_a_person_attaches_lists_and_detaches_a_watchdog(client):
    item = _create(client, "Release epic")
    r = client.post(f"/v1/work-items/{item['id']}/watchdogs", json={
        "project_id": HOME, "actor": "ceo", "conditions": ["blocked", "closed"],
        "instructions": "make sure the changelog shipped"})
    assert r.status_code == 201, r.text
    dog = r.json()["watchdog"]
    assert dog["watcher"].endswith("ceo") and dog["conditions"] == ["blocked", "closed"]
    listed = client.get(f"/v1/work-items/{item['id']}/watchdogs",
                        params={"project_id": HOME}).json()
    assert [w["id"] for w in listed["watchdogs"]] == [dog["id"]]
    assert "stopped_unfinished" in listed["conditions"]
    assert client.post(f"/v1/work-items/{item['id']}/watchdogs", json={
        "project_id": HOME, "actor": "ceo", "conditions": ["nope"]}).status_code == 422
    gone = client.delete(f"/v1/work-items/{item['id']}/watchdogs/{dog['id']}",
                         params={"project_id": HOME, "actor": "ceo"})
    assert gone.status_code == 200, gone.text
    assert client.get(f"/v1/work-items/{item['id']}/watchdogs",
                      params={"project_id": HOME}).json()["watchdogs"] == []


def test_one_search_finds_work_by_its_content(client):
    item = _create(client, "Ledger reconciliation", description="Match the nightly ledger export")
    r = client.get("/v1/search", params={"project_id": HOME, "q": "nightly ledger"})
    assert r.status_code == 200, r.text
    body = r.json()
    hit = next(h for h in body["items"] if h["id"] == item["id"])
    assert hit["kind"] == "work_item" and "nightly ledger" in hit["snippet"]
    assert "decision" in body["searched"] and "knowledge" in body["searched"]
    only = client.get("/v1/search", params={"project_id": HOME, "q": "nightly ledger",
                                            "kinds": "decision"}).json()
    assert only["searched"] == ["decision"] and all(h["kind"] == "decision" for h in only["items"])
