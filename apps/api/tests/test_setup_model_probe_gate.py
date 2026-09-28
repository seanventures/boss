"""First-run Community model setup stores keys without ungoverned provider calls."""

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import secret_backends, setup_service


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_setup_model_saves_unverified_key_without_provider_network(
    client, engine, monkeypatch, edition,
):
    monkeypatch.setattr(api_main.settings, "boss_edition", edition)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "ceo-key:ceo")
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda _settings: "database")
    network_calls = []

    def forbidden_network(*_args, **_kwargs):
        network_calls.append("provider")
        raise AssertionError("Community setup sent a provider request")

    monkeypatch.setattr(setup_service.urllib.request, "build_opener", forbidden_network)
    response = client.post(
        "/v1/setup/model",
        headers={"Authorization": "Bearer ceo-key"},
        json={"project_id": "boss-dogfood", "actor": "ceo", "provider": "openai",
              "api_key": "test-secret-value", "model": "test-model"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["verified"] is False
    assert response.json()["verification_state"] == "unverified"
    assert "test-secret-value" not in response.text
    assert network_calls == []
    with engine.begin() as connection:
        source = connection.execute(
            text("SELECT connection_metadata, credential_secret_ref_id "
                 "FROM entitlement_sources WHERE project_id='boss-dogfood' "
                 "AND source_id='setup-openai'")
        ).mappings().one()
        assert source["connection_metadata"]["verification_state"] == "unverified"
        assert source["credential_secret_ref_id"]


def test_paid_setup_retains_live_probe_before_storage(client, engine, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise_self_hosted")
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "ceo-key:ceo")
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda _settings: "database")
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "model": kwargs["model"],
                "endpoint": "https://api.openai.com/v1/chat/completions"}

    monkeypatch.setattr(setup_service, "verify_model_key", probe)
    response = client.post(
        "/v1/setup/model",
        headers={"Authorization": "Bearer ceo-key"},
        json={"project_id": "boss-dogfood", "actor": "ceo", "provider": "openai",
              "api_key": "test-secret-value", "model": "test-model"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["verified"] is True
    assert response.json()["verification_state"] == "verified"
    assert len(calls) == 1
    assert calls[0]["settings"] is api_main.settings
    with engine.begin() as connection:
        metadata = connection.execute(
            text("SELECT connection_metadata FROM entitlement_sources "
                 "WHERE project_id='boss-dogfood' AND source_id='setup-openai'")
        ).scalar_one()
    assert metadata["verification_state"] == "verified"


def test_community_setup_status_marks_environment_key_unverified_without_network(
    client, monkeypatch,
):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "ceo-key:ceo")
    monkeypatch.setattr(api_main.settings, "openai_api_key", "test-key-1234")
    monkeypatch.setattr(api_main.settings, "openai_base_url", "https://example.invalid/v1")
    monkeypatch.setattr(api_main.settings, "chat_model", "test-model")
    monkeypatch.setattr(
        setup_service, "check_github_for_project",
        lambda *_args: {"configured": False, "ok": False},
    )
    network_calls = []

    def forbidden_network(*_args, **_kwargs):
        network_calls.append("provider")
        raise AssertionError("Community status sent a provider request")

    monkeypatch.setattr(setup_service, "_probe", forbidden_network)
    response = client.get(
        "/v1/setup/status", headers={"Authorization": "Bearer ceo-key"},
        params={"project_id": "boss-dogfood"},
    )

    assert response.status_code == 200, response.text
    model = response.json()["environment"]["model"]
    assert model["configured"] is True
    assert model["verification_state"] == "unverified"
    assert model["ok"] is False
    assert "error" not in model
    assert network_calls == []
