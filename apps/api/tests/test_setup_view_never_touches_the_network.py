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
    monkeypatch.setattr(api_main.settings, "target_github_owner", "e2e")
    monkeypatch.setattr(api_main.settings, "target_github_repo", "app")
    monkeypatch.setattr(api_main.settings, "openai_api_key", "sk-stub-ok")
    offline()
    body = view(client)
    [repo] = body["repository"]["repos"]
    assert repo["full_name"] == "e2e/app" and body["repository"]["connection"]["source"] == "install"
    [model] = body["models"]
    assert (model["label"], model["source"], model["pill"]["label"]) == (
        "OpenAI", "install", "Not checked")
    assert body["ready"] is True                    # never told it is unconfigured


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
    response = client.put("/v1/setup/budget", headers=AUTH,
                          json={"project_id": PROJECT, "monthly_cap_usd": 0.01})
    assert response.status_code == 200
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO budget_reservations (id, project_id, category, amount_usd_micros, "
            "reason, status, expires_at) VALUES ('br-test', :p, 'model', 20000, 'test', 'active', "
            "now() + interval '1 hour')"),
            {"p": PROJECT})
    body = view(client)
    assert body["budget"]["state"] == "spent" and body["verdict"]["label"] == "Budget spent"
    assert body["models"][0]["pill"]["label"] == "Paused · budget"


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
