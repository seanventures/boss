"""Falling back to Postgres for a live credential is a choice, so somebody has to make it.

`default_secret_backend` takes the best store an installation has and drops to `database` when
it has none. That is right for not blocking a first run — a wizard that refuses to complete is
worse than one that completes and can be improved — and wrong as a silent default for a
subscription credential, because this module's own comment says the database is not a good
place for a token and nobody reading a success message would know that is where it went.

So the write refuses once, says exactly what the trade is, and proceeds when told. And the
acceptance is recorded, because "who accepted a weaker store, and when" is a question asked
after an incident, and an answer that depends on memory is no answer.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.config import Settings
from boss_engine.services.setup_service import (
    SecretFallbackNotAccepted,
    SetupError,
    store_coding_agent_credential,
)

CREDENTIAL = json.dumps({"tokens": {"access_token": "v"}})


class _Connection:
    """Enough of a connection to record the statements run against it.

    These tests are about the DECISION, not about storage, so put_secret is stubbed — but the
    write also stamps the provider onto the reference it just created, and a None standing in
    for a connection made that look like a failure of this module rather than of the fiction.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return self


@pytest.fixture
def on_database(monkeypatch):
    """An installation with no secret store configured — the common first run."""
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    stored = {}
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *a, **k: stored.update(k) or {
            "secret_ref_id": "sref_1", "ref_key": "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON",
            "backend": k.get("backend"), "fingerprint": "fp"})
    return stored


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr("boss_engine.services.core.emit_event",
                        lambda c, p, t, a, et, ei, d: seen.append((t, a, d)) or "evt_1")
    return seen


def test_it_refuses_the_database_until_asked(on_database, events) -> None:
    with pytest.raises(SecretFallbackNotAccepted) as exc:
        store_coding_agent_credential(_Connection(), Settings(), "p", "codex", CREDENTIAL, actor="ceo")

    assert exc.value.backend == "database"
    assert not events, "nothing should be recorded for a write that did not happen"


def test_the_refusal_says_what_the_trade_is_and_how_to_proceed(on_database) -> None:
    """"Not configured" would send somebody hunting. This is a decision, so state it."""
    with pytest.raises(SecretFallbackNotAccepted) as exc:
        store_coding_agent_credential(_Connection(), Settings(), "p", "codex", CREDENTIAL, actor="ceo")

    message = str(exc.value)
    assert "anyone with the database has the credential" in message
    assert "accept_database_fallback=true" in message
    assert "bootstrap_infisical" in message


def test_it_is_not_a_plain_setup_error(on_database) -> None:
    """The API answers 409 for this and 422 for the rest; folding them loses the difference.

    Retrying a SetupError verbatim can succeed once the operator fixes the input. Retrying
    this one cannot — what is missing is a decision, not a correction.
    """
    with pytest.raises(SetupError):        # still one, for callers that catch broadly
        store_coding_agent_credential(_Connection(), Settings(), "p", "codex", CREDENTIAL, actor="ceo")

    assert issubclass(SecretFallbackNotAccepted, SetupError)


def test_accepting_stores_it_and_records_who_accepted(on_database, events) -> None:
    result = store_coding_agent_credential(
        _Connection(), Settings(), "p", "codex", CREDENTIAL, actor="ceo",
        accept_database_fallback=True)

    assert result["backend"] == "database"
    assert result["fallback_accepted"] is True
    assert on_database["value"] == CREDENTIAL

    assert len(events) == 1
    event_type, actor, data = events[0]
    assert event_type == "secret.fallback_accepted"
    assert actor == "ceo"
    assert data["backend"] == "database"
    assert CREDENTIAL not in json.dumps(data), "the event must not carry the credential"


def test_a_real_store_needs_no_acknowledgement(monkeypatch, events) -> None:
    """The prompt must appear only when it means something, or it becomes noise to click past."""
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "infisical")
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *a, **k: {"secret_ref_id": "sref_1", "ref_key": "K",
                         "backend": "infisical", "fingerprint": "fp"})

    result = store_coding_agent_credential(
        _Connection(), Settings(), "p", "codex", CREDENTIAL, actor="ceo")

    assert result["backend"] == "infisical"
    assert result["fallback_accepted"] is False
    assert not events, "only a degraded store is worth recording an acceptance for"
