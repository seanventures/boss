"""A TypeSafe key is proved through the governed spend path before it is kept (BOSS-451 step 5).

The proof is a billed provider call. It used to run inside the setup request's transaction,
before any reservation or receipt, so a failed key write or a crash erased all evidence of the
charge. It now runs through TypedDecisionSpend — reservation, lease and receipt committed on
their own connections before the call, settled after — exactly like a workflow decision.
"""
from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import project_settings
from boss_engine.services.setup_service import (
    DecisionModelGovernanceHold,
    SecretFallbackNotAccepted,
    SetupError,
    decision_model_status,
    store_decision_key,
)
from boss_engine.services.typed_decision_spend import (
    TypedDecisionReconciliationRequired,
    TypedDecisionSpendError,
)


class _Connection:
    engine = object()

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return self


class _Spend:
    """Stands in for TypedDecisionSpend; records what it was asked and in what order."""

    def __init__(self, seen, outcome):
        self.seen, self.outcome = seen, outcome

    def execute(self, settings, binding, state):
        self.seen["order"].append("spend")
        self.seen["proving"] = settings
        self.seen["binding"] = binding
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def world(monkeypatch):
    seen = {"stored": {}, "knobs": {}, "events": [], "order": [], "for_runner": None,
            "outcome": {"answer": "yes", "model": "jev-1", "latency_ms": 180,
                        "receipt_id": "rcpt_1", "cost_usd_micros": 12}}
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "infisical")

    def put_secret(*a, **k):
        seen["order"].append("store")
        seen["stored"].update(k)
        return {"secret_ref_id": "sref_1", "ref_key": "BOSS_SUBSCRIPTION_TYPESAFE_API_KEY",
                "backend": k.get("backend"), "fingerprint": "fp"}

    monkeypatch.setattr("boss_engine.services.secrets_service.put_secret", put_secret)
    monkeypatch.setattr(project_settings, "set_one",
                        lambda c, p, k, v, actor="": seen["knobs"].update({k: v}))
    monkeypatch.setattr("boss_engine.services.core.emit_event",
                        lambda *a: seen["events"].append((a[2], a[-1])))

    def for_runner(settings, **kwargs):
        seen["for_runner"] = kwargs
        return _Spend(seen, seen["outcome"])

    monkeypatch.setattr(
        "boss_engine.services.typed_decision_spend.TypedDecisionSpend.for_runner", for_runner)
    monkeypatch.setattr("boss_engine.services.typed_decisions.TypeSafeDecider.decide",
                        lambda *a, **k: pytest.fail("TypeSafe was called outside the spend path"))
    return seen


def test_a_key_is_proved_through_the_spend_path_then_stored(world):
    result = store_decision_key(_Connection(), Settings(), "p", " ts_key ", actor="ceo")

    assert world["order"] == ["spend", "store"], "proved before it is kept"
    assert world["for_runner"]["project_id"] == "p"
    assert world["for_runner"]["engine"] is _Connection.engine, "committed on its own connection"
    proving = world["proving"]
    assert proving.decision_backend == "typesafe" and proving.typesafe_api_key == "ts_key"
    assert proving.decision_fallback_below == 0.0, "a Claude second opinion proves nothing"
    assert set(world["binding"]["typed_decision"]["options"]) == {"yes", "no"}

    assert world["stored"]["value"] == "ts_key" and world["stored"]["backend"] == "infisical"
    assert world["knobs"] == {"decision_backend": "typesafe"}
    assert result["ok"] and result["receipt_id"] == "rcpt_1" and result["cost_usd_micros"] == 12
    assert result["raw_secret_stored"] is False and "ts_key" not in repr(result)
    event, data = world["events"][0]
    assert event == "setup.decision_model_connected" and data["receipt_id"] == "rcpt_1"


def test_nowhere_safe_to_store_it_is_settled_before_anything_is_spent(world, monkeypatch):
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    with pytest.raises(SecretFallbackNotAccepted):
        store_decision_key(_Connection(), Settings(), "p", "ts_key")
    assert world["order"] == [], "no proof is paid for a key that cannot be kept"


@pytest.mark.parametrize(("failure", "says"), [
    (TypedDecisionSpendError("TypeSafe HTTP 401 unauthorized"), "rejected that key"),
    (TypedDecisionSpendError("TypeSafe HTTP 402 insufficient funds"), "no credit"),
    (TypedDecisionSpendError("Budget reservation denied."), "could not check that key"),
    (TypedDecisionReconciliationRequired("timed out"), "reconciled rather than charged twice"),
])
def test_a_failed_proof_keeps_nothing_and_says_why(world, failure, says):
    world["outcome"] = failure
    with pytest.raises(SetupError, match=says):
        store_decision_key(_Connection(), Settings(), "p", "ts_key")
    assert world["order"] == ["spend"]
    assert not world["stored"] and not world["knobs"] and not world["events"]


def test_without_a_separate_connection_there_is_no_call(world):
    class _Bare(_Connection):
        engine = None

    with pytest.raises(DecisionModelGovernanceHold, match="BOSS-451"):
        store_decision_key(_Bare(), Settings(), "p", "ts_key")
    assert world["order"] == []


def test_blank_key_still_has_an_actionable_validation_error(world):
    with pytest.raises(SetupError, match="paste the API key"):
        store_decision_key(_Connection(), Settings(), "p", "")
    assert not world["stored"] and world["order"] == []


def test_status_says_who_is_actually_deciding(monkeypatch):
    monkeypatch.setattr(project_settings, "applied",
                        lambda c, p, s: s.model_copy(update={"decision_backend": "typesafe"}))
    monkeypatch.setattr("boss_engine.services.typed_decisions._typesafe_key", lambda s: None)
    status = decision_model_status(None, Settings(), "p")
    assert status["backend"] == "typesafe" and status["effective_backend"] == "claude", (
        "typesafe chosen with no key still means Claude")
    assert status["execution_available"] is True and status["hold_reason"] == ""

    monkeypatch.setattr("boss_engine.services.typed_decisions._typesafe_key", lambda s: "k")
    assert decision_model_status(None, Settings(), "p")["effective_backend"] == "typesafe"


def test_the_backend_knob_accepts_only_the_two_it_knows():
    assert project_settings.coerce("decision_backend", "TypeSafe") == "typesafe"
    with pytest.raises(ValueError):
        project_settings.coerce("decision_backend", "gpt")


def test_jev_is_offered_where_keys_are_pasted_but_never_as_a_model_to_build_with():
    from boss_engine.services.setup_service import provider_catalogue

    jev = next(p for p in provider_catalogue() if p["provider"] == "typesafe")
    assert jev["kind"] == "decision" and "decisions only" in jev["label"]
