"""Metatron HTTP — the terminal's turn is an Ask BOSS turn; captain powers are commands."""

from apps.api.app import main as api_main
from boss_engine.tools import captain_tools


def test_installer_ceo_key_can_use_default_terminal_actor(client, monkeypatch):
    """The installer binds its operator key to ceo; TUI defaults to executive_console."""
    key = "fake-installer-ceo-key"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    seen = []

    def chat(_connection, *, actor, **_kwargs):
        seen.append(("session", actor))
        return {"thread_id": "athr_fake", "reply": "ok", "actions": [], "messages": []}

    class FakeCaptain:
        def watch_once(self, _connection, *, actor, **_kwargs):
            seen.append(("watch", actor))
            return {"actor": actor}

        def drive(self, _connection, *, actor, **_kwargs):
            seen.append(("drive", actor))
            return {"actor": actor}

        def overwatch_once(self, _connection, *, actor, **_kwargs):
            seen.append(("overwatch", actor))
            return {"actor": actor}

    monkeypatch.setattr(api_main.orchestration_agent, "chat", chat)
    # A typed captain command is a press of the agent's action: the captain behind it is the tool's.
    monkeypatch.setattr(captain_tools, "_captain", lambda _ctx: FakeCaptain())
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


def test_captain_session_load_is_the_ask_boss_thread(client, monkeypatch):
    """GET /v1/metatron/session opens the actor's general chat thread — the one the console's
    Ask BOSS and a Metatron turn both land in — with the captain's goals and watch beside it."""

    loaded = client.get("/v1/metatron/session",
                        params={"project_id": "boss-dogfood", "actor": "ceo"})
    assert loaded.status_code == 200, loaded.text
    body = loaded.json()
    assert body["thread_id"].startswith("athr_")
    assert isinstance(body["goals"], list) and "watch" in body
    threads = client.get("/v1/agent/threads", params={"project_id": "boss-dogfood", "actor": "ceo"})
    assert threads.status_code == 200, threads.text


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
