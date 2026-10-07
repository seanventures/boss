"""GET /v1/identity/delegations/{id}: one grant, in exactly the shape the lists answer, for its grantor
or a reader of every grant; 404 for anyone else, another org's grant, and an unknown id. The console's
delegation page (/identity/agents/delegations/{id}) reads it instead of scanning a 500-row list."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._identity_agents_support import add_person, build_env, session_ctx


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


@pytest.fixture()
def api(env, monkeypatch):
    from apps.api.app.routers import identity_agents

    monkeypatch.setattr(identity_agents, "_engine", lambda: env.engine)
    monkeypatch.setattr(identity_agents, "_ctx", lambda request: session_ctx(request.headers.get("x-test-as", env.alice)))
    app = FastAPI()
    app.include_router(identity_agents.build_identity_agents_router())
    return TestClient(app, follow_redirects=False)


def test_one_grant_is_the_lists_entry_for_its_grantor_and_a_reader_of_all(env, api):
    g = env.grant(repos=None)
    one = api.get(f"/v1/identity/delegations/{g.id}")
    assert one.status_code == 200
    listed = next(d for d in api.get("/v1/identity/me/delegations").json()["delegations"] if d["id"] == g.id)
    assert one.json() == listed                         # the same helper: new fields flow through both
    with env.db() as c:
        add_person(c, env.bob)
    assert api.get(f"/v1/identity/delegations/{g.id}", headers={"x-test-as": env.bob}).status_code == 404
    env.world.grant(env.bob, {"delegation:read_all"})
    assert api.get(f"/v1/identity/delegations/{g.id}", headers={"x-test-as": env.bob}).json()["id"] == g.id


def test_an_unknown_or_foreign_grant_is_not_found(env, api):
    assert api.get("/v1/identity/delegations/dlg_nope").json() == {"error": "not_found"}
    g = env.grant(repos=None)
    # Mallory is in another org: Alice's grant does not exist for her.
    assert api.get(f"/v1/identity/delegations/{g.id}", headers={"x-test-as": "usr_mallory"}).status_code == 404
