"""/v1/golden-journeys over HTTP (BOSSAPP-1284/1285): results stored one row per journey per
day per host, a red that stays red for its day, the green-day counter the Govern card reads, and
the daily run armed as BOSS's own schedule (a schedule_work wake)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.config import get_settings
from boss_engine.services.golden_journeys import definitions, results, service


@pytest.fixture(autouse=True)
def _clean(engine, monkeypatch):
    monkeypatch.setenv("BOSS_JOURNEY_HOSTS", "laptop")
    with engine.begin() as c:
        c.execute(text("DELETE FROM golden_journey_results"))
        c.execute(text("DELETE FROM agent_wakeups"))
    yield


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


ALL = [j.id for j in definitions.load_all()]


def green_day(engine, day: date, *, host="laptop", red=()):
    with engine.begin() as c:
        for jid in ALL:
            results.record(c, {"journey_id": jid, "status": "red" if jid in red else "green",
                               "mode": "real", "title": jid}, host=host, day=day)


def test_results_post_and_today_reads_them_back(client):
    r = client.post("/v1/golden-journeys/results", json={"host": "laptop", "results": [
        {"journey_id": "J1", "status": "green", "mode": "real", "title": "J1", "verdict": "met",
         "items": [{"n": 1, "text": "done", "status": "met"}], "seconds": 12.5},
        {"journey_id": "J5", "status": "red", "mode": "real", "why": "AC-2 missing"}]})
    assert r.status_code == 201, r.text
    assert [s["status"] for s in r.json()["stored"]] == ["green", "red"]
    body = client.get("/v1/golden-journeys").json()
    today = {j["id"]: j["today"]["laptop"] for j in body["journeys"]}
    assert today["J1"]["status"] == "green" and today["J1"]["verdict"] == "met"
    assert today["J5"]["status"] == "red" and "AC-2" in today["J5"]["why"]
    assert today["J3"]["status"] == "not_run"
    assert body["needed"] == 5 and body["green_days"] == 0
    assert {p["id"] for p in body["pending"]} >= {"J2", "J4", "J7", "J8", "J9", "J10"}


def test_an_unknown_journey_or_status_is_refused(client):
    r = client.post("/v1/golden-journeys/results", json={"results": [
        {"journey_id": "J404", "status": "green"}]})
    assert r.status_code == 422 and r.json()["error"] == "unknown_journey"
    r = client.post("/v1/golden-journeys/results", json={"results": [
        {"journey_id": "J1", "status": "fine"}]})
    assert r.status_code == 422


def test_a_red_stays_red_for_its_day_when_run_again(engine, client):
    for status in ("red", "green"):
        client.post("/v1/golden-journeys/results", json={"host": "laptop", "results": [
            {"journey_id": "J1", "status": status, "mode": "real", "why": "first" if status == "red" else ""}]})
    with engine.begin() as c:
        row = c.execute(text("SELECT status, runs, why FROM golden_journey_results "
                             "WHERE journey_id = 'J1'")).mappings().one()
    assert row["status"] == "red" and row["runs"] == 2 and "run again: green" in row["why"]


def test_the_counter_counts_whole_green_days_and_resets_on_red(engine, client):
    today = results.today()
    for k in (1, 2, 3):
        green_day(engine, today - timedelta(days=k))
    assert client.get("/v1/golden-journeys").json()["green_days"] == 3
    green_day(engine, today, red=("J3",))
    body = client.get("/v1/golden-journeys").json()
    assert body["green_days"] == 0
    assert not body["history"][-1]["green"] and "J3 red" in body["history"][-1]["short"]


def test_five_green_days_lift_the_freeze(engine, client):
    today = results.today()
    for k in range(5):
        green_day(engine, today - timedelta(days=k))
    body = client.get("/v1/golden-journeys").json()
    assert body["green_days"] == 5 and body["freeze_lifted"] is True


def test_the_daily_run_is_a_schedule_work_wake_and_arming_again_replaces_it(engine, client):
    pid = get_settings().default_project_id
    r = client.post("/v1/golden-journeys/schedule",
                    json={"project_id": pid, "at_time": "06:00", "timezone": "Europe/London"})
    assert r.status_code == 201, r.text
    client.post("/v1/golden-journeys/schedule", json={"project_id": pid, "at_time": "07:30"})
    with engine.begin() as c:
        rows = c.execute(text("SELECT kind, status, at_time, prompt FROM agent_wakeups "
                              "WHERE project_id = :p ORDER BY created_at"), {"p": pid}).mappings().all()
    assert [(r["kind"], r["status"], r["at_time"]) for r in rows] == [
        ("schedule", "stopped", "06:00"), ("schedule", "active", "07:30")]
    assert "run_golden_journeys" in rows[-1]["prompt"]
    body = client.get(f"/v1/golden-journeys?project_id={pid}").json()
    assert body["schedule"]["at_time"] == "07:30"


def test_a_bad_schedule_is_refused_in_words(client):
    r = client.post("/v1/golden-journeys/schedule",
                    json={"project_id": get_settings().default_project_id, "at_time": "25:99"})
    assert r.status_code == 422


def test_a_run_records_one_row_per_journey(engine, monkeypatch):
    """The tool's run: every journey judged and stored; pending ones stored as pending."""
    class Down:
        mode = "real"

        def call(self, method, path, body=None):
            return 503, {"detail": "down"}

    out = service.start(engine, get_settings(), project_id=get_settings().default_project_id,
                        actor="ceo", client=Down(), wait=True, only=["J3", "J4"])
    assert out["ok"], out
    with engine.begin() as c:
        rows = dict(c.execute(text("SELECT journey_id, status FROM golden_journey_results")).all())
    assert rows == {"J3": "red", "J4": "pending"}
