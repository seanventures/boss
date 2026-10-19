"""GET /v1/setup renders what the checks recorded. It never calls GitHub or a provider.

The old status card called GitHub on every page load (rate limit, slowness) and still could not
say when a model last worked. This builds the view for the fixture projects with every network
path raising, and checks the pills, `ready` and `next` each one should have.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.setup_helpers import (
    AUTH, PROJECT, as_ceo, connect_key, connect_repository, fakes, view,
)


@pytest.fixture()
def offline(monkeypatch):
    """Every way out: httpx, urllib, and the checks' own transport."""
    import httpx
    import urllib.request

    from boss_engine.services import setup_checks

    def forbidden(*_a, **_k):
        raise AssertionError("GET /v1/setup touched the network")

    def go_offline():
        monkeypatch.setattr(httpx, "request", forbidden)
        monkeypatch.setattr(urllib.request, "urlopen", forbidden)
        monkeypatch.setattr(setup_checks, "default_http", forbidden)

    return go_offline


def test_an_empty_project(client, monkeypatch, offline):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    offline()
    body = view(client)
    assert (body["ready"], body["next"], body["verdict"]["label"]) == (False, "repository",
                                                                       "2 to go")
    assert body["repository"]["repos"] == [] and body["models"] == []
    assert body["budget"]["cap_usd"] == 200 and body["budget"]["state"] == "ok"
    assert {j["job"] for j in body["jobs"]} == {"build", "review", "chat", "decisions", "hard",
                                                "routine"}


def test_an_install_configured_from_its_settings_file(client, monkeypatch, offline):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setattr(api_main.settings, "github_token", "ghp_e2e_full")
    # The pair README installs set (BOSS_GITHUB_OWNER / BOSS_GITHUB_REPO), not only TARGET_*.
    monkeypatch.setattr(api_main.settings, "github_owner", "e2e", raising=False)
    monkeypatch.setattr(api_main.settings, "github_repo", "app", raising=False)
    monkeypatch.setattr(api_main.settings, "openai_api_key", "sk-stub-ok")
    offline()
    body = view(client)
    [repo] = body["repository"]["repos"]
    assert repo["full_name"] == "e2e/app" and body["repository"]["connection"]["source"] == "install"
    [model] = body["models"]
    assert (model["label"], model["source"], model["pill"]["label"]) == (
        "OpenAI", "install", "Not checked")
    # Never told it is unconfigured — and never told Ready over a check that has not run.
    assert repo["pill"]["label"] == "Checking…" and body["ready"] is False


def test_connected_then_expired_then_budget_red(client, engine, monkeypatch, offline):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    first = connect_repository(client)
    assert connect_key(client).status_code == 200
    added = client.post("/v1/setup/repository/select", headers=AUTH, json={
        "project_id": PROJECT, "connection_id": first["repository"]["connection"]["id"],
        "full_name": "e2e/site", "make_default": False})
    assert added.status_code == 200
    offline()
    body = view(client)
    assert body["ready"] is True and body["next"] == "try"
    assert body["verdict"]["label"] == "Ready"
    assert [r["full_name"] for r in body["repository"]["repos"]] == ["e2e/app", "e2e/site"]
    with engine.begin() as c:
        c.execute(text("UPDATE github_connections SET expires_at = :t WHERE project_id = :p"),
                  {"t": datetime.now(timezone.utc) - timedelta(hours=1), "p": PROJECT})
    body = view(client)
    assert {r["pill"]["label"] for r in body["repository"]["repos"]} == {"Expired"}
    assert body["verdict"]["label"] == "2 problems" and body["ready"] is False
    with engine.begin() as c:
        c.execute(text("UPDATE github_connections SET expires_at = :t WHERE project_id = :p"),
                  {"t": datetime.now(timezone.utc) + timedelta(days=2, hours=2), "p": PROJECT})
    body = view(client)
    assert body["repository"]["repos"][0]["pill"]["label"] == "Expires in 2d"
    assert body["ready"] is True
    # J8: ONE answer for the budget row, the header and the metered model — the gate's own.
    # A $0.01 cap cannot fit one metered check, so it is Spent everywhere at once ...
    response = client.put("/v1/setup/budget", headers=AUTH,
                          json={"project_id": PROJECT, "monthly_cap_usd": 0.01})
    assert response.status_code == 200
    body = response.json()
    assert body["budget"]["state"] == "spent" and body["budget"]["pill"]["label"] == "Spent"
    assert body["verdict"]["label"] == "Budget spent"
    assert body["models"][0]["pill"]["label"] == "Paused · budget"
    # ... and Save on a real cap clears all three, with no re-test.
    body = client.put("/v1/setup/budget", headers=AUTH,
                      json={"project_id": PROJECT, "monthly_cap_usd": 200}).json()
    assert body["budget"]["pill"]["label"] == "OK"
    assert body["models"][0]["pill"]["label"] == "Working"
    assert body["verdict"]["label"] != "Budget spent"


