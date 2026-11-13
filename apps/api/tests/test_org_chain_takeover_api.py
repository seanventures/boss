"""BOSSAPP-885 over HTTP: the org chart payload carries each placed role's chain, one agent's
chain reads on its own, and a manager takes a report's work over — or is refused in words."""
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.db import begin


def test_the_chain_reads_and_a_manager_takes_over_while_an_outsider_is_refused(client, engine):
    from boss_engine.services import work_items

    project_id = get_settings().default_project_id
    for agent, parent in (("captain_885", "person:usr_lead"), ("qa_885", "captain_885")):
        placed = client.put(f"/v1/console/v2/org/{agent}",
                            json={"project_id": project_id, "reports_to": parent, "actor": "ceo"})
        assert placed.status_code == 200, placed.text

    chart = client.get("/v1/console/v2/org", params={"project_id": project_id}).json()
    assert [r["party"] for r in chart["chains"]["qa_885"]["chain"]] == [
        "agent:qa_885", "agent:captain_885", "person:usr_lead"]
    assert chart["takeover_offers"] == [] or all(
        o["subject"] != "agent:qa_885" for o in chart["takeover_offers"])

    one = client.get("/v1/console/v2/org/chain",
                     params={"project_id": project_id, "agent_id": "qa_885"})
    assert one.status_code == 200, one.text
    assert one.json()["acting_manager"]["id"] == "captain_885"
    assert one.json()["blocked"] is None

    with begin(engine) as c:
        item = work_items.create_item(c, project_id=project_id, title=f"Contract fix {uuid4().hex[:6]}",
                                      assignee="agent:qa_885", actor="person:ceo")

    refused = client.post("/v1/console/v2/org/takeover", json={
        "project_id": project_id, "taker": "agent:designer_885", "work_item_id": item["id"],
        "actor": "ceo"})
    assert refused.status_code == 409, refused.text
    assert "cannot take this work over" in refused.json()["detail"]

    taken = client.post("/v1/console/v2/org/takeover", json={
        "project_id": project_id, "taker": "agent:captain_885", "work_item_id": item["id"],
        "note": "Use API v2.", "actor": "ceo"})
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert body["authority"]["path"] == ["agent:qa_885", "agent:captain_885"]
    assert body["authority"]["taken_by"] == "agent:captain_885"
    with begin(engine) as c:
        assignee = c.execute(text("SELECT assignee FROM work_items WHERE id = :w"),
                             {"w": item["id"]}).scalar()
        events = c.execute(text("SELECT count(*) FROM events WHERE event_type = 'org.takeover' "
                                "AND entity_id = :w"), {"w": item["id"]}).scalar()
        for agent in ("qa_885", "captain_885"):
            c.execute(text("DELETE FROM org_assignments WHERE project_id = :p AND agent_id = :a"),
                      {"p": project_id, "a": agent})
    assert assignee == "agent:captain_885" and events == 1

    missing = client.post("/v1/console/v2/org/takeover",
                          json={"project_id": project_id, "taker": "agent:x", "actor": "ceo"})
    assert missing.status_code == 400
