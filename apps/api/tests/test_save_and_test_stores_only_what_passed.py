"""Save & test stores only what passed (skills-mcp §6.4, test 3).

A connection form that saved first and tested later left bad credentials behind, and the next
screen said "connected". The Kit tests the ENTERED values first; a failure answers 422 with the
field the provider objected to and writes nothing — no secret, no configuration, no tracker.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _counts(engine) -> dict:
    with engine.begin() as c:
        return {
            "secrets": c.execute(text("SELECT count(*) FROM secret_refs WHERE project_id=:p"),
                                 {"p": PROJECT}).scalar(),
            "kit_tracker": c.execute(text(
                "SELECT count(*) FROM work_tracker_providers WHERE project_id=:p "
                "AND configuration ? 'kit'"), {"p": PROJECT}).scalar(),
            "tests": c.execute(text("SELECT count(*) FROM kit_tests WHERE project_id=:p "
                                    "AND ability='file_tickets'"), {"p": PROJECT}).scalar(),
        }


def test_a_rejected_key_writes_nothing_and_names_the_field(client, engine, monkeypatch):
    from boss_engine.services.work_tracker_adapters import LinearWorkTrackerAdapter

    seen = []

    def refuse(self, configuration):
        seen.append(configuration.get("api_token"))
        return {"ok": False, "message": "POST https://api.linear.app/graphql failed HTTP 401",
                "status": 401}

    monkeypatch.setattr(LinearWorkTrackerAdapter, "test_connection", refuse)
    before = _counts(engine)
    r = client.post("/v1/kit/file_tickets/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "linear",
        "fields": {"api_token": "lin_bad_key_0000", "team_key": "ENG"}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "api_token"
    assert seen == ["lin_bad_key_0000"], "the ENTERED key was not the one tested"
    assert _counts(engine) == before, "a failed Save & test wrote something"


def test_a_passing_key_is_stored_by_reference_and_tested(client, engine, monkeypatch):
    from boss_engine.services.work_tracker_adapters import LinearWorkTrackerAdapter

    monkeypatch.setattr(LinearWorkTrackerAdapter, "test_connection",
                        lambda self, c: {"ok": True, "message": "Connected", "account": "Ada"})
    before = _counts(engine)
    r = client.post("/v1/kit/file_tickets/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "linear",
        "fields": {"api_token": "lin_good_key_1234", "team_key": "ENG"}})
    assert r.status_code == 200, r.text
    ability = r.json()
    assert ability["connection"]["provider"] == "linear"
    assert ability["connection"]["set_fields"]["api_token"] == "…1234"
    assert ability["last_test"]["ok"] is True
    after = _counts(engine)
    assert after["secrets"] == before["secrets"] + 1
    assert after["kit_tracker"] == 1 and after["tests"] == before["tests"] + 1
    with engine.begin() as c:
        config = c.execute(text("SELECT configuration FROM work_tracker_providers "
                                "WHERE project_id=:p AND provider_key='linear'"),
                           {"p": PROJECT}).scalar()
    assert "api_token" not in config and config["secret_ref_id"]
    assert "lin_good_key_1234" not in r.text


def test_disconnect_forgets_the_tracker(client, engine, monkeypatch):
    from boss_engine.services.work_tracker_adapters import LinearWorkTrackerAdapter

    monkeypatch.setattr(LinearWorkTrackerAdapter, "test_connection",
                        lambda self, c: {"ok": True, "message": "Connected"})
    client.post("/v1/kit/file_tickets/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "linear",
        "fields": {"api_token": "lin_good_key_1234", "team_key": "ENG"}})
    gone = client.delete("/v1/kit/file_tickets/connection",
                         params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200, gone.text
    assert gone.json()["status"] in ("setup", "needs_repo")
    assert _counts(engine)["kit_tracker"] == 0
