"""The Agents roster lists every agent a workflow step's agent picker offers.

The roster said "All 5" — only agents with recorded work — while the picker offered 34. Both
now read one list (step_agents.agent_keys); the roster adds only agents doing work with no
registered role, and says which those are.
"""
from __future__ import annotations

PROJECT = "boss-dogfood"


def test_the_roster_and_the_picker_show_the_same_agents(client):
    palette = client.get("/v1/delivery-workflows/capabilities", params={"project_id": PROJECT})
    assert palette.status_code == 200, palette.text
    picker = {a["role_key"] for a in palette.json()["agents"]}
    assert picker, "the test project offers no agents at all"

    feed = client.get("/v1/console/v2/workforce", params={"project_id": PROJECT})
    assert feed.status_code == 200, feed.text
    body = feed.json()
    roster = {a["id"] for a in body["roster"]}

    assert picker <= roster, sorted(picker - roster)
    # Anything else on the roster is an agent doing work that nothing registered.
    assert roster - picker <= set(body["unregistered"]), sorted(roster - picker)
    for agent in body["roster"]:
        if agent["id"] not in {a["id"] for a in body["agents"]}:
            assert agent["task"] == "No work yet", agent
