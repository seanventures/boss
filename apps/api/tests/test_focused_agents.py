"""Focused always-on agents HTTP — builder, wake, and validated tick."""


def test_create_and_list_focused_agent(client):
    created = client.post(
        "/v1/focused-agents",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "kind": "preview_keeper",
            "name": "Preview keeper",
            "focus": "Keep verify-green apps previewed",
            "start": True,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "preview_keeper"
    assert body["status"] == "active"
    assert body["mode"] == "always_on"
    assert "restart_preview" in body["allowed_actions"]

    listed = client.get("/v1/focused-agents", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200, listed.text
    names = [item["name"] for item in listed.json()["agents"]]
    assert "Preview keeper" in names


def test_tick_all_validates_and_does_not_apply_human_only(client):
    client.post(
        "/v1/focused-agents",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "kind": "factory_driver",
            "name": "Factory driver",
            "focus": "Ship the next canary slice",
            "start": True,
        },
    )
    ticked = client.post(
        "/v1/focused-agents/tick-all",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert ticked.status_code == 200, ticked.text
    body = ticked.json()
    assert "ran" in body
    for cycle in body.get("ran") or []:
        for move in cycle.get("applied") or []:
            assert move.get("kind") not in {"escalate_decision", "propose_deadletter_review"}
            assert (move.get("validation") or {}).get("ok") is True


def test_create_requires_project(client):
    missing = client.post("/v1/focused-agents", json={"actor": "ceo", "name": "x", "focus": "y"})
    assert missing.status_code == 422
