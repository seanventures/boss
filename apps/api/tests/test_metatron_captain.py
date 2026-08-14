"""Metatron Captain HTTP — persistent session, goals, watch, proposal-only drive."""


def test_captain_session_remembers_and_sets_goal(client):
    remember = client.post(
        "/v1/metatron/session",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "message": "remember sandbox root must stay on the artifacts volume",
        },
    )
    assert remember.status_code == 200, remember.text
    body = remember.json()
    assert body["intent"] == "remember"
    assert body["thread_id"].startswith("athr_")
    assert any("artifacts volume" in (m.get("content") or "") for m in body["memories"])

    goal = client.post(
        "/v1/metatron/session",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "thread_id": body["thread_id"],
            "message": "set goal All three canaries serve backends kind=working_software",
        },
    )
    assert goal.status_code == 200, goal.text
    assert goal.json()["intent"] == "set_goal"
    titles = [g["title"] for g in goal.json()["goals"]]
    assert any("canaries" in t.lower() for t in titles)


def test_captain_watch_and_drive_are_proposal_only(client):
    watch = client.post(
        "/v1/metatron/watch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert watch.status_code == 200, watch.text
    tick = watch.json()
    assert tick.get("id", "").startswith("mwt_")
    assert isinstance(tick.get("findings"), list)
    assert tick.get("receipt_id")

    drive = client.post(
        "/v1/metatron/drive",
        json={"project_id": "boss-dogfood", "actor": "ceo", "auto": True},
    )
    assert drive.status_code == 200, drive.text
    body = drive.json()
    assert body["status"] in {"proposed", "applied"}
    assert "validated" in (body.get("note") or "").lower()
    for move in body.get("moves") or []:
        if move.get("kind") in {"escalate_decision", "propose_deadletter_review"}:
            assert move.get("status") != "applied"
        if move.get("status") == "applied":
            assert (move.get("validation") or {}).get("ok") is True


def test_captain_session_requires_project(client):
    missing = client.post("/v1/metatron/session", json={"actor": "ceo", "message": "watch"})
    assert missing.status_code == 422


def test_captain_overwatch_validates_and_verifies(client):
    cycle = client.post(
        "/v1/metatron/overwatch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert cycle.status_code == 200, cycle.text
    body = cycle.json()
    assert body.get("status") in {"verified", "needs_attention", "applied", "applied_unverified", "proposed"}
    assert "verify" in body
    assert isinstance(body.get("applied"), list)
    assert (body.get("validation") or {}).get("ok") is True
    for move in body.get("applied") or []:
        assert (move.get("validation") or {}).get("ok") is True
    for move in (body.get("drive") or {}).get("moves") or []:
        if move.get("kind") in {"escalate_decision", "propose_deadletter_review"}:
            assert move.get("status") != "applied"
