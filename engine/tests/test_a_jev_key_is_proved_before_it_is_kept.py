"""A TypeSafe key is proved with one decision before BOSS keeps it, and keeping it is what
switches a project's decisions to Jev. Without a key, "typesafe" chosen still means Claude, and
the status says so rather than letting the setting look ignored."""
from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import project_settings
from boss_engine.services.setup_service import (
    SecretFallbackNotAccepted,
    SetupError,
    decision_model_status,
    store_decision_key,
)
from boss_engine.services.typed_decisions import Decision, DecisionBatch


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return self


@pytest.fixture
def world(monkeypatch):
    seen = {"stored": {}, "knobs": {}, "events": []}
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "infisical")
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *a, **k: seen["stored"].update(k) or {
            "secret_ref_id": "sref_1", "ref_key": "BOSS_SUBSCRIPTION_TYPESAFE_API_KEY",
            "backend": k.get("backend"), "fingerprint": "fp"})
    monkeypatch.setattr(project_settings, "set_one",
                        lambda c, p, k, v, actor="": seen["knobs"].update({k: v}))
    monkeypatch.setattr("boss_engine.services.core.emit_event",
                        lambda *a: seen["events"].append(a[2]))
    return seen


def _jev(monkeypatch, answer=None, error=None):
    def decide(self, context, questions):
        if error:
            raise RuntimeError(error)
        return DecisionBatch({"check": Decision(answer, 0.9)}, "typesafe", "jev-1.13.0", 120)
    monkeypatch.setattr("boss_engine.services.typed_decisions.TypeSafeDecider.decide", decide)


def test_a_working_key_is_stored_and_turns_jev_on(monkeypatch, world):
    _jev(monkeypatch, answer="yes")
    out = store_decision_key(_Connection(), Settings(), "p", " ts_key ", actor="ceo")
    assert out["ok"] and out["raw_secret_stored"] is False
    assert world["stored"]["name"] == "TYPESAFE_API_KEY" and world["stored"]["value"] == "ts_key"
    assert world["knobs"] == {"decision_backend": "typesafe"}


def test_a_rejected_key_is_not_stored(monkeypatch, world):
    _jev(monkeypatch, error="TypeSafe 401: unauthorized")
    with pytest.raises(SetupError, match="rejected"):
        store_decision_key(_Connection(), Settings(), "p", "bad")
    assert not world["stored"] and not world["knobs"]


def test_an_unfunded_account_says_so(monkeypatch, world):
    _jev(monkeypatch, error="TypeSafe 402: Your organization is out of funds")
    with pytest.raises(SetupError, match="no credit"):
        store_decision_key(_Connection(), Settings(), "p", "ts_key")


def test_the_database_is_a_decision_here_too(monkeypatch, world):
    _jev(monkeypatch, answer="yes")
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    with pytest.raises(SecretFallbackNotAccepted):
        store_decision_key(_Connection(), Settings(), "p", "ts_key")
    assert not world["stored"]


def test_typesafe_without_a_key_is_reported_as_claude(monkeypatch):
    monkeypatch.setattr(project_settings, "applied",
                        lambda c, p, s: s.model_copy(update={"decision_backend": "typesafe"}))
    monkeypatch.setattr("boss_engine.services.typed_decisions._typesafe_key", lambda s: None)
    status = decision_model_status(None, Settings(), "p")
    assert status["backend"] == "typesafe" and status["effective_backend"] == "claude"


def test_the_backend_knob_accepts_only_the_two_it_knows():
    assert project_settings.coerce("decision_backend", "TypeSafe") == "typesafe"
    with pytest.raises(ValueError):
        project_settings.coerce("decision_backend", "gpt")
