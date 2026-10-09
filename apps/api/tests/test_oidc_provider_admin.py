"""BOSS as an OIDC provider: the admin API (F6 §6.5, stream S6), under enforce, on the real app.

Pools need `oidc:admin` (Owner/Admin, step-up); clients need `oidc_client:manage` at the app's
project (Operators on their project); a client secret is shown once and never again; only an
app sign-in admin marks a client first-party; nothing registered makes BOSS fetch a URL.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import main
from apps.api.tests.oidc_world import LOGOUT, REDIRECT, make_world


@pytest.fixture
def w(engine, monkeypatch):
    return make_world(engine, monkeypatch)


@pytest.fixture
def api():
    return TestClient(main.app, raise_server_exceptions=False)


def _body(w, **over):
    body = {"project_id": w.ids.project_a, "app_key": "bank", "pool_id": w.pool["id"], "name": "Bank",
            "redirect_uris": [REDIRECT], "post_logout_redirect_uris": [LOGOUT],
            "allowed_scopes": ["openid", "profile", "email", "roles", "offline_access"]}
    body.update(over)
    return body


def test_an_owner_manages_pools_and_an_operator_cannot(w, api):
    owner, op = w.ids.person("own"), w.ids.person("op")
    w.ids.bind(owner, "owner")
    w.ids.bind(op, "operator")
    with w.ids.acting_as(w.ids.ctx(owner)):
        r = api.post("/v1/oidc/pools", json={"name": "Customers", "kind": "customer", "signup": "open", "mfa": "required"})
        assert r.status_code == 201, r.text
        pool = r.json()
        assert api.get("/v1/oidc/pools").json()["pools"][-1]["id"] == pool["id"]
        assert api.patch(f"/v1/oidc/pools/{pool['id']}", json={"kind": "workforce"}).status_code == 400
        assert api.patch(f"/v1/oidc/pools/{pool['id']}", json={"mfa": "optional"}).json()["mfa"] == "optional"
        assert api.post("/v1/oidc/pools", json={"name": "W", "kind": "workforce", "signup": "open"}).status_code == 400
        assert api.delete(f"/v1/oidc/pools/{w.pool['id']}").status_code in (200, 400)
    with w.ids.acting_as(w.ids.ctx(op)):
        assert api.post("/v1/oidc/pools", json={"name": "x"}).status_code == 403
        assert api.get("/v1/oidc/pools").status_code == 403
    with w.ids.acting_as(w.ids.ctx(owner, mfa=False)):
        assert api.post("/v1/oidc/pools", json={"name": "y"}).status_code in (401, 403)  # step-up


def test_a_client_secret_is_shown_once_and_never_again(w, api):
    owner = w.ids.person("own")
    w.ids.bind(owner, "owner")
    with w.ids.acting_as(w.ids.ctx(owner)):
        r = api.post("/v1/oidc/clients", json=_body(w))
        assert r.status_code == 201, r.text
        created = r.json()
        secret = created["client_secret"]
        assert len(secret) >= 43 and created["has_secret"] is True
        got = api.get(f"/v1/oidc/clients/{created['client_id']}")
        assert got.status_code == 200 and secret not in got.text and "client_secret" not in got.json()
        listed = api.get("/v1/oidc/clients", params={"project_id": w.ids.project_a})
        assert secret not in listed.text and listed.json()["clients"][0]["client_id"] == created["client_id"]
        rotated = api.post(f"/v1/oidc/clients/{created['client_id']}/rotate-secret", json={})
        assert rotated.status_code == 200 and rotated.json()["client_secret"] != secret
        assert rotated.headers["cache-control"] == "no-store"


def test_an_operator_registers_clients_on_their_project_only(w, api):
    op = w.ids.person("op")
    w.ids.bind(op, "operator", project=w.ids.project_a)
    viewer = w.ids.person("viewer")
    w.ids.bind(viewer, "viewer", project=w.ids.project_a)
    with w.ids.acting_as(w.ids.ctx(op)):
        assert api.post("/v1/oidc/clients", json=_body(w)).status_code == 201
        assert api.post("/v1/oidc/clients", json=_body(w, project_id=w.ids.project_b)).status_code in (403, 404)
        assert api.post("/v1/oidc/clients", json=_body(w, first_party=True)).status_code == 403
    with w.ids.acting_as(w.ids.ctx(viewer)):
        assert api.post("/v1/oidc/clients", json=_body(w)).status_code == 403


@pytest.mark.parametrize("extra", [{"jwks_uri": "https://evil.test/jwks"}, {"request_uris": ["https://evil.test"]},
                                   {"sector_identifier_uri": "https://evil.test"}, {"client_secret": "mine"}])
def test_registration_never_takes_a_url_to_fetch_or_a_chosen_secret(w, api, extra):
    owner = w.ids.person("own")
    w.ids.bind(owner, "owner")
    with w.ids.acting_as(w.ids.ctx(owner)):
        assert api.post("/v1/oidc/clients", json=_body(w, **extra)).status_code == 422


def test_bad_registrations_are_400_with_a_reason(w, api):
    owner = w.ids.person("own")
    w.ids.bind(owner, "owner")
    with w.ids.acting_as(w.ids.ctx(owner)):
        r = api.post("/v1/oidc/clients", json=_body(w, redirect_uris=["https://*.evil.test/cb"]))
        assert r.status_code == 400 and "redirect_uri" in r.json()["detail"]
        assert api.post("/v1/oidc/clients", json=_body(w, pool_id="opl_nope")).status_code == 404


def test_roles_reach_the_token_and_consent_revoke_ends_refresh(w, api):
    owner = w.ids.person("own")
    w.ids.bind(owner, "owner")
    alice = w.person("alice")
    with w.ids.acting_as(w.ids.ctx(owner)):
        created = api.post("/v1/oidc/clients", json=_body(w)).json()
        cid = created["client_id"]
        assert api.post(f"/v1/oidc/clients/{cid}/roles", json={"role_key": "teller", "name": "Teller"}).status_code == 201
        assert api.post(f"/v1/oidc/clients/{cid}/role-assignments",
                        json={"subject_ref": alice.principal.id, "role_key": "teller"}).status_code == 200
        assert api.post(f"/v1/oidc/clients/{cid}/role-assignments",
                        json={"subject_ref": alice.principal.id, "role_key": "nope"}).status_code == 404
        assert api.post(f"/v1/oidc/clients/{cid}/role-assignments",
                        json={"subject_ref": "usr_nobody", "role_key": "teller"}).status_code == 404
    client = {"id": cid}
    code, verifier, _ = w.sign_in(client, alice)
    tok = w.exchange(client, created["client_secret"], code, verifier).json()
    assert w.userinfo(tok["access_token"]).json()["roles"] == ["teller"]
    with w.ids.acting_as(w.ids.ctx(owner)):
        consents = api.get("/v1/oidc/consents", params={"client_id": cid}).json()["consents"]
        assert [c["subject_ref"] for c in consents] == [alice.principal.id]
        assert api.post("/v1/oidc/consents/revoke", json={"client_id": cid, "subject_ref": alice.principal.id}).status_code == 200
    r = w.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=client,
                secret=created["client_secret"])
    assert r.json()["error"] == "invalid_grant"
    assert w.userinfo(tok["access_token"]).status_code == 401


def test_deleting_a_client_ends_its_tokens(w, api):
    owner = w.ids.person("own")
    w.ids.bind(owner, "owner")
    alice = w.person("alice")
    with w.ids.acting_as(w.ids.ctx(owner)):
        created = api.post("/v1/oidc/clients", json=_body(w)).json()
    client = {"id": created["client_id"]}
    code, verifier, _ = w.sign_in(client, alice)
    tok = w.exchange(client, created["client_secret"], code, verifier).json()
    with w.ids.acting_as(w.ids.ctx(owner)):
        assert api.delete(f"/v1/oidc/clients/{created['client_id']}").status_code == 200
        assert api.get(f"/v1/oidc/clients/{created['client_id']}").status_code == 404
    assert w.userinfo(tok["access_token"]).status_code == 401


def test_customer_pool_users_are_phase_2(w, api):
    owner = w.ids.person("own")
    w.ids.bind(owner, "owner")
    with w.ids.acting_as(w.ids.ctx(owner)):
        assert api.get(f"/v1/oidc/pools/{w.pool['id']}/users").status_code == 501
