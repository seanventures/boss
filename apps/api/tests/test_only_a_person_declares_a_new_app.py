"""ONLY A PERSON DECLARES A NEW APP, UNDER EVERY MODE (ENT-FEAT-01 review of #352).

A declared new app is never stopped by another app's unmerged branch (app_scope), so who may
declare one matters. The start door honours `new_app` only from a person: the credential the
request PRESENTS is read even when the guard decides nothing (identity_guard.presented_identity:
BOSS_AUTHZ_MODE=off, or shadow with no credential). An agent's run token, or no credential at all,
asks in vain — the run starts, not declared a new app.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.identity_s1_world import (  # noqa: F401 — world is a fixture
    HEADERS, csrf, fresh_client, make_user, sign_in, world)
from apps.api.tests.test_identity_authn_run_token import TOKEN, _agent, _install_verifier


#: The identity world (identity_s1_world): real sign-in, sessions and run-token verification.
pytestmark = pytest.mark.usefixtures("world")


def _project() -> str:
    return get_settings().default_project_id


def _start(client, headers=None) -> dict:
    """POST /v1/delivery-workflows/start asking for a new app, past what this test project lacks."""
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": _project(), "actor": "ceo"}, headers=headers)
    assert installed.status_code == 200, installed.text
    body = {"project_id": _project(), "actor": "ceo", "workflow_key": "minor_feature",
            "input": {"title": "Parking Rota", "directive": "A parking rota.", "new_app": True}}
    started = client.post("/v1/delivery-workflows/start", json=body, headers=headers)
    if started.status_code == 409 and isinstance(started.json().get("detail"), dict):
        missing = [m["key"] for m in started.json()["detail"]["readiness"]["missing"]]
        started = client.post("/v1/delivery-workflows/start",
                              json={**body, "start_anyway": missing}, headers=headers)
    assert started.status_code == 200, started.text
    return started.json()["run"]


def _declared(engine, run) -> bool:
    with engine.begin() as connection:
        run_input = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                                       {"r": run["id"]}).scalar()
    return run_input.get("new_app") is True


@pytest.fixture()
def asked(monkeypatch):
    """Who the door saw present each request (the AuthContext app_scope.a_person_asked got)."""
    from boss_engine.services import app_scope

    seen: list = []
    real = app_scope.a_person_asked
    monkeypatch.setattr(app_scope, "a_person_asked", lambda auth: seen.append(auth) or real(auth))
    return seen


@pytest.fixture()
def mode(monkeypatch):
    def set_mode(value: str) -> None:
        monkeypatch.setattr(get_settings(), "authz_mode", value)
    return set_mode


def test_a_persons_session_declares_a_new_app_even_with_identity_off(engine, mode, asked):
    make_user(engine, "rota@example.test", role="operator")
    client = fresh_client()
    assert sign_in(client, "rota@example.test").status_code == 200
    headers = csrf(client)
    mode("off")
    assert _declared(engine, _start(client, headers)) is True
    assert asked[-1].principal.is_person and asked[-1].session is not None


def test_an_agents_run_token_never_declares_one_with_identity_off(engine, mode, asked,
                                                                   monkeypatch):
    person = make_user(engine, "owner@example.test", role="owner")
    agent = _agent(engine)
    _install_verifier(monkeypatch, engine, person, agent, [])
    mode("off")
    try:
        token = {**HEADERS, "Authorization": f"DPoP {TOKEN}", "DPoP": "p.r.f"}
        assert _declared(engine, _start(fresh_client(), token)) is False
        # Refused for what it IS (an agent's run token), not for failing to authenticate.
        assert asked[-1] is not None and asked[-1].run_token is not None
        assert asked[-1].principal.id == agent
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM identity_principals WHERE id = :a"), {"a": agent})


def test_no_credential_never_declares_one_in_shadow_mode(engine, mode, asked):
    mode("shadow")
    assert _declared(engine, _start(fresh_client())) is False
    assert asked[-1] is None
