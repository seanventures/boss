"""A saved Gemini or xAI key must reach its own coding lane, never OpenAI's key."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import credential_resolve, secret_backends
from boss_engine.services.code_agent import _bind_lane_credentials, _build_code_agent
from boss_engine.services.setup_service import connected_engines, model_key_details, provider_catalogue


@pytest.mark.parametrize(
    ("provider", "endpoint", "model"),
    [
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-3.8-flash"),
        ("xai", "https://api.x.ai/v1/chat/completions", "grok-4.7"),
    ],
)
def test_setup_catalogue_gives_a_real_chat_endpoint(provider, endpoint, model):
    offered = {entry["provider"]: entry for entry in provider_catalogue()}
    assert offered[provider]["kind"] == "api_key"
    details = model_key_details(provider=provider)
    assert details["endpoint"] == endpoint
    assert details["model"] == model


@pytest.mark.parametrize(
    ("lane", "key_field", "base_field"),
    [
        ("gemini", "google_api_key", "gemini_base_url"),
        ("xai", "xai_api_key", "xai_base_url"),
    ],
)
def test_env_key_binds_only_the_selected_provider(
    monkeypatch, entitlement_tables, lane, key_field, base_field,
):
    requested = []

    def resolve(connection, settings, *, project_id, provider, env_fallback_attr):
        requested.append((project_id, provider, env_fallback_attr))
        return f"{lane}-project-key"

    monkeypatch.setattr(credential_resolve, "resolve_project_credential", resolve)
    settings = Settings(_env_file=None).model_copy(update={
        "openai_api_key": "another-provider-key",
        "azure_openai_key": "another-provider-key",
        "azure_openai_endpoint": "https://azure.example.invalid",
        "azure_openai_deployment": "other",
    })
    db = create_engine("sqlite+pysqlite:///:memory:")
    with db.begin() as connection:
        entitlement_tables(connection, with_settings=False)
        bound = _bind_lane_credentials(settings, lane, connection=connection, project_id="p")
    agent = _build_code_agent(bound, lane)

    assert requested == [("p", lane, key_field)]
    assert getattr(bound, key_field) == f"{lane}-project-key"
    assert agent.settings.openai_api_key == f"{lane}-project-key"
    assert agent.settings.openai_base_url == getattr(settings, base_field)
    assert agent.settings.azure_openai_key is None


@pytest.mark.parametrize(("lane", "key_field"), [
    ("gemini", "google_api_key"), ("xai", "xai_api_key"),
])
def test_saved_key_model_and_url_remain_one_binding(
    monkeypatch, entitlement_tables, lane, key_field,
):
    monkeypatch.setattr(
        secret_backends, "resolve_secret_ref",
        lambda connection, ref, settings: f"secret-for-{ref}",
    )
    db = create_engine("sqlite+pysqlite:///:memory:")
    with db.begin() as connection:
        entitlement_tables(connection, with_settings=False)
        connection.execute(text(
            "INSERT INTO entitlement_sources (project_id, source_id, provider, "
            "connection_status, credential_secret_ref_id, supported_models, "
            "connection_metadata, updated_at) VALUES "
            "('p', :source, :provider, 'not_connected', 'ref-1', :models, :metadata, :at)"
        ), {"source": f"setup-{lane}", "provider": lane,
            "models": f'["{lane}-chosen-model"]',
            "metadata": '{"base_url":"https://selected.example.invalid/v1"}',
            "at": datetime(2026, 9, 28, tzinfo=timezone.utc)})
        settings = Settings(_env_file=None).model_copy(update={
            "openai_api_key": "wrong-provider", "openai_model": "wrong-model",
        })
        bound = _bind_lane_credentials(settings, lane, connection=connection, project_id="p")
    agent = _build_code_agent(bound, lane)
    assert getattr(bound, key_field) == "secret-for-ref-1"
    assert agent.settings.openai_api_key == "secret-for-ref-1"
    assert agent.settings.openai_model == f"{lane}-chosen-model"
    assert agent.settings.openai_base_url == "https://selected.example.invalid/v1"


def test_custom_compatible_source_drives_native_with_its_own_key(entitlement_tables, monkeypatch):
    monkeypatch.setattr(
        secret_backends, "resolve_secret_ref",
        lambda connection, ref, settings: "custom-project-key",
    )
    db = create_engine("sqlite+pysqlite:///:memory:")
    with db.begin() as connection:
        entitlement_tables(connection, with_settings=False)
        connection.execute(text(
            "INSERT INTO entitlement_sources (project_id, source_id, provider, "
            "connection_status, credential_secret_ref_id, supported_models, "
            "connection_metadata, updated_at) VALUES "
            "('p', 'setup-custom', 'custom', 'not_connected', 'ref-custom', "
            "'[\"my-local-model\"]', '{\"base_url\":\"http://model.local:1234/v1\"}', :at)"
        ), {"at": datetime(2026, 9, 28, tzinfo=timezone.utc)})
        settings = Settings(_env_file=None).model_copy(update={
            "openai_api_key": "wrong-provider", "openai_model": "wrong-model",
        })
        bound = _bind_lane_credentials(settings, "native", connection=connection, project_id="p")
    assert bound.openai_api_key == "custom-project-key"
    assert bound.openai_model == "my-local-model"
    assert bound.openai_base_url == "http://model.local:1234/v1"


@pytest.mark.parametrize(("source_id", "lane"), [
    ("setup-gemini", "gemini"), ("setup-xai", "xai"),
])
def test_a_saved_key_is_offered_to_roles(entitlement_tables, source_id, lane):
    db = create_engine("sqlite+pysqlite:///:memory:")
    with db.begin() as connection:
        entitlement_tables(connection, with_settings=False)
        connection.execute(text(
            "INSERT INTO entitlement_sources (project_id, source_id, provider, "
            "connection_status, credential_secret_ref_id, updated_at) "
            "VALUES ('p', :source, :provider, 'not_connected', 'secret-ref', :at)"
        ), {"source": source_id, "provider": lane,
            "at": datetime(2026, 9, 28, tzinfo=timezone.utc)})
        settings = Settings(_env_file=None).model_copy(update={
            "openai_api_key": None, "google_api_key": None, "xai_api_key": None,
        })
        assert lane in connected_engines(connection, settings, "p")
