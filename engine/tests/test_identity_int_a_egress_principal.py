"""int-a · The egress service is a principal: `sys_egress` with a `boss_sak_` key of its own (A40).

/v1/identity/introspect accepts the egress service as `sys_egress` (a live boss_sak_ key, checked
live on every call) or, as before, the legacy BOSS_EGRESS_SERVICE_KEY shared key. Any other key, a
revoked key, a suspended sys_egress, or a bad bearer next to a good legacy key is refused. Each test
fails if the protection it names is removed.
"""
from __future__ import annotations

import sys

import pytest
from sqlalchemy import text

from boss_engine.identity import tokens
from boss_engine.identity.delegation import AUD_API
from boss_engine.identity.permissions_gen import SYSTEM_PRINCIPALS

from ._identity_agents_support import ROOT, build_env, start

sys.path.insert(0, str(ROOT))

LEGACY = "egress-service-key-for-tests"


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


@pytest.fixture()
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import identity_agents

    monkeypatch.setattr(identity_agents, "_engine", lambda: env.engine)
    monkeypatch.setenv("BOSS_EGRESS_SERVICE_KEY", LEGACY)
    app = FastAPI()
    app.include_router(identity_agents.build_identity_agents_router())
    return TestClient(app)


def _key(env, **kw):
    with env.db() as c:
        return tokens.issue_egress_service_key(c, org_id="org_boss", **kw).key


def test_sys_egress_is_a_catalogue_system_principal_holding_nothing():
    meta = SYSTEM_PRINCIPALS["sys_egress"]
    assert meta.access_role == "none"


def test_introspection_accepts_the_egress_principals_service_key_and_checks_it_live(env, client):
    key = _key(env)
    assert key.startswith("boss_sak_")
    with env.db() as c:
        stored = c.execute(text("SELECT principal_id, kind, token_hash FROM identity_api_credentials")).one()
        assert stored.principal_id == "sys_egress" and stored.kind == "sak" and key not in stored.token_hash
    tok = start(env, env.grant().id).tokens[AUD_API].access_token
    ok = client.post("/v1/identity/introspect", data={"token": tok}, headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200 and ok.json()["active"] is True and ok.json()["sub"] == env.alice
    # The legacy shared key still works, alone.
    assert client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"x-boss-egress-key": LEGACY}).json()["active"] is True
    # A bad bearer DECIDES: a good legacy key next to it does not rescue it.
    assert client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"Authorization": "Bearer boss_sak_" + "x" * 49,
                                "x-boss-egress-key": LEGACY}).status_code == 401
    # Suspend sys_egress: its key stops at once.
    with env.db() as c:
        c.execute(text("UPDATE identity_principals SET status = 'suspended' WHERE id = 'sys_egress'"))
    assert client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"Authorization": f"Bearer {key}"}).status_code == 401
    with env.db() as c:
        c.execute(text("UPDATE identity_principals SET status = 'active' WHERE id = 'sys_egress'"))
    # Rotate without overlap: the old key is dead, the new one works.
    new = _key(env, revoke_others=True)
    assert client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_a_service_key_of_any_other_principal_is_refused(env, client):
    from boss_engine.identity.authn import hash_token, mint_api_key

    other = mint_api_key("sak")
    pat = mint_api_key("pat")
    with env.db() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES ('sva_ci', 'org_boss', 'service_account', 'active', 'CI')"))
        for kid, owner, kind, raw in (("key_ci", "sva_ci", "sak", other), ("key_pat", env.alice, "pat", pat)):
            c.execute(text("INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
                           "created_by) VALUES (:i, :p, :k, 'x', :h, '{}', 'test')"),
                      {"i": kid, "p": owner, "k": kind, "h": hash_token(raw)})
    tok = start(env, env.grant().id).tokens[AUD_API].access_token
    for raw in (other, pat):
        assert client.post("/v1/identity/introspect", data={"token": tok},
                           headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    with env.db() as c:
        assert tokens.egress_caller_ok("Basic abc", LEGACY, db=c) is False     # another scheme never falls back
        assert tokens.egress_caller_ok(None, None, db=c) is False
