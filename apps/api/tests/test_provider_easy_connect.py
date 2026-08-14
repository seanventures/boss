"""Community BYOK paste-and-connect — write-through, never return the raw key."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from apps.api.app import main as api_main
from boss_engine.services import secret_backends as secret_backend_mod


PROJECT_ID = "boss-dogfood"
PASTE_VALUE = "sk-test-easy-connect-never-leak-xyz"
REPLACEMENT_VALUE = "sk-test-easy-connect-rotated-never-leak"
SHORT_VALUE = "xyzwv"


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


def _assert_no_raw_secret(payload, *secrets: str) -> None:
    dumped = payload if isinstance(payload, str) else json.dumps(payload)
    for secret in secrets:
        assert secret not in dumped


def test_catalog_lists_openai_and_anthropic(client):
    response = client.get("/v1/provider-definitions")
    assert response.status_code == 200, response.text
    definitions = {item["provider"]: item for item in response.json()["items"]}
    assert "openai" in definitions
    assert "anthropic" in definitions
    assert "openai_compatible" in definitions
    assert "cloudflare" in definitions
    assert "supabase" in definitions
    assert "digitalocean" in definitions
    assert definitions["openai"]["adapter_status"] == "available"
    assert definitions["anthropic"]["adapter_status"] == "available"
    assert definitions["gcp"]["adapter_status"] == "planned"


def test_community_edition_mounts_easy_connect(client):
    """conftest mounts the Community edition; the route must not be paid-gated."""
    missing = client.post("/v1/providers/easy-connect", json={})
    assert missing.status_code != 404
    assert missing.status_code == 422
    assert PASTE_VALUE not in missing.text


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
    assert body["secret_ref_id"] == body["secret"]["secret_ref_id"]
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
    assert PASTE_VALUE not in blank.text


def test_too_short_secret_is_rejected(client):
    created = _paste(client, secret_value=SHORT_VALUE, account_label="openai-short")
    assert created.status_code == 422
    assert "too short" in created.text
    assert SHORT_VALUE not in created.text
    listed = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200
    assert not any(
        item.get("account_label") == "openai-short" for item in listed.json()["items"]
    )


def test_validation_error_does_not_echo_secret(client):
    created = client.post(
        "/v1/providers/easy-connect",
        json={
            "project_id": PROJECT_ID,
            "provider": "openai",
            "account_label": "openai-echo",
            "secret_value": PASTE_VALUE,
        },
    )
    assert created.status_code == 422
    assert PASTE_VALUE not in created.text
    assert "sk-test-easy-connect-never-leak-xyz" not in created.text


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
    refs = client.get("/v1/secret-refs", params={"project_id": PROJECT_ID})
    assert refs.status_code == 200, refs.text
    assert PASTE_VALUE not in refs.content.decode("utf-8")
    secret_ref_id = created.json()["secret_ref_id"]
    detail = client.get(
        f"/v1/secret-refs/{secret_ref_id}",
        params={"project_id": PROJECT_ID},
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert PASTE_VALUE not in json.dumps(body)
    assert "value" not in body
    assert "secret_value" not in body


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


def test_repaste_replaces_same_label_and_never_echoes_secret(client):
    first = _paste(client, account_label="openai-rotate")
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["replaced"] is False
    assert first_body["rotated"] is False
    assert first_body["secret_ref_id"] == first_body["secret"]["secret_ref_id"]
    _assert_no_raw_secret(first_body, PASTE_VALUE, REPLACEMENT_VALUE)

    second = _paste(
        client,
        account_label="openai-rotate",
        secret_value=REPLACEMENT_VALUE,
    )
    assert second.status_code == 201, second.text
    body = second.json()
    _assert_no_raw_secret(body, PASTE_VALUE, REPLACEMENT_VALUE)
    assert body["secret_ref_id"]
    assert body["secret"]["secret_ref_id"] == body["secret_ref_id"]
    assert "secret_value" not in body
    assert "value" not in body
    assert body["replaced"] is True
    assert body["provider_account"]["id"] == first_body["provider_account"]["id"]
    assert body["provider_account"]["credential_version"] == 2
    assert body["secret"]["last4"] == REPLACEMENT_VALUE[-4:]
    assert body["secret"]["last4"] != PASTE_VALUE[-4:]


def test_paste_rotates_existing_env_secretref(client):
    created = client.post(
        "/v1/providers",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "provider": "openai",
            "account_label": "openai-env-then-paste",
            "auth_method": "api_key",
            "credential_refs": {"api_key": "env:BOSS_TEST_OPENAI_EASY_CONNECT"},
            "read_only": True,
        },
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    pasted = _paste(client, account_label="openai-env-then-paste")
    assert pasted.status_code == 201, pasted.text
    body = pasted.json()
    _assert_no_raw_secret(body, PASTE_VALUE)
    assert body["rotated"] is True
    assert body["provider_account"]["id"] == account_id
    assert body["provider_account"]["credential_version"] == 2
    pointers = body["provider_account"]["credential_refs"]
    assert all(str(pointer).startswith("local_vault:") for pointer in pointers.values())


def test_infra_and_model_providers_accept_paste(client):
    model = _paste(client, provider="anthropic", account_label="anthropic-paste")
    assert model.status_code == 201, model.text
    _assert_no_raw_secret(model.json(), PASTE_VALUE)
    assert model.json()["provider_account"]["provider"] == "anthropic"
    assert model.json()["secret_ref_id"]

    infra = _paste(
        client,
        provider="cloudflare",
        account_label="cloudflare-paste",
    )
    assert infra.status_code == 201, infra.text
    _assert_no_raw_secret(infra.json(), PASTE_VALUE)
    assert infra.json()["provider_account"]["provider"] == "cloudflare"
    assert all(
        str(pointer).startswith("local_vault:")
        for pointer in infra.json()["provider_account"]["credential_refs"].values()
    )

    droplet = _paste(
        client,
        provider="digitalocean",
        account_label="do-paste",
    )
    assert droplet.status_code == 201, droplet.text
    _assert_no_raw_secret(droplet.json(), PASTE_VALUE)
    refs = droplet.json()["provider_account"]["credential_refs"]
    assert "token" in refs
    assert refs["token"].startswith("local_vault:")
    listed = client.get("/v1/secret-refs", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200
    assert PASTE_VALUE not in listed.content.decode("utf-8")


def test_hosted_kms_backend_is_rejected(client):
    created = _paste(
        client,
        account_label="openai-kms",
        backend="aws_kms",
    )
    assert created.status_code == 422
    assert "local_vault" in created.text
    assert PASTE_VALUE not in created.text


def test_vault_missing_is_503_not_500(client, monkeypatch):
    monkeypatch.delenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", raising=False)
    monkeypatch.delenv("BOSS_KMS_MASTER_KEY", raising=False)
    monkeypatch.delenv("BOSS_KMS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setattr(secret_backend_mod, "_EPHEMERAL_MASTER", None)
    created = _paste(client, account_label="openai-no-vault")
    assert created.status_code == 503, created.text
    assert created.status_code != 500
    assert PASTE_VALUE not in created.text
    detail = created.json()["detail"]
    assert PASTE_VALUE not in str(detail)
    assert "local_vault" in str(detail).lower() or "master" in str(detail).lower()


def test_schema_missing_is_503_not_500(client, monkeypatch):
    class _UndefinedTable(Exception):
        pass

    _UndefinedTable.__name__ = "UndefinedTable"

    def _boom(*_args, **_kwargs):
        raise ProgrammingError(
            "SELECT 1",
            {},
            _UndefinedTable('relation "secret_refs" does not exist'),
        )

    monkeypatch.setattr(api_main.resource_plane, "easy_connect", _boom)
    created = _paste(client, account_label="openai-no-schema")
    assert created.status_code == 503, created.text
    assert created.status_code != 500
    assert "not applied" in created.text
    assert PASTE_VALUE not in created.text
