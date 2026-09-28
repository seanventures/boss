"""Metatron Captain HTTP — persistent session, goals, watch, proposal-only drive."""

from apps.api.app import main as api_main
from apps.api.app.routers import metatron_captain


def test_installer_ceo_key_can_use_default_terminal_actor(client, monkeypatch):
    """The installer binds its operator key to ceo; TUI defaults to executive_console."""
    key = "fake-installer-ceo-key"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    seen = []

    class FakeCaptain:
        def session_turn(self, _connection, *, actor, **_kwargs):
            seen.append(("session", actor))
            return {"actor": actor}

        def watch_once(self, _connection, *, actor, **_kwargs):
            seen.append(("watch", actor))
            return {"actor": actor}

        def drive(self, _connection, *, actor, **_kwargs):
            seen.append(("drive", actor))
            return {"actor": actor}

        def overwatch_once(self, _connection, *, actor, **_kwargs):
            seen.append(("overwatch", actor))
            return {"actor": actor}

    monkeypatch.setattr(metatron_captain, "_captain", FakeCaptain)
    headers = {"X-Boss-Api-Key": key}
    session = client.get("/v1/auth/session", headers=headers)
    assert session.status_code == 200, session.text
    assert session.json()["principal_member_id"] == "ceo"

    for route, body in (
        ("session", {"message": "Show my work"}),
        ("watch", {}),
        ("drive", {"auto": False}),
        ("overwatch", {}),
    ):
        response = client.post(
            f"/v1/metatron/{route}",
            json={"project_id": "boss-dogfood", "actor": "executive_console", **body},
            headers=headers,
        )
        assert response.status_code == 200, (route, response.text)
    assert seen == [
        ("session", "executive_console"),
        ("watch", "executive_console"),
        ("drive", "executive_console"),
        ("overwatch", "executive_console"),
    ]


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
