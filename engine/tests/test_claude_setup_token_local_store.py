"""An operator-supplied Claude setup token is stored only in the local secret store.

The value must not land in Postgres, an env file, a log line, or a backup. A missing
token or an unavailable local store is an error, not a successful connection.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.config import Settings
from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
from boss_engine.services.secret_backends import LocalVaultBackend
from boss_engine.services.secrets_service import ref_key_for
from boss_engine.services.setup_service import SetupError, store_coding_agent_credential

TOKEN = "test-claude-token"


class _Row:
    def mappings(self):
        return self

    def first(self):
        return {"id": "sref_test"}


class _Connection:
    def __init__(self) -> None:
        self.params: list[dict] = []

    def execute(self, _statement, params=None):
        self.params.append(dict(params or {}))
        return _Row()


def test_claude_setup_token_is_stored_only_in_the_local_secret_store(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setenv("BOSS_KMS_VAULT_DIR", str(vault))
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "boss_engine.services.secret_backends.default_secret_backend",
        lambda _settings: "database",
    )
    monkeypatch.setattr("boss_engine.services.core.emit_event", lambda *args, **kwargs: "evt")
    connection = _Connection()

    result = store_coding_agent_credential(
        connection, Settings(), "boss-dogfood", "claude_code", TOKEN,
        actor="ceo", accept_database_fallback=True,
    )

    assert result["backend"] == "local_vault"
    assert result["raw_secret_stored"] is False
    assert result["fallback_accepted"] is False
    key = ref_key_for(SUBSCRIPTION_SECRET_SCOPE, "CLAUDE_CODE_OAUTH_TOKEN")
    assert LocalVaultBackend().get(key) == TOKEN
    archived = b"".join(path.read_bytes() for path in vault.rglob("*") if path.is_file())
    assert TOKEN.encode() not in archived
    persisted = json.dumps(connection.params)
    assert TOKEN not in persisted
    assert "database" not in {call.get("sb") or call.get("rk") for call in connection.params}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in __import__("os").environ


def test_a_missing_claude_setup_token_is_not_stored(monkeypatch):
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *args, **kwargs: pytest.fail("empty token was stored"),
    )

    with pytest.raises(SetupError, match="required") as exc:
        store_coding_agent_credential(
            None, Settings(), "boss-dogfood", "claude_code", "   ", actor="ceo",
        )

    assert "test-claude-token" not in str(exc.value)


def test_claude_setup_token_does_not_fall_back_to_postgres(monkeypatch):
    monkeypatch.delenv("BOSS_KMS_MASTER_KEY", raising=False)
    monkeypatch.delenv("BOSS_KMS_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", raising=False)
    monkeypatch.setattr(
        "boss_engine.services.secret_backends.default_secret_backend",
        lambda _settings: "database",
    )
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *args, **kwargs: pytest.fail("token stored without a local secret store"),
    )

    with pytest.raises(SetupError, match="not stored") as exc:
        store_coding_agent_credential(
            None, Settings(), "boss-dogfood", "claude_code", TOKEN,
            actor="ceo", accept_database_fallback=True,
        )

    message = str(exc.value)
    assert "never put in BOSS's Postgres database" in message
    assert "--secret-store infisical" in message
    assert TOKEN not in message


def test_an_install_with_a_secret_store_keeps_the_claude_token_there(monkeypatch):
    """--secret-store infisical: the token goes to Infisical, not refused for want of a vault.

    The first version accepted only the local vault, which no installer configures, so a fresh
    `install.sh --secret-store infisical --connect-claude` refused the token with "that store is
    not available on this install" — while the same run had just stored the Codex login in
    Infisical (reported 2026-09-30)."""
    monkeypatch.delenv("BOSS_KMS_MASTER_KEY", raising=False)
    monkeypatch.delenv("BOSS_KMS_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", raising=False)
    monkeypatch.setattr(
        "boss_engine.services.secret_backends.default_secret_backend",
        lambda _settings: "infisical",
    )
    stored = {}
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *args, **kwargs: stored.update(kwargs) or {
            "secret_ref_id": "sref_1", "ref_key": "k", "backend": kwargs["backend"],
            "fingerprint": "fp"},
    )
    monkeypatch.setattr("boss_engine.services.core.emit_event", lambda *args, **kwargs: "evt")

    result = store_coding_agent_credential(
        _Connection(), Settings(), "boss-dogfood", "claude_code", TOKEN, actor="ceo",
    )

    assert result["backend"] == "infisical"
    assert stored["backend"] == "infisical"
    assert result["fallback_accepted"] is False
