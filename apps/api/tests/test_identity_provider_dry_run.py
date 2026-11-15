"""TEST A PROVIDER BEFORE ADDING IT (BOSSAPP-1048 console walk): POST /v1/identity/providers/test.

The Add form's Test is a dry run: the same discovery + signing-key read the saved provider's Test
does (OIDC), or the configuration check (SAML), and NOTHING is stored — no provider row, no vault
item, no secret read or kept. Before, Okta's Test appeared only after Add provider.
"""
from __future__ import annotations

# Fixtures are imported from their suites, then named as parameters.
# ruff: noqa: F811

from sqlalchemy import text

from apps.api.tests.identity_s1_world import world  # noqa: F401  (fixture)
from apps.api.tests.test_identity_okta_sso import okta, owner_client  # noqa: F401  (fixture, helper)


def _counts(engine) -> tuple[int, int]:
    with engine.begin() as db:
        return (db.execute(text("SELECT count(*) FROM identity_providers")).scalar(),
                db.execute(text("SELECT count(*) FROM identity_vault_items")).scalar())


def test_an_unsaved_okta_provider_is_tested_against_its_issuer_and_nothing_is_stored(world, engine, okta):
    client, h = owner_client(engine)
    before = _counts(engine)
    body = {"protocol": "oidc", "preset": "okta", "name": "Okta", "issuer": okta.issuer + "/",
            "client_id": okta.client_id, "client_secret": "never-kept-secret-value"}
    ok = client.post("/v1/identity/providers/test", json=body, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "checked": "discovery", "issuer": okta.issuer}
    assert "never-kept-secret-value" not in ok.text
    assert _counts(engine) == before                          # a dry run: no row, no vault item

    # The -admin host is refused with words, before any request leaves BOSS.
    admin = client.post("/v1/identity/providers/test", headers=h,
                        json={**body, "issuer": "https://acme-admin.okta.com"})
    assert admin.status_code == 200 and admin.json()["ok"] is False
    assert admin.json()["error"] == "invalid_provider" and "-admin" in admin.json()["detail"]

    # The test needs no client id or secret: discovery is public.
    bare = client.post("/v1/identity/providers/test", headers=h,
                       json={"protocol": "oidc", "preset": "okta", "name": "Okta", "issuer": okta.issuer})
    assert bare.status_code == 200 and bare.json()["ok"] is True


def test_an_unsaved_saml_provider_is_checked_for_an_https_url_and_a_certificate_that_parses(world, engine):
    client, h = owner_client(engine)
    base = {"protocol": "saml", "name": "SAML", "issuer": "urn:idp", "saml_sso_url": "https://idp.example/sso"}
    garbled = client.post("/v1/identity/providers/test", headers=h, json={**base, "saml_certificates": ["not a cert"]})
    assert garbled.status_code == 200 and garbled.json()["ok"] is False
    plain = client.post("/v1/identity/providers/test", headers=h,
                        json={**base, "saml_sso_url": "http://idp.example/sso", "saml_certificates": ["x"]})
    assert plain.json()["ok"] is False and "https" in plain.json()["detail"]


def test_the_dry_run_needs_the_permission_to_manage_providers(world, engine):
    from apps.api.tests.identity_s1_world import HEADERS, fresh_client, make_user, sign_in

    make_user(engine, "viewer@acme.dev", role="viewer")
    client = fresh_client()
    assert sign_in(client, "viewer@acme.dev").status_code == 200
    h = {**HEADERS, "X-Boss-CSRF": client.get("/v1/identity/me").json()["csrf_token"]}
    refused = client.post("/v1/identity/providers/test", headers=h,
                          json={"protocol": "oidc", "preset": "okta", "name": "Okta", "issuer": "https://acme.okta.com"})
    assert refused.status_code == 403, refused.text
