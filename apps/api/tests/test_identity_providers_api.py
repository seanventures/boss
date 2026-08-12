"""E2E TestClient coverage for IdP product packs (no external network)."""

from __future__ import annotations

from boss_engine.services.identity_provider_catalog import (
    IDENTITY_PROVIDER_CATALOG,
    materialize_blueprint,
)


PROJECT_ID = "boss-dogfood"
EXPECTED_IDS = [
    "okta",
    "entra_id",
    "google_workspace",
    "auth0",
    "keycloak",
    "github",
    "gitlab",
    "onelogin",
    "jumpcloud",
    "ping",
]


def test_catalog_lists_top_10_idps(client):
    response = client.get("/v1/identity-providers")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 10
    assert body["product"] == "boss_identity_providers"
    ids = [item["id"] for item in body["items"]]
    assert ids == EXPECTED_IDS
    for item in body["items"]:
        assert item["status"] in ("available", "planned")
        assert "oidc" in item["protocol_support"] or "saml" in item["protocol_support"]
        assert item["stars"] >= 4.0
        assert item["docs_url"].startswith("https://")
        # No secrets in catalog list
        assert "client_secret" not in item
        assert "secret" not in item or item.get("secret") is None
    assert "OpenID Connect 1.0" in body["standards"]
    assert "SAML 2.0" in body["standards"]
    assert "SCIM 2.0" in body["standards"]


def test_catalog_trailing_slash(client):
    response = client.get("/v1/identity-providers/")
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 10


def test_get_okta_detail_includes_blueprint_template(client):
    response = client.get("/v1/identity-providers/okta")
    assert response.status_code == 200, response.text
    body = response.json()
    idp = body["idp"]
    assert idp["id"] == "okta"
    assert idp["name"] == "Okta"
    assert "oidc" in idp["protocol_support"]
    assert "saml" in idp["protocol_support"]
    assert "scim" in idp["protocol_support"]
    assert len(idp["setup_steps"]) >= 3
    assert "sub" in idp["default_oidc_claim_map"]
    assert "issuer" in idp["metadata_url_templates"]
    tpl = body["blueprint_template"]
    assert tpl["method"] == "POST"
    assert "domain" in tpl["required_fields"]
    assert "client_secret" not in (tpl["example_body"].get("variables") or {})


def test_get_unknown_idp_404(client):
    response = client.get("/v1/identity-providers/not-a-real-idp")
    assert response.status_code == 404
    assert "Unknown" in response.json()["detail"]


def test_blueprint_okta_dry_run(client):
    response = client.post(
        "/v1/identity-providers/okta/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {
                "domain": "dev-123.okta.com",
                "client_id": "0oa_example",
                "audience": "boss-api",
                "secret_ref": "secretref:okta-client-secret",
            },
            "provider_type": "oidc",
            "actor": "ceo",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["idp_id"] == "okta"
    assert body["project_id"] == PROJECT_ID
    assert body["provider_type"] == "oidc"
    assert "dev-123.okta.com" in body["rendered_endpoints"]["issuer"]
    payload = body["enterprise_payload"]
    assert payload["provider_key"] == "okta"
    assert payload["provider_type"] == "oidc"
    assert payload["status"] == "planned"
    assert payload["secret_ref"] == "secretref:okta-client-secret"
    assert payload["issuer"].startswith("https://dev-123.okta.com")
    # Fail closed: no raw secrets
    blob = response.text.lower()
    assert "client_secret_value" not in blob
    assert "password" not in payload
    assert body["enterprise_upsert_path"] == "/v1/enterprise/identity-providers"


def test_blueprint_rejects_raw_secrets(client):
    response = client.post(
        "/v1/identity-providers/okta/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {
                "domain": "dev-123.okta.com",
                "client_id": "0oa_example",
                "client_secret": "super-secret-value",
            },
        },
    )
    assert response.status_code == 422
    assert "secret" in response.json()["detail"].lower()


def test_blueprint_rejects_apply_true(client):
    response = client.post(
        "/v1/identity-providers/okta/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {
                "domain": "dev-123.okta.com",
                "client_id": "0oa_example",
            },
            "apply": True,
        },
    )
    assert response.status_code == 422
    assert "apply" in response.json()["detail"].lower()


def test_blueprint_missing_required_fields(client):
    response = client.post(
        "/v1/identity-providers/okta/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {"client_id": "only-client"},
        },
    )
    assert response.status_code == 422
    assert "domain" in response.json()["detail"]


def test_blueprint_entra_id(client):
    response = client.post(
        "/v1/identity-providers/entra_id/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {
                "tenant_id": "11111111-2222-3333-4444-555555555555",
                "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "11111111-2222-3333-4444-555555555555" in body["rendered_endpoints"]["issuer"]
    assert body["enterprise_payload"]["provider_key"] == "entra_id"


def test_blueprint_unsupported_protocol(client):
    response = client.post(
        "/v1/identity-providers/github/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {"client_id": "Iv1.example"},
            "provider_type": "saml",
        },
    )
    assert response.status_code == 422
    assert "not supported" in response.json()["detail"].lower()


def test_project_status_empty_then_configured(client):
    empty = client.get(
        "/v1/identity-providers/project-status",
        params={"project_id": PROJECT_ID},
    )
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["project_id"] == PROJECT_ID
    assert body["catalog_count"] == 10
    assert body["configured_count"] == 0
    assert all(not item["configured"] for item in body["items"])

    # Register via existing enterprise API (dry-run blueprint materialization path)
    upsert = client.post(
        "/v1/enterprise/identity-providers",
        json={
            "project_id": PROJECT_ID,
            "provider_key": "okta",
            "provider_type": "oidc",
            "display_name": "Okta Dogfood",
            "issuer": "https://dev-123.okta.com/oauth2/default",
            "jwks_url": "https://dev-123.okta.com/oauth2/default/v1/keys",
            "audience": "boss-api",
            "secret_ref": "secretref:okta-client-secret",
            "status": "planned",
            "actor": "ceo",
        },
    )
    assert upsert.status_code == 201, upsert.text

    status = client.get(
        "/v1/identity-providers/project-status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["configured_count"] >= 1
    okta = next(item for item in payload["items"] if item["idp_id"] == "okta")
    assert okta["configured"] is True
    assert okta["configurations"][0]["provider_key"] == "okta"
    assert okta["configurations"][0]["secret_ref"] == "secretref:okta-client-secret"
    # Ensure response does not contain raw secret material
    assert "super-secret" not in status.text


def test_catalog_module_has_exactly_ten_entries():
    assert len(IDENTITY_PROVIDER_CATALOG) == 10
    assert {e["id"] for e in IDENTITY_PROVIDER_CATALOG} == set(EXPECTED_IDS)


def test_materialize_blueprint_unit_no_network():
    blueprint = materialize_blueprint(
        "keycloak",
        project_id=PROJECT_ID,
        variables={
            "base_url": "https://keycloak.example",
            "realm": "boss",
            "client_id": "boss-console",
        },
    )
    assert blueprint["dry_run"] is True
    assert "https://keycloak.example/realms/boss" == blueprint["rendered_endpoints"]["issuer"]
    assert blueprint["enterprise_payload"]["provider_key"] == "keycloak"
