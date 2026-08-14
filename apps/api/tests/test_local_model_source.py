"""Local OpenAI-compatible entitlement source routing."""
from __future__ import annotations

from types import SimpleNamespace

from apps.api.app.db import begin
from apps.api.app.services import openai_adapter
from apps.api.app.services.entitlements import EntitlementBrokerService
from apps.api.app.services.openai_adapter import (
    OpenAIAdapter,
    adapter_for_entitlement_source,
)
from apps.api.app.config import get_settings


PROJECT_ID = "boss-dogfood"
SOURCE_ID = "approved_local_model_host"
BASE_URL = "http://127.0.0.1:11434/v1"


def test_configure_local_source_routes_base_url_and_does_not_leak_secret(client, engine, monkeypatch):
    dummy = "sk-local-dummy-not-for-git"
    configured = client.post(
        f"/v1/entitlements/sources/{SOURCE_ID}/openai-compatible",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "base_url": BASE_URL,
        },
    )
    assert configured.status_code == 200, configured.text
    body = configured.json()
    dumped = str(body)
    assert dummy not in dumped
    assert body["endpoint"]["base_url"] == BASE_URL
    assert body["endpoint"]["openai_compatible"] is True
    assert body["adapter"]["base_url"] == BASE_URL
    assert body["adapter"]["receives_source_base_url"] is True
    assert body["effective_status"] == "active"
    assert "api_key" not in (body.get("connection_metadata") or {})

    listed = client.get("/v1/entitlements/sources", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200
    dumped_list = str(listed.json())
    assert dummy not in dumped_list
    source = next(
        item for item in listed.json()["items"] if item["source_id"] == SOURCE_ID
    )
    meta = source.get("connection_metadata") or {}
    if meta:
        assert meta.get("base_url") == BASE_URL

    captured: dict = {}

    class _Capture:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            msg = SimpleNamespace(
                content="ok",
                tool_calls=None,
                model_dump=lambda exclude_none=True: {},
            )
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: SimpleNamespace(
                        id="r1",
                        choices=[SimpleNamespace(message=msg)],
                        usage=SimpleNamespace(
                            prompt_tokens=1, completion_tokens=1, total_tokens=2
                        ),
                    )
                )
            )

    monkeypatch.setattr(openai_adapter, "OpenAI", _Capture)
    settings = get_settings()
    with begin(engine) as connection:
        row = EntitlementBrokerService(settings).get_source(
            connection, PROJECT_ID, SOURCE_ID
        )
    adapter = adapter_for_entitlement_source(settings, row, api_key="local")
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert captured["base_url"] == BASE_URL
    assert captured.get("api_key") == "local"


def test_configure_rejects_embedded_credentials(client):
    response = client.post(
        f"/v1/entitlements/sources/{SOURCE_ID}/openai-compatible",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "base_url": "http://user:secret@127.0.0.1:11434/v1",
        },
    )
    assert response.status_code == 422
    assert "credential" in response.json()["detail"].lower()


def test_configure_rejects_non_local_source_kind(client):
    response = client.post(
        "/v1/entitlements/sources/openai_api/openai-compatible",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "base_url": BASE_URL,
        },
    )
    assert response.status_code == 422


def test_adapter_override_does_not_use_settings_base_url(monkeypatch):
    class _Capture:
        last = {}

        def __init__(self, **kwargs):
            _Capture.last = kwargs
            msg = SimpleNamespace(
                content="ok", tool_calls=None, model_dump=lambda exclude_none=True: {}
            )
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: SimpleNamespace(
                        id="r1",
                        choices=[SimpleNamespace(message=msg)],
                        usage=SimpleNamespace(
                            prompt_tokens=1, completion_tokens=1, total_tokens=2
                        ),
                    )
                )
            )

    monkeypatch.setattr(openai_adapter, "OpenAI", _Capture)
    settings = SimpleNamespace(
        openai_api_key="sk-platform",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-test",
        azure_openai_key=None,
        azure_openai_endpoint=None,
        azure_openai_deployment=None,
        native_max_output_tokens=0,
        code_agent_engine="",
    )
    adapter = OpenAIAdapter(settings, base_url=BASE_URL, api_key="local")
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert _Capture.last["base_url"] == BASE_URL
    assert _Capture.last["api_key"] == "local"
