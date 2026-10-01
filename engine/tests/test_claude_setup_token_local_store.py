"""An operator-supplied Claude setup token is stored only in the local secret store.

The value must not land in Postgres, an env file, a log line, or a backup. A missing
token or an unavailable local store is an error, not a successful connection.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.config import Settings
from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
from boss_engine.services.secrets_service import ref_key_for
from boss_engine.services.setup_service import SetupError, store_coding_agent_credential

TOKEN = "sk-ant-oat01-" + "t" * 48  # shaped like a real setup token; not one


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


def test_with_its_key_outside_the_database_the_database_store_may_hold_the_token(monkeypatch):
    """An install that chose the database store, with BOSS_KMS_MASTER_KEY in .env: Postgres holds
    only ciphertext and a dump is not a key compromise, so the token is accepted there."""
    monkeypatch.setenv("BOSS_KMS_MASTER_KEY", "a" * 44)
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
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

    assert result["backend"] == "database" and stored["backend"] == "database"
    assert result["raw_secret_stored"] is False
    # Chosen at install time, so it is not reported as a reluctant fallback.
    assert result["fallback_accepted"] is True  # backend == "database"; recorded, not prompted


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
    assert "BOSS_KMS_MASTER_KEY" in message and "Re-run the installer" in message
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


@pytest.mark.parametrize("pasted", ["Everyword99", "hunter2", "sk-ant-short", "not a token at all"])
def test_something_that_is_not_a_setup_token_is_refused_before_it_is_stored(monkeypatch, pasted):
    """On boss-1 a 10-character value had been pasted where the Claude setup token belonged. It
    was stored without a word and surfaced as "401 Invalid bearer token" inside a workflow. The
    refusal never repeats what was pasted: a wrong paste is often a password."""
    monkeypatch.setattr("boss_engine.services.secrets_service.put_secret",
                        lambda *a, **k: pytest.fail("a non-token was stored"))
    with pytest.raises(SetupError) as refused:
        store_coding_agent_credential(None, Settings(), "boss-dogfood", "claude_code", pasted,
                                      actor="ceo")
    assert "sk-ant-" in str(refused.value) and "Nothing was stored" in str(refused.value)
    assert pasted not in str(refused.value)