def test_paused_budget_names_the_cap_that_is_actually_binding(client, engine, monkeypatch, offline):
    """"Paused · budget" used to say "Monthly cap reached" no matter which cap refused the
    call. A key tester's monthly cap had room; it was the DAILY cap that was tiny, and the
    pill still blamed "Monthly" — the operator raised the wrong one. The tip now names
    whichever cap is actually binding (J8 follow-up, boss-1 rc10.9)."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert connect_key(client).status_code == 200
    offline()
    # The monthly cap stays generous; only the daily cap is tiny, so the DAY period is the one
    # with no room left — the month is nowhere close.
    with engine.begin() as c:
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = 1, "
                       "monthly_cap_usd_micros = 200000000, version = version + 1 "
                       "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT})
    body = view(client)
    assert body["budget"]["period"] == "day"
    pill = body["models"][0]["pill"]
    assert pill["label"] == "Paused · budget"
    assert pill["tip"] == "Daily cap reached. Raise it or wait for reset."


def test_a_budget_cap_refusal_names_the_real_reason(client, engine, monkeypatch, offline):
    """POST /v1/setup/models, end to end, with the cap already spent: the refusal is "Paused ·
    budget" with the API's own sentence ("Budget cap exceeded: …") ahead of the generic tip —
    never only the tip, which connection_spend.py used to drop on the floor (boss-1 rc10.9:
    proof["summary"] was never set, so setup_actions._failure had nothing but "Monthly/Daily
    cap reached" to show)."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    offline()
    with engine.begin() as c:
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = 200000000, "
                       "monthly_cap_usd_micros = 1, version = version + 1 "
                       "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT})
        if c.execute(text("SELECT 1 FROM budget_policies WHERE scope_type='project' "
                          "AND scope_id=:p"), {"p": PROJECT}).first() is None:
            c.execute(text("INSERT INTO budget_policies (id, scope_type, scope_id, "
                           "daily_cap_usd_micros, monthly_cap_usd_micros) "
                           "VALUES (:id, 'project', :p, 200000000, 1)"),
                     {"id": "bp_" + PROJECT, "p": PROJECT})
    refused = connect_key(client)
    assert refused.status_code == 422, refused.text
    refusal = refused.json()["detail"]
    assert refusal["headline"] == "Paused · budget"
    assert refusal["fix"].startswith("Budget cap exceeded:")
    assert "Budget cap reached" in refusal["fix"]  # the generic tip still follows it
    assert refusal.get("summary", "").startswith("Budget cap exceeded:")


def test_a_deferred_job_is_not_shown_as_a_budget_refusal(client, monkeypatch, offline):
    """POST /v1/setup/models, end to end, when BOSS's OWN job queue defers the proof's job
    (JobDeferredError) — nothing to do with money. It used to come back exactly like a cap
    refusal ("Paused · budget", a "Raise it" fix) and sent the operator to the Budget screen
    for a problem a cap could never fix (boss-1 rc10.9)."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    offline()
    from boss_engine.services.connection_spend import ConnectionSpend
    from boss_engine.services.jobs import JobDeferredError

    def deferred(self, connection, dedupe, role):
        raise JobDeferredError("queue_full")

    monkeypatch.setattr(ConnectionSpend, "_enqueue", deferred)
    refused = connect_key(client)
    assert refused.status_code == 422, refused.text
    refusal = refused.json()["detail"]
    assert refusal["headline"] == "Busy · retry"
    assert refusal["headline"] != "Paused · budget"
    assert "queue_full" in refusal["fix"]
    assert refusal.get("summary") == "queue_full"


@pytest.mark.parametrize("state", ["rate_limited", "unreachable"])
def test_a_check_github_did_not_answer_is_not_ready(client, engine, monkeypatch, offline, state):
    """READY MEANS PROVEN: "Repository and model checked" is false over a busy or unreachable
    GitHub. The row is still usable (a build may start), but the header says why it waits."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    connect_repository(client)
    assert connect_key(client).status_code == 200
    offline()
    assert view(client)["ready"] is True
    unknown = {"ok": None, "state": state, "at": None}
    checks = {"read": unknown, "write": unknown, "pull_requests": unknown, "state": state,
              "retry_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}
    import json
    with engine.begin() as c:
        c.execute(text("UPDATE github_repository_bindings SET checks = CAST(:c AS jsonb) "
                       " WHERE project_id = :p"), {"c": json.dumps(checks), "p": PROJECT})
    body = view(client)
    [repo] = body["repository"]["repos"]
    assert repo["pill"]["state"] == state and body["ready"] is False
    assert body["verdict"]["label"] == "1 to go" and body["verdict"]["tip"] == repo["pill"]["tip"]
    # The tip names a time, so the instant travels with it (the console shows it locally).
    if state == "rate_limited":
        assert body["verdict"]["at"] and body["verdict"]["at"] == repo["pill"]["at"]
    from boss_engine.services import setup_view
    assert setup_view.basics(body)["github"] is True      # usable: a build may still start


def test_a_read_never_waits_for_a_cli_or_the_secret_store(client, monkeypatch):
    """Connecting Claude Code hung the console: each GET asked the CLI for its catalog on the
    request thread. Now every probe is answered from a cache refreshed in the background."""
    import time as _time

    from boss_engine.services import cli_harness, setup_view

    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setattr(setup_view, "_detect_clis", lambda _s: _time.sleep(6) or {})
    monkeypatch.setattr(setup_view, "_pick_backend", lambda _s: _time.sleep(6) or "database")
    monkeypatch.setattr(cli_harness, "_read_model_catalog", lambda _spec: _time.sleep(6) or ())
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    setup_view.forget_cached()
    started = _time.monotonic()
    body = view(client)
    assert _time.monotonic() - started < 3
    assert body["detecting"] is True


def test_after_the_first_pr_a_failure_is_a_red_row_not_the_stepper(client, engine, monkeypatch):
    """Any failure after the first PR dropped the board back into the stepper."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    connect_repository(client)
    assert connect_key(client).status_code == 200
    with engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, "
                       "pull_request_url, pull_request_number) VALUES ('ctr-first', :p, 'x', "
                       "'completed', 'https://github.com/e2e/app/pull/1', 1)"), {"p": PROJECT})
        c.execute(text("UPDATE github_connections SET expires_at = now() - interval '1 hour' "
                       "WHERE project_id = :p"), {"p": PROJECT})
    body = view(client)
    assert body["repository"]["repos"][0]["pill"]["label"] == "Expired"
    assert body["next"] is None and body["try"]["done"] is True


def test_an_install_repository_is_checked_then_taken_over(client, monkeypatch):
    """M6: Test proves the settings-file repository, and Replace token moves it into BOSS —
    the picker offers the repository it is replacing."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setattr(api_main.settings, "github_token", "ghp_e2e_full")
    monkeypatch.setattr(api_main.settings, "github_owner", "e2e", raising=False)
    monkeypatch.setattr(api_main.settings, "github_repo", "app", raising=False)
    body = client.post("/v1/setup/checks", headers=AUTH,
                       json={"project_id": PROJECT, "target": "repository"}).json()
    assert body["repository"]["repos"][0]["pill"]["label"] == "Working"
    found = client.post("/v1/setup/repository/token", headers=AUTH,
                        json={"project_id": PROJECT, "token": "ghp_e2e_full"}).json()
    assert "e2e/app" in {r["full_name"] for r in found["repos"]}
    taken = client.post("/v1/setup/repository/select", headers=AUTH, json={
        "project_id": PROJECT, "connection_id": found["connection_id"],
        "full_name": "e2e/app"}).json()
    [repo] = taken["repository"]["repos"]
    assert repo["binding_id"] and taken["repository"]["connection"]["source"] == "connected"
    assert repo["pill"]["label"] == "Working"
