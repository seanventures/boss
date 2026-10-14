"""A connection proof records WHY it ended, settles a known-good unpriced reply, and a person
can release a held one. Real authority on a disposable Postgres; only provider replies are
scripted (setup_checks.complete_once with a fake HTTP answer, as Anthropic shapes them)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from boss_engine.services import setup_checks
from boss_engine.services.connection_spend import (
    HoldRefused,
    describe_outcome,
    held_proofs,
    release_hold,
    review_hold,
)
from boss_engine.services.openai_adapter import price_for_model
from boss_engine.services.setup_service import PROVIDERS, governed_proof
from boss_engine.services.setup_view import CATALOGUE_BY_PROVIDER

from .test_connection_governance import database, scope  # noqa: F401  (fixtures)

KEY = "sk-ant-owned-nonsecret-marker-0123456789"
ACTOR = "usr_owned_owner"


def anthropic_candidate(p, model):
    return {
        "provider": "anthropic", "source_kind": "api", "entitlement_kind": "api_key",
        "owner_type": "project", "owner_id": p, "cost_mode": "usage_based_api",
        "auth_method": "api_key", "connection_status": "connected", "status": "active",
        "runtime_supported": True, "observed_capacity": {"credential_present": False},
        "supported_models": [model], "workload_classes_allowed": ["owner_sandbox"],
        "risk_tier_max": 1, "limit_shape": {"max_concurrent": 1},
    }


def anthropic_proof(scope, model, answer, *, intent="anthropic-intent", sent=None):  # noqa: F811
    e, p, s = scope

    def http(method, url, headers=None, json_body=None, timeout=None):
        if sent is not None:
            sent.append(json_body)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def contact(bound):
        result = setup_checks.complete_once(
            api="anthropic", endpoint="https://api.anthropic.com/v1/messages",
            api_key=str(bound.get("credential") or ""), model=model, http=http)
        result["usage"] = {**result.get("usage", {}), "provider": "anthropic"}
        return result

    return governed_proof(
        e, s, p, "setup-anthropic", ACTOR, intent=intent,
        candidate=anthropic_candidate(p, model), credential=KEY, backend="database",
        capacity_group="connection:anthropic", bound_prove=contact)


def receipt(e, p):
    with e.connect() as c:
        return c.execute(text("SELECT status, result FROM receipts WHERE project_id=:p "
                              "AND result ? 'connection_fingerprint' ORDER BY created_at DESC "
                              "LIMIT 1"), {"p": p}).mappings().one()


def events(e, p, kind):
    with e.connect() as c:
        return [r[0] for r in c.execute(text(
            "SELECT data FROM events WHERE project_id=:p AND event_type=:k ORDER BY created_at"),
            {"p": p, "k": kind})]


def everything(e, p):
    with e.connect() as c:
        return "".join(c.execute(text(
            "SELECT coalesce(string_agg(result::text, ''), '') FROM receipts WHERE project_id=:p "
            "UNION ALL SELECT coalesce(string_agg(data::text, ''), '') FROM events "
            "WHERE project_id=:p"), {"p": p}).scalars())


def anthropic_error(status, kind, message):
    return setup_checks.HttpAnswer(
        status, {}, {"type": "error", "error": {"type": kind, "message": message}})


@pytest.mark.parametrize("status,kind,message,words", [
    (404, "not_found_error", "model: claude-sonnet-9", "Anthropic refused: model not found "
                                                        "(claude-sonnet-9)."),
    (400, "invalid_request_error", "Your credit balance is too low to access the Anthropic "
                                   "API.", "Anthropic refused: credit balance too low."),
    (401, "authentication_error", f"invalid x-api-key {KEY}", "Anthropic refused: the key was "
                                                              "not accepted."),
])
def test_a_refusal_records_the_providers_reason_in_words(scope, status, kind, message, words):  # noqa: F811
    e, p, _ = scope
    result = anthropic_proof(scope, "claude-sonnet-9", anthropic_error(status, kind, message))
    assert result["ok"] is False and not result.get("reconciliation_required")
    assert result["summary"] == words
    saved = receipt(e, p)
    assert saved["status"] == "failed"
    outcome = saved["result"]["connection_outcome"]
    assert outcome["http_status"] == status and outcome["provider_error_type"] == kind
    assert outcome["model"] == "claude-sonnet-9" and outcome["kind"] == "refused"
    assert outcome["summary"] == words
    rejected = events(e, p, "connection.proof_rejected")
    assert rejected and rejected[-1]["outcome"]["summary"] == words
    # Redacted, never the key — not on the receipt, not on the event.
    assert KEY not in everything(e, p)


def test_an_ok_reply_for_an_unpriced_model_settles_labelled_not_held(scope):  # noqa: F811
    e, p, _ = scope
    ok = setup_checks.HttpAnswer(200, {}, {"usage": {"input_tokens": 10, "output_tokens": 2}})
    result = anthropic_proof(scope, "claude-brand-new-9", ok)
    assert result["ok"] is True and result["cost_basis"] == "unpriced"
    assert "BOSS has no price for claude-brand-new-9" in result["summary"]
    saved = receipt(e, p)
    assert saved["status"] == "succeeded"
    assert saved["result"]["connection_outcome"]["usage_present"] is True
    with e.connect() as c:
        held = c.execute(text("SELECT count(*) FROM budget_reservations WHERE project_id=:p "
                              "AND status='active'"), {"p": p}).scalar_one()
        charged = c.execute(text("SELECT coalesce(sum(actual_usd_micros),0) FROM cost_receipts "
                                 "WHERE project_id=:p"), {"p": p}).scalar_one()
    assert held == 0 and charged == 10 * 15 + 2 * 75
    with e.connect() as c:
        assert held_proofs(c, p) == []


def test_an_ok_reply_for_a_current_anthropic_model_is_priced(scope):  # noqa: F811
    e, p, _ = scope
    ok = setup_checks.HttpAnswer(200, {}, {"usage": {"input_tokens": 10, "output_tokens": 2}})
    result = anthropic_proof(scope, "claude-sonnet-5", ok)
    assert result["ok"] is True and result["cost_basis"] == "priced"
    assert result["cost_usd_micros"] == 10 * 2 + 2 * 10


def test_the_proof_asks_exactly_the_model_the_person_typed(scope):  # noqa: F811
    sent = []
    ok = setup_checks.HttpAnswer(200, {}, {"usage": {"input_tokens": 1, "output_tokens": 1}})
    anthropic_proof(scope, "claude-opus-5-5", ok, sent=sent)
    assert [body["model"] for body in sent] == ["claude-opus-5-5"]


def test_a_held_proof_is_reviewed_and_released_by_a_person_then_connect_works(scope):  # noqa: F811
    e, p, s = scope
    held = anthropic_proof(scope, "claude-sonnet-5", setup_checks.HttpAnswer(529, {}, {
        "type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    assert held["reconciliation_required"]
    assert held["summary"].startswith("Anthropic answered HTTP 529: Overloaded.")
    with e.begin() as c:
        [listed] = held_proofs(c, p)
        assert listed["receipt_id"] == held["receipt_id"] and listed["model"] == "claude-sonnet-5"
        assert listed["held_usd_micros"] == 50_000
        assert listed["release"]["basis"] == "unpriced" and listed["release"]["succeeded"]
        assert review_hold(c, p, held["receipt_id"])["summary"] == held["summary"]
        with pytest.raises(HoldRefused):
            release_hold(c, s, p, held["receipt_id"], actor=ACTOR, reason="   ")
    with e.begin() as c:
        done = release_hold(c, s, p, held["receipt_id"], actor=ACTOR,
                            reason="Anthropic was overloaded; I checked the console usage.")
    assert done["status"] == "succeeded"
    assert receipt(e, p)["status"] == "succeeded"
    with e.connect() as c:
        assert c.execute(text("SELECT count(*) FROM budget_reservations WHERE project_id=:p "
                              "AND status='active'"), {"p": p}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p "
                              "AND status='pending'"), {"p": p}).scalar_one() == 0
        assert held_proofs(c, p) == []
    [event] = events(e, p, "connection.hold_released")
    assert event["actor"] == ACTOR and "overloaded" in event["reason"]
    with e.begin() as c, pytest.raises(HoldRefused):
        release_hold(c, s, p, held["receipt_id"], actor=ACTOR, reason="again")
    # Connect is no longer blocked: a new press proves and connects.
    ok = setup_checks.HttpAnswer(200, {}, {"usage": {"input_tokens": 1, "output_tokens": 1}})
    assert anthropic_proof(scope, "claude-sonnet-5", ok, intent="after-release")["ok"]


def test_a_refused_hold_releases_at_zero(scope):  # noqa: F811
    """A hold whose recorded outcome is a refusal settles at cost 0, failed."""
    e, p, s = scope
    held = anthropic_proof(scope, "claude-sonnet-5", TimeoutError("read timed out"))
    with e.begin() as c:
        c.execute(text("UPDATE receipts SET result = jsonb_set(result, '{connection_outcome}', "
                       "CAST(:o AS JSONB)) WHERE id=:r"), {"r": held["receipt_id"], "o": json.dumps(
                           {"kind": "refused", "http_status": 400, "model": "claude-sonnet-5",
                            "provider_error_type": "invalid_request_error",
                            "provider_error": "Your credit balance is too low",
                            "summary": "Anthropic refused: credit balance too low."})})
        done = release_hold(c, s, p, held["receipt_id"], actor=ACTOR, reason="no credit")
    assert done["status"] == "failed" and done["released"]["cost_usd_micros"] == 0


def test_a_legacy_hold_without_an_outcome_reads_its_own_event(scope):  # noqa: F811
    """The laptop's stuck proof: held before outcomes were kept, by the "no usable cost"
    error, which the old code only raised after an OK reply."""
    e, p, s = scope
    held = anthropic_proof(scope, "claude-sonnet-4-5", TimeoutError("x"))
    with e.begin() as c:
        c.execute(text("UPDATE receipts SET result = result - 'connection_outcome' WHERE id=:r"),
                  {"r": held["receipt_id"]})
        c.execute(text("UPDATE events SET data = data - 'outcome' || jsonb_build_object("
                       "'message', 'RuntimeError: RuntimeError: Paid connection reply has no "
                       "usable cost or priced usage evidence.') WHERE project_id=:p "
                       "AND event_type='connection.reconciliation_required'"), {"p": p})
        fact = review_hold(c, p, held["receipt_id"])
        assert fact["summary"].startswith(
            "Anthropic replied OK, but BOSS has no price for claude-sonnet-4-5")
        done = release_hold(c, s, p, held["receipt_id"], actor=ACTOR, reason="legacy repair")
    assert done["status"] == "succeeded" and 0 < done["released"]["cost_usd_micros"] < 50_000


def test_current_anthropic_models_are_priced_and_the_example_is_current():
    for model, rate in {"claude-sonnet-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0),
                        "claude-fable-5-1": (10.0, 50.0), "claude-haiku-4-5": (1.0, 5.0),
                        "claude-sonnet-4-5": (3.0, 15.0),
                        "claude-sonnet-4-5-20250929": (3.0, 15.0),
                        "claude-opus-4-1": (15.0, 75.0)}.items():
        assert price_for_model(model, provider="anthropic",
                               endpoint="https://api.anthropic.com/v1/messages") == (*rate, True)
    assert PROVIDERS["anthropic"]["example_model"] == "claude-sonnet-5"
    assert CATALOGUE_BY_PROVIDER["anthropic"]["example_model"] == "claude-sonnet-5"


def test_words_for_unknown_outcomes():
    assert describe_outcome({"kind": "unknown", "http_status": None,
                             "provider_error": "timed out"}, "Anthropic").startswith(
        "No reply from Anthropic (timed out).")
