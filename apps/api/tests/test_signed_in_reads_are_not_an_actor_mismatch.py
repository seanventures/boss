"""A signed-in project read is not an actor_mismatch (BOSSAPP-669, 668, 639).

The legacy console key's transport principal is ``sys_legacy_console``. Under enforce the guard
binds the actor to that principal's legacy member, ``executive_console``. These reads handed
``require_authenticated_actor_binding`` the principal id (or another name the client did not
send), so a call that named no actor was refused 403 actor_mismatch: "the actor is the
signed-in principal; it cannot be named".

The same key, naming nobody, can read its own project. The routes stay authenticated.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.tests.setup_helpers import PROJECT, fakes

KEY = "legacy-console-key-actor-mismatch-00000000"

#: (path, key that a successful read carries). No actor is sent.
READS = (
    ("/v1/secrets", "secrets"),
    ("/v1/worker-autoscaling/policies", "items"),
    ("/v1/worker-autoscaling/status", "project_id"),
    ("/v1/worker-autoscaling/history", "items"),
    ("/v1/agent/briefing", "items"),
    ("/v1/setup", "models"),
    ("/v1/setup/status", "steps"),
    ("/v1/setup/coding-agents", "agents"),
    ("/v1/setup/decision-model", "backend"),
    ("/v1/setup/models/openrouter/catalogue", "models"),
)


@pytest.fixture
def console_key(engine, monkeypatch):
    from apps.api.app import main
    from boss_engine.config import get_settings
    from boss_engine.identity import legacy, migration

    for settings in (main.settings, get_settings()):
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", KEY)
        monkeypatch.setattr(settings, "api_key_principals", f"{KEY}:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key", KEY)
        monkeypatch.setattr(settings, "authz_mode", "enforce")
        monkeypatch.setattr(settings, "authz_router_modes", "")
    legacy.invalidate_cache()
    migration.forget_seeded()
    fakes(monkeypatch)
    yield TestClient(main.app), {"Authorization": f"Bearer {KEY}"}
    legacy.invalidate_cache()
    migration.forget_seeded()


def test_a_signed_in_console_key_reads_its_project_without_naming_an_actor(console_key):
    """No second actor name. The production break: a read passes the transport principal
    (``sys_legacy_console``) to the actor binding instead of the guard's bound actor."""
    client, auth = console_key
    session = client.get("/v1/auth/session", headers=auth)
    assert session.status_code == 200, session.text
    assert session.json()["principal_member_id"] == "sys_legacy_console"
    failed = []
    for path, key in READS:
        response = client.get(path, params={"project_id": PROJECT}, headers=auth)
        if response.status_code != 200 or key not in response.json():
            failed.append(f"{path} -> {response.status_code} {response.text[:400]}")
    assert not failed, "\n".join(failed)


def test_naming_a_different_actor_is_still_refused(console_key):
    """An actor query that is not the signed-in principal still 403s actor_mismatch.

    The production break: dropping the comparison, or treating every supplied name as the
    signed-in actor. Naming the principal itself (sys_legacy_console) is not someone else.
    """
    client, auth = console_key
    refused = []
    allowed = []
    for path, key in READS:
        other = client.get(path, params={"project_id": PROJECT, "actor": "someone_else"},
                           headers=auth)
        detail = other.json().get("detail") if other.headers.get("content-type", "").startswith("application/json") else {}
        reason = detail.get("reason") if isinstance(detail, dict) else None
        if other.status_code != 403 or reason != "actor_mismatch":
            refused.append(f"{path} -> {other.status_code} {other.text[:300]}")
        own = client.get(path, params={"project_id": PROJECT, "actor": "sys_legacy_console"},
                         headers=auth)
        if own.status_code != 200 or key not in own.json():
            allowed.append(f"{path} -> {own.status_code} {own.text[:300]}")
    assert not refused, "named someone else:\n" + "\n".join(refused)
    assert not allowed, "named the signed-in principal:\n" + "\n".join(allowed)


def test_these_reads_stay_authenticated(console_key):
    """No key is not a read. The production break: dropping transport auth on these routes."""
    client, _auth = console_key
    open_routes = []
    for path, _key in READS:
        response = client.get(path, params={"project_id": PROJECT})
        if response.status_code != 401:
            open_routes.append(f"{path} -> {response.status_code} {response.text[:200]}")
    assert not open_routes, "\n".join(open_routes)
