"""Typed-decision key proof is held until model spend has durable evidence."""
from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import project_settings
from boss_engine.services.setup_service import (
    DecisionModelGovernanceHold,
    SetupError,
    decision_model_status,
    store_decision_key,
)


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


@pytest.mark.parametrize("accept_database_fallback", [False, True])
def test_key_proof_is_held_before_provider_call_or_secret_write(
    monkeypatch, world, accept_database_fallback,
):
    monkeypatch.setattr("boss_engine.services.typed_decisions.TypeSafeDecider.decide",
                        lambda *a, **k: pytest.fail("TypeSafe was called before reservation"))
    connection = _Connection()
    with pytest.raises(DecisionModelGovernanceHold, match="BOSS-451"):
        store_decision_key(connection, Settings(), "p", " ts_key ", actor="ceo",
                           accept_database_fallback=accept_database_fallback)
    assert connection.statements == []
    assert not world["stored"] and not world["knobs"] and not world["events"]


def test_blank_key_still_has_an_actionable_validation_error(world):
    with pytest.raises(SetupError, match="paste the API key"):
        store_decision_key(_Connection(), Settings(), "p", "")
    assert not world["stored"]


def test_typesafe_without_a_key_is_reported_as_paused_with_saved_preference(monkeypatch):
    monkeypatch.setattr(project_settings, "applied",
                        lambda c, p, s: s.model_copy(update={"decision_backend": "typesafe"}))
    monkeypatch.setattr("boss_engine.services.typed_decisions._typesafe_key", lambda s: None)
    status = decision_model_status(None, Settings(), "p")
    assert status["backend"] == "typesafe" and status["configured_backend"] == "claude"
    assert status["effective_backend"] == "paused"
    assert status["execution_available"] is False and "BOSS-451" in status["hold_reason"]


def test_the_backend_knob_accepts_only_the_two_it_knows():
    assert project_settings.coerce("decision_backend", "TypeSafe") == "typesafe"
    with pytest.raises(ValueError):
        project_settings.coerce("decision_backend", "gpt")


def test_jev_is_offered_where_keys_are_pasted_but_never_as_a_model_to_build_with():
    from boss_engine.services.setup_service import provider_catalogue

    jev = next(p for p in provider_catalogue() if p["provider"] == "typesafe")
    assert jev["kind"] == "decision" and "decisions only" in jev["label"]
