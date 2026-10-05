"""A preview restarts on the same build (operator decision §11.3): Restart stops every live
preview of the run and starts a fresh one of the same build on the same substrate, through
preview_service; the same permission as Stop (task_create)."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def previews(engine, monkeypatch):
    from boss_engine.services import preview_service

    seed(engine, HOME, samples=False)
    calls = []

    def fake_stop(engine_, preview_id, settings=None):
        calls.append(("stop", preview_id))
        with engine.begin() as c:
            c.execute(text("UPDATE app_previews SET status = 'stopped' WHERE id = :i"),
                      {"i": preview_id})
        return {"id": preview_id, "status": "stopped"}

    def fake_start(engine_, settings, run_id, *, ttl_minutes=60, mode="auto", actor="operator"):
        calls.append(("start", run_id, mode, actor))
        return {"id": "prev_fresh", "status": "starting"}

    monkeypatch.setattr(preview_service, "stop_preview", fake_stop)
    monkeypatch.setattr(preview_service, "start_preview_async", fake_start)
    yield calls
    with engine.begin() as c:
        clear(c, HOME)


def _restart(client, preview_id, actor="ceo"):
    return client.post(f"/v1/infra/previews/{preview_id}/restart",
                       json={"project_id": HOME, "actor": actor})


def test_restart_stops_it_and_starts_the_same_build(client, previews):
    body = client.get("/v1/infra/previews", params={"project_id": HOME}).json()
    assert body["live"][0]["can"]["restart"] is True
    r = _restart(client, "pv_live")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "prev_fresh" and r.json()["stopped"] == ["pv_live"]
    assert previews == [("stop", "pv_live"), ("start", "run_pv_live", "docker", "ceo")]


def test_a_failed_preview_restarts_without_a_stop(client, previews):
    r = _restart(client, "pv_fail")
    assert r.status_code == 200, r.text
    assert previews == [("start", "run_pv_fail", "docker", "ceo")]


def test_restart_needs_a_build_a_project_and_the_permission(client, engine, previews):
    with engine.begin() as c:
        c.execute(text("UPDATE app_previews SET run_id = NULL WHERE id = 'pv_ended'"))
    assert _restart(client, "pv_ended").status_code == 409
    assert _restart(client, "nope").status_code == 404
    assert _restart(client, "pv_live", actor="nobody-here").status_code == 403
    assert previews == []
