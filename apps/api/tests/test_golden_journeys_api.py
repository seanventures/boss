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
from boss_engine.services.golden_journeys import definitions, results, runner, service


@pytest.fixture(autouse=True)
def _clean(engine, monkeypatch):
    monkeypatch.setenv("BOSS_JOURNEY_HOSTS", "laptop")
    monkeypatch.setenv("BOSS_JOURNEY_PROJECT", get_settings().default_project_id)
    monkeypatch.delenv("BOSS_JOURNEY_PRINCIPAL", raising=False)
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


class Builds:
    """The API a J1 run talks to: a build that completes with a PR and every gate passed."""

    mode = "real"

    def call(self, method, path, body=None):
        if path.startswith("/v1/ticket-systems/"):
            return 200, {"ticket": {"title": "Slug", "body": "Build it.", "acceptance_criteria": ["works"]}}
        if method == "POST":
            return 200, {"run_id": "crun_1"}
        if path.endswith("/gates"):
            return 200, {"gates": [{"name": n, "ok": True} for n in
                                   ("code_review_passed", "acceptance_met", "existing_tests_intact")]}
        return 200, {"status": "completed", "pull_request_url": "https://example.test/pr/1"}


def j1_green():
    j1 = {j.id: j for j in definitions.load_all()}["J1"]
    row = runner.run_journey(j1, Builds(), sleep=lambda s: None, variables={
        "project_id": "p", "owner": "o", "repo": "r", "ticket_connection": "c", "ticket_key": "K-1"})
    assert row["status"] == "green", row["why"]
    return row


def test_results_post_and_today_reads_them_back(client, monkeypatch):
    monkeypatch.setenv("BOSS_JOURNEY_PRINCIPAL", "anonymous")     # no identity in this app
    r = client.post("/v1/golden-journeys/results", json={"host": "laptop", "results": [
        j1_green(), {"journey_id": "J5", "status": "red", "mode": "real", "why": "AC-2 missing"}]})
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "run"
    assert [s["status"] for s in r.json()["stored"]] == ["green", "red"]
    body = client.get("/v1/golden-journeys").json()
    today = {j["id"]: j["today"]["laptop"] for j in body["journeys"]}
    assert today["J1"]["status"] == "green" and today["J1"]["verdict"] == "met"
    assert today["J1"]["recorded_by"] == "anonymous" and today["J1"]["source"] == "run"
    assert today["J5"]["status"] == "red" and "AC-2" in today["J5"]["why"]
    assert today["J3"]["status"] == "not_run"
    assert body["needed"] == 5 and body["green_days"] == 0
    assert {p["id"] for p in body["pending"]} >= {"J2", "J4", "J7", "J9", "J10"}


def test_a_posted_green_the_verdict_does_not_accept_is_refused(client):
    row = j1_green()
    forged = dict(row, reply=row["reply"].replace('"status": "completed"', '"status": "done!!"'))
    r = client.post("/v1/golden-journeys/results", json={"results": [forged]})
    assert r.status_code == 422 and r.json()["error"] == "not_green"
    bare = {"journey_id": "J1", "status": "green", "verdict": "met"}
    assert client.post("/v1/golden-journeys/results", json={"results": [bare]}).status_code == 422


def test_rows_posted_by_anyone_but_the_journey_account_never_count(engine, client):
    today = results.today()
    for k in (1, 2):
        green_day(engine, today - timedelta(days=k))
    r = client.post("/v1/golden-journeys/results", json={"host": "laptop", "results": [j1_green()]})
    assert r.status_code == 201 and r.json()["source"] == "posted"
    body = client.get("/v1/golden-journeys").json()
    assert body["green_days"] == 2           # today's posted green adds nothing
    j1 = next(j for j in body["journeys"] if j["id"] == "J1")
    assert j1["today"]["laptop"]["source"] == "posted"


def test_a_posted_row_never_replaces_the_installs_own_run(engine, client):
    with engine.begin() as c:
        results.record(c, {"journey_id": "J5", "status": "red", "mode": "real"}, host="laptop")
    r = client.post("/v1/golden-journeys/results", json={"host": "laptop", "results": [
        {"journey_id": "J5", "status": "red", "mode": "real"}]})
    assert r.status_code == 422 and "own run" in r.json()["message"]


def test_a_huge_result_is_stored_not_lost(engine):
    calls = [{"id": "E1", "cmd": "GET /x", "kind": "http", "stdout": "é\\\"" * 300_000}]
    with engine.begin() as c:
        results.record(c, {"journey_id": "J3", "status": "red", "mode": "real", "calls": calls},
                       host="laptop")
        kept = c.execute(text("SELECT calls FROM golden_journey_results WHERE journey_id='J3'")).scalar()
    assert "characters trimmed" in kept[0]["stdout"]


def test_an_unknown_journey_or_status_is_refused(client):
    r = client.post("/v1/golden-journeys/results", json={"results": [
        {"journey_id": "J404", "status": "green"}]})
    assert r.status_code == 422 and r.json()["error"] == "unknown_journey"
    r = client.post("/v1/golden-journeys/results", json={"results": [
        {"journey_id": "J1", "status": "fine"}]})
    assert r.status_code == 422


def test_a_red_stays_red_for_its_day_when_run_again(engine, client):
    with engine.begin() as c:
        results.record(c, {"journey_id": "J1", "status": "red", "mode": "real", "why": "first"},
                       host="laptop")
        results.record(c, {"journey_id": "J1", "status": "green", "mode": "real"}, host="laptop")
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


