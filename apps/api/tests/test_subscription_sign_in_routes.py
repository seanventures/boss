"""The sign-in routes (BOSSAPP-769): the one subscription route, over HTTP, with a stub CLI.

POST /v1/setup/sign-in starts the provider's own login inside BOSS; GET polls it; POST .../code
hands a pasted code to the CLI; DELETE cancels. Refusals are {state, headline, fix}. The old
import door, POST /v1/setup/coding-agents/credential, is gone.
"""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, fakes
from boss_engine.services import subscription_sign_in as sign_in


@pytest.fixture(autouse=True)
def _clean():
    sign_in.reset_for_tests()
    yield
    sign_in.reset_for_tests()


def _cli(tmp_path: Path, provider: str, monkeypatch) -> None:
    """Point the provider's login at a stub that prints a link + code and waits for approval."""
    path = tmp_path / provider
    if provider == "claude_code":
        body = ("echo 'visit: https://claude.com/cai/oauth/authorize?code=true&state=s'\n"
                "printf 'Paste code here if prompted > '\nread c\n"
                f"[ \"$c\" = ok#s ] && touch '{tmp_path}/done' && exit 0\nexit 1\n")
    else:
        body = ("echo 'https://auth.openai.com/codex/device'\necho '   WXYZ-12345'\n"
                f"while [ ! -e '{tmp_path}/approve' ]; do sleep 0.05; done\n"
                f"touch '{tmp_path}/done'\n")
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    login = sign_in.LOGINS[provider]
    monkeypatch.setitem(sign_in.LOGINS, provider, sign_in._Login(
        login.label, (str(path),), login.url_hosts, login.device_code, login.drop_env))
    monkeypatch.setattr(sign_in, "_signed_in_check",
                        lambda _p, _s: (lambda: (tmp_path / "done").exists()))


def _poll(client, sign_in_id: str, state: str) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/v1/setup/sign-in/{sign_in_id}", headers=AUTH,
                          params={"project_id": PROJECT}).json()
        if body["state"] == state:
            return body
        time.sleep(0.05)
    raise AssertionError(f"never reached {state}: {body}")


def test_device_sign_in_over_http(client, monkeypatch, tmp_path):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    _cli(tmp_path, "codex", monkeypatch)
    started = client.post("/v1/setup/sign-in", headers=AUTH,
                          json={"project_id": PROJECT, "provider": "codex"})
    assert started.status_code == 200, started.text
    session = started.json()
    assert session["state"] == "waiting"
    assert session["verification_url"] == "https://auth.openai.com/codex/device"
    assert session["user_code"] == "WXYZ-12345"
    assert session["needs_code_input"] is False
    (tmp_path / "approve").touch()
    done = _poll(client, session["sign_in_id"], "signed_in")
    assert done["headline"] and done["fix"]


def test_claude_code_is_pasted_back_and_cancel_kills(client, monkeypatch, tmp_path):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    _cli(tmp_path, "claude_code", monkeypatch)
    session = client.post("/v1/setup/sign-in", headers=AUTH,
                          json={"project_id": PROJECT, "provider": "claude_code"}).json()
    assert session["needs_code_input"] is True
    sent = client.post(f"/v1/setup/sign-in/{session['sign_in_id']}/code", headers=AUTH,
                       json={"project_id": PROJECT, "code": "ok#s"})
    assert sent.status_code == 200, sent.text
    assert "ok#s" not in sent.text
    _poll(client, session["sign_in_id"], "signed_in")

    # A code when none is expected is a {state, headline, fix} refusal.
    again = client.post(f"/v1/setup/sign-in/{session['sign_in_id']}/code", headers=AUTH,
                        json={"project_id": PROJECT, "code": "ok#s"})
    assert again.status_code == 409
    assert set(again.json()["detail"]) == {"state", "headline", "fix"}


def test_cancel(client, monkeypatch, tmp_path):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    _cli(tmp_path, "codex", monkeypatch)
    session = client.post("/v1/setup/sign-in", headers=AUTH,
                          json={"project_id": PROJECT, "provider": "codex"}).json()
    cancelled = client.delete(f"/v1/setup/sign-in/{session['sign_in_id']}", headers=AUTH,
                              params={"project_id": PROJECT})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"


def test_refusals_are_pills(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    bad = client.post("/v1/setup/sign-in", headers=AUTH,
                      json={"project_id": PROJECT, "provider": "cursor"})
    assert bad.status_code == 422
    assert set(bad.json()["detail"]) == {"state", "headline", "fix"}
    missing = client.get("/v1/setup/sign-in/si_nope", headers=AUTH,
                         params={"project_id": PROJECT})
    assert missing.status_code == 404
    assert set(missing.json()["detail"]) == {"state", "headline", "fix"}


def test_an_expired_login_signs_in_again(client, engine, monkeypatch, tmp_path):
    """`codex login status` says "Logged in" about a dead session; the last proof knows."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    _cli(tmp_path, "codex", monkeypatch)
    monkeypatch.setattr(sign_in, "_signed_in_check", lambda _p, _s: (lambda: True))
    with engine.begin() as c:
        before = c.execute(text(
            "SELECT id, connection_metadata FROM entitlement_sources "
            "WHERE project_id = :p AND source_id = 'openai_codex_chatgpt'"),
            {"p": PROJECT}).mappings().first()
        if before is None:
            c.execute(text(
                "INSERT INTO entitlement_sources (id, project_id, provider, source_id, "
                "source_kind, entitlement_kind, owner_type, owner_id) VALUES ('es_si_test', :p, "
                "'openai', 'openai_codex_chatgpt', 'subscription', 'subscription', 'org', 'o')"),
                {"p": PROJECT})
        c.execute(text(
            "UPDATE entitlement_sources SET connection_metadata = COALESCE(connection_metadata, "
            "'{}'::jsonb) || CAST(:m AS JSONB) WHERE project_id = :p "
            "AND source_id = 'openai_codex_chatgpt'"),
            {"p": PROJECT, "m": json.dumps({"last_check": {"state": "login_expired"}})})
    try:
        session = client.post("/v1/setup/sign-in", headers=AUTH,
                              json={"project_id": PROJECT, "provider": "codex"}).json()
        assert session["state"] == "waiting", session
    finally:
        with engine.begin() as c:
            if before is None:
                c.execute(text("DELETE FROM entitlement_sources WHERE id = 'es_si_test'"))
            else:
                c.execute(text("UPDATE entitlement_sources SET connection_metadata = "
                               "CAST(:m AS JSONB) WHERE id = :i"),
                          {"i": before["id"], "m": json.dumps(before["connection_metadata"])})


def test_already_signed_in_answers_signed_in(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setattr(sign_in, "_signed_in_check", lambda _p, _s: (lambda: True))
    session = client.post("/v1/setup/sign-in", headers=AUTH,
                          json={"project_id": PROJECT, "provider": "grok"}).json()
    assert session["state"] == "signed_in"


def test_the_import_door_is_gone(client, monkeypatch):
    as_ceo(monkeypatch)
    response = client.post("/v1/setup/coding-agents/credential", headers=AUTH, json={
        "project_id": PROJECT, "provider": "codex", "credential": "never-stored"})
    assert response.status_code in (404, 405), response.text
    from apps.api.app import main as api_main

    paths = {getattr(route, "path", "") for route in api_main.app.routes}
    assert "/v1/setup/coding-agents/credential" not in paths
    assert {"/v1/setup/sign-in", "/v1/setup/sign-in/{sign_in_id}",
            "/v1/setup/sign-in/{sign_in_id}/code"} <= paths
