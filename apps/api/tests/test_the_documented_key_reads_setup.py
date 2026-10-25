"""The key the docs tell users to source works on Repo and models (audit P2-11).

`boss-community.env.sh` exports BOSS_API_KEY, the legacy console key. Under enforce its
transport principal is `sys_legacy_console`, while the guard binds the request's actor to that
principal's legacy member, `executive_console`. The setup router took its actor from the
principal alone, so `require_authenticated_actor_binding` saw two names and answered 403
actor_mismatch on GET /v1/setup and GET /v1/setup/coding-agents — and on every setup write.

Now reads ask only the project read their SETUP_READ names, and every router derives its actor
by the one rule (`request_actor_or_principal`).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.tests.setup_helpers import PROJECT, fakes

KEY = "legacy-console-key-p2-11-000000000000000"


@pytest.fixture
def env_sh_key(engine, monkeypatch):
    from apps.api.app import main
    from boss_engine.config import get_settings
    from boss_engine.identity import legacy, migration

    for settings in (main.settings, get_settings()):
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", KEY)
        monkeypatch.setattr(settings, "api_key_principals", f"{KEY}:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key", KEY)
        monkeypatch.setattr(settings, "authz_mode", "enforce")
    legacy.invalidate_cache()
    migration.forget_seeded()
    fakes(monkeypatch)
    yield TestClient(main.app), {"Authorization": f"Bearer {KEY}"}
    legacy.invalidate_cache()
    migration.forget_seeded()


def test_the_env_sh_key_reads_setup(env_sh_key):
    client, auth = env_sh_key
    session = client.get("/v1/auth/session", headers=auth)
    assert session.status_code == 200, session.text
    assert session.json()["principal_member_id"] == "sys_legacy_console"   # unchanged (round 2 f)
    setup = client.get("/v1/setup", params={"project_id": PROJECT}, headers=auth)
    assert setup.status_code == 200, setup.text
    assert isinstance(setup.json()["models"], list)
    agents = client.get("/v1/setup/coding-agents", params={"project_id": PROJECT}, headers=auth)
    assert agents.status_code == 200, agents.text
    assert isinstance(agents.json()["agents"], list)


def test_the_env_sh_key_is_not_refused_as_someone_else_on_a_write(env_sh_key):
    """The same rule for writes: the actor is the guard's, so no write is an actor_mismatch."""
    client, auth = env_sh_key
    response = client.post("/v1/setup/checks", headers=auth,
                           json={"project_id": PROJECT, "target": "repository"})
    assert response.status_code != 403 or "actor_mismatch" not in response.text, response.text
    assert response.status_code == 200, response.text