def test_the_daily_run_is_one_install_wide_wake_and_arming_again_replaces_it(engine, client):
    pid = get_settings().default_project_id
    r = client.post("/v1/golden-journeys/schedule",
                    json={"project_id": "someone-elses", "at_time": "06:00", "timezone": "Europe/London"})
    assert r.status_code == 201, r.text
    assert r.json()["project_id"] == pid           # the fixture project, never the caller's
    client.post("/v1/golden-journeys/schedule", json={"at_time": "07:30"})
    with engine.begin() as c:
        rows = c.execute(text("SELECT kind, status, at_time, prompt, purpose FROM agent_wakeups "
                              "WHERE project_id = :p ORDER BY created_at"), {"p": pid}).mappings().all()
    assert [(r["kind"], r["status"], r["at_time"]) for r in rows] == [
        ("schedule", "stopped", "06:00"), ("schedule", "active", "07:30")]
    assert [r["purpose"] for r in rows] == [service.PURPOSE, service.PURPOSE]
    body = client.get("/v1/golden-journeys").json()
    assert body["schedule"]["at_time"] == "07:30"


def test_a_bad_schedule_is_refused_in_words(client):
    r = client.post("/v1/golden-journeys/schedule", json={"at_time": "25:99"})
    assert r.status_code == 422


class Narrow:
    """The journey account (narrow, fixture project only), and an API that is down."""

    mode = "real"

    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        pid = get_settings().default_project_id
        if path == "/v1/identity/me":
            return 200, {"principal_id": "sa_j", "kind": "service_account", "access_roles": []}
        if path == "/v1/identity/me/projects":
            return 200, {"projects": [{"project_id": pid, "permissions": ["project:read"], "via": ["project"]}]}
        return 503, {"detail": "down"}


def test_a_run_records_one_row_per_journey_in_the_fixture_project(engine, monkeypatch):
    monkeypatch.setenv("BOSS_JOURNEY_VARS", '{"project_id": "customer", "owner": "o", "java_repo": "j"}')
    api = Narrow()
    out = service.start(engine, get_settings(), actor="ceo", client=api, wait=True, only=["J3", "J4"])
    assert out["ok"], out
    assert out["project_id"] == get_settings().default_project_id
    with engine.begin() as c:
        rows = dict(c.execute(text("SELECT journey_id, status FROM golden_journey_results")).all())
    assert rows == {"J3": "red", "J4": "pending"}
    build = next(b for m, p, b in api.calls if p == "/v1/code-tasks")
    assert build["project_id"] == get_settings().default_project_id      # never "customer"


def test_a_due_wake_starts_the_run_without_a_model_turn(engine, monkeypatch):
    from boss_engine.services import agent_wakeups

    monkeypatch.delenv("BOSS_JOURNEY_KEY_REF", raising=False)
    with engine.begin() as c:
        row = service.arm_daily(c, project_id=get_settings().default_project_id, actor="ceo",
                                at_time="06:00")
        due = dict(c.execute(text("SELECT * FROM agent_wakeups WHERE id = :i"), {"i": row["id"]}).mappings().one())
    called = []
    monkeypatch.setattr("boss_engine.services.agent_service.AgentService.chat",
                        lambda *a, **k: called.append(1))
    out = agent_wakeups.wake_one(get_settings(), engine, due)
    assert called == []                                   # no agent turn
    assert "BOSS_JOURNEY_KEY_REF" in out["error"]         # refused: never unauthenticated


def test_schedule_work_cannot_make_or_imitate_the_journeys_wake(engine, monkeypatch):
    """An agent's schedule_work (autopilot:operate) must never start a journeys run: the marker
    prompt is refused, and a row that only SAYS the marker is an ordinary agent wake."""
    from boss_engine.services import agent_wakeups
    from boss_engine.tools.builtin import build_default_registry

    pid = get_settings().default_project_id
    with engine.begin() as c:
        with pytest.raises(agent_wakeups.WakeupError, match="reserved"):
            agent_wakeups.schedule(c, project_id=pid, actor="ceo", prompt="[golden-journeys] go",
                                   at_time="06:00")
    spec = build_default_registry().get("schedule_work")
    from boss_engine.context import EngineContext

    out = spec.handler(EngineContext.create(), project_id=pid, actor="ceo",
                       args={"prompt": "  [Golden-Journeys] run them", "at_time": "06:00"})
    assert out["ok"] is False and "reserved" in out["error"]
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM agent_wakeups")).scalar() == 0
        # a row written around schedule() with the marker in its words, but no purpose
        c.execute(text(
            "INSERT INTO agent_wakeups (id, project_id, kind, actor, prompt, at_time, days, "
            "timezone, next_wake_at, max_cycles, created_by) VALUES ('wake_fake', :p, 'schedule', "
            "'ceo', '[golden-journeys] x', '06:00', '[]'::jsonb, 'UTC', now(), 0, 'ceo')"), {"p": pid})
        due = dict(c.execute(text("SELECT * FROM agent_wakeups WHERE id = 'wake_fake'")).mappings().one())
    started = []
    monkeypatch.setattr(service, "start", lambda *a, **k: started.append(1) or {"ok": True, "started": []})
    monkeypatch.setattr("boss_engine.services.agent_service.AgentService.chat",
                        lambda *a, **k: {"reply": "an ordinary turn"})
    agent_wakeups.wake_one(get_settings(), engine, due)
    assert started == []
