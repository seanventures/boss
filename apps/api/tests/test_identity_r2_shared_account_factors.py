"""R2: the shared console password's session changing "its" password answered 500 (an assert on a
user row the shared principal does not have). Every self-service factor change of a principal with
no account of its own is refused with words."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.mark.parametrize("method, path, body", [
    ("POST", "/v1/identity/me/password", {"current": "x", "new": "a-new-long-password-123"}),
    ("POST", "/v1/identity/me/mfa/totp", {}),
])
def test_a_shared_principal_is_told_it_has_no_factors_of_its_own(world, method, path, body):
    shared = world.person("shared-console-like")        # a principal with no identity_users row
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(shared)):
        got = client.request(method, path, json=body, headers={"sec-fetch-site": "same-origin"})
    assert got.status_code == 409, got.text
    assert got.json()["error"] == "shared_account" and "own" in got.json()["message"]


def test_a_shared_principal_reads_a_whole_profile(world):
    """Round 3 upgrade walk: GET /v1/identity/me/profile answered {} for a principal with no
    account row, and My account crashed on the missing name. It is always the full shape."""
    shared = world.person("Shared console password")
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(shared)):
        got = client.get("/v1/identity/me/profile")
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == {"display_name", "email", "avatar_seed"}
    assert body["display_name"] == "Shared console password" and body["email"] is None
