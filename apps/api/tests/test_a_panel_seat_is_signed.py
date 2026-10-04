"""A panel's seats are signed work, and presence follows them (Agent World §5.1 hook 5, §4.5).

A panel opens: each seat — named by its title in the meeting engine — is one act for the agent
that title resolves to, and the Roster says "In a panel" for exactly those agents. It ends: the
seats close, and each agent is idle with the panel as its last work. The line is built from the
act, never from a model.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _live(client) -> dict[str, dict]:
    got = client.get("/v1/agents/live", params={"project_id": PROJECT})
    assert got.status_code == 200, got.text
    return {i["role_key"]: i for i in got.json()["items"]}


def test_a_panel_seats_its_agents_and_presence_follows(client, engine):
    from boss_engine.services import agent_acts, meeting_service

    with engine.begin() as c:
        run_id = meeting_service.start_meeting_run(
            c, project_id=PROJECT, task_id=None, directive="Should we ship dark mode?",
            url="", constraints={}, role_keys=["product_manager", "engineer"],
            finalize_ctx={"actor": "ceo"}, title="Dark mode")
        seated = sorted(c.execute(text(
            "SELECT role_key FROM agent_acts WHERE meeting_run_id = :m AND kind = 'seat'"),
            {"m": run_id}).scalars())
    assert seated == ["pm", "software_engineer"]

    live = _live(client)
    assert {k: v["state"] for k, v in live.items()} == {
        "pm": "in_panel", "software_engineer": "in_panel"}
    assert live["pm"]["line"] == "In a panel: Panel · Dark mode"

    roster = client.get("/v1/console/v2/workforce", params={"project_id": PROJECT}).json()
    faces = {a["id"]: a.get("presence") for a in roster["agents"] + roster["roster"]}
    assert faces["pm"]["state"] == "in_panel"

    feed = client.get("/v1/agents/pm/acts", params={"project_id": PROJECT}).json()["items"]
    assert [(a["kind"], a["edge_label"], a["from"]["kind"]) for a in feed] == [
        ("seat", "Seated on panel", "person")]

    with engine.begin() as c:
        c.execute(text("UPDATE meeting_runs SET status = 'completed', phase = 'done' "
                       "WHERE id = :m"), {"m": run_id})
        agent_acts.reconcile(c, PROJECT)
    assert _live(client) == {}
    with engine.begin() as c:
        presence = agent_acts.presence_map(c, PROJECT)
    assert presence["pm"]["state"] == "idle"
    assert presence["pm"]["line"].startswith("Last: Panel · Dark mode · ")
