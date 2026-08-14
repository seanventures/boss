"""Community BYOK paste-and-connect — write-through, never return the raw key."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text


PROJECT_ID = "boss-dogfood"
PASTE_VALUE = "sk-test-easy-connect-never-leak-xyz"


@pytest.fixture(autouse=True)
def _ephemeral_local_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    monkeypatch.setenv("BOSS_KMS_VAULT_DIR", str(tmp_path / "easy-connect-vault"))
    yield


def _paste(client, **overrides):
    payload = {
        "project_id": PROJECT_ID,
        "actor": "ceo",
        "provider": "openai",
        "account_label": "openai-paste",
        "secret_value": PASTE_VALUE,
        "backend": "local_vault",
        "read_only": True,
    }
    payload.update(overrides)
    return client.post("/v1/providers/easy-connect", json=payload)


def test_catalog_lists_openai_and_anthropic(client):
    response = client.get("/v1/provider-definitions")
    assert response.status_code == 200, response.text
    definitions = {item["provider"]: item for item in response.json()["items"]}
    assert "openai" in definitions
    assert "anthropic" in definitions
    assert "openai_compatible" in definitions
    assert "cloudflare" in definitions
    assert "supabase" in definitions
    assert definitions["openai"]["adapter_status"] == "available"
    assert definitions["anthropic"]["adapter_status"] == "available"
    assert definitions["gcp"]["adapter_status"] == "planned"


def test_community_edition_mounts_easy_connect(client):
    """conftest mounts the Community edition; the route must not be paid-gated."""
    missing = client.post("/v1/providers/easy-connect", json={})
    assert missing.status_code != 404
    assert missing.status_code == 422


def test_paste_key_creates_pointer_only_account(client):
    created = _paste(client)
    assert created.status_code == 201, created.text
    body = created.json()
    dumped = json.dumps(body)
    assert PASTE_VALUE not in dumped
    assert "sk-test-easy-connect-never-leak-xyz" not in dumped
    assert body["raw_secret_stored"] is False
    assert body["secret"]["raw_secret_stored"] is False
    assert body["secret"]["last4"] == PASTE_VALUE[-4:]
    assert body["secret"]["secret_ref_id"]
    assert "value" not in body
    assert "secret_value" not in body
    account = body["provider_account"]
    assert account["provider"] == "openai"
    assert account["account_label"] == "openai-paste"
    assert account["credential_refs"]
    assert all(str(pointer).startswith("local_vault:") for pointer in account["credential_refs"].values())
    listed = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    match = next(item for item in items if item["id"] == account["id"])
    assert match["credential_refs"] == account["credential_refs"]
    assert PASTE_VALUE not in json.dumps(listed.json())


def test_empty_secret_is_rejected(client):
    created = _paste(client, secret_value="")
    assert created.status_code == 422
    assert PASTE_VALUE not in created.text
    blank = _paste(client, secret_value="   ", account_label="openai-blank")
    assert blank.status_code == 422


def test_response_never_contains_raw_value(client):
    created = _paste(client, account_label="openai-redact")
    assert created.status_code == 201, created.text
    raw = created.content.decode("utf-8")
    assert PASTE_VALUE not in raw
    listed = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert PASTE_VALUE not in listed.content.decode("utf-8")
    events = client.get("/v1/events", params={"project_id": PROJECT_ID, "limit": 50})
    if events.status_code == 200:
        assert PASTE_VALUE not in events.content.decode("utf-8")


def test_openai_compatible_stores_base_url_in_connection_metadata(client):
    created = _paste(
        client,
        provider="openai_compatible",
        account_label="ollama-local",
        base_url="http://127.0.0.1:11434/v1",
    )
    assert created.status_code == 201, created.text
    account = created.json()["provider_account"]
    assert account["provider"] == "openai_compatible"
    assert account["connection_metadata"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert PASTE_VALUE not in json.dumps(created.json())
    listed = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    match = next(item for item in listed.json()["items"] if item["id"] == account["id"])
    assert match["connection_metadata"]["base_url"] == "http://127.0.0.1:11434/v1"


def test_easy_connected_event_has_no_secret_material(client, engine):
    created = _paste(client, account_label="openai-event")
    assert created.status_code == 201, created.text
    account_id = created.json()["provider_account"]["id"]
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT data
                FROM events
                WHERE project_id = :project_id
                  AND event_type = 'provider.easy_connected'
                  AND entity_id = :account_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID, "account_id": account_id},
        ).mappings().first()
    assert row is not None
    payload = row["data"]
    assert PASTE_VALUE not in json.dumps(payload)
    assert payload["raw_secret_stored"] is False
    assert payload["last4"] == PASTE_VALUE[-4:]
