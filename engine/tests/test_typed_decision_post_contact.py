"""BOSS-470: post-contact errors retain liability unless nonacceptance is proved.

Canonical PG cases require BOSS_TYPED_DECISION_TEST_DATABASE_URL on an owned,
disposable schema. Only HTTP transport is inert; reservations, leases, receipts,
settlement and events use the real services. No live provider is contacted.
"""
from __future__ import annotations

import os
import json
import traceback
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.entitlements import EntitlementBrokerService
from boss_engine.services.typed_decision_spend import (
    TypedDecisionCancelled,
    TypedDecisionReconciliationRequired,
    TypedDecisionSpend,
    TypedDecisionSpendError,
    _uncertain,
)
from boss_engine.services.typed_decisions import (
    Decision, DecisionBatch, Question, TypeSafeAuthenticationRefused, TypeSafeDecider,
)
from engine.tests.test_typed_decision_spend import (
    QUOTE, World, _binding, _jev_then_claude, _settings,
)


@pytest.mark.parametrize("error", [
    httpx.ReadError("response stream lost"),
    httpx.RemoteProtocolError("peer closed response"),
    RuntimeError("provider rejected the prompt"),
    RuntimeError("TypeSafe HTTP 401 unauthorized"),
    ValueError("response cannot be decoded"),
    TypedDecisionCancelled("too late to prove cancellation"),
])
def test_unknown_error_cannot_release_spend_or_authorize_retry_or_fallback(error):
    world = World()
    spend, jev, careful = _jev_then_claude(world)
    jev.fail = error
    for attempt in (1, 1, 2):
        spend.attempt = attempt
        with pytest.raises(TypedDecisionReconciliationRequired):
            spend.execute(_settings(), _binding(), "My card was charged twice")
    assert (jev.calls, careful.calls) == (1, 0)
    assert world.budget.active_reserved() == QUOTE
    assert world.budget.actual == 0
    assert [r["status"] for r in world.receipts.rows.values()] == ["in_progress"]
    assert "typed_decision.reconciliation_required" in world.events.types()
    assert "typed_decision.fallback" not in world.events.types()


@pytest.mark.parametrize(("status", "attempts", "known_refusal"), [
    (401, 1, True), (401, 2, False), (403, 1, False), (500, 1, False),
    (422, 1, False), (429, 1, False), (529, 1, False),
])
def test_only_single_attempt_adapter_authentication_refusal_is_definitive(
    status, attempts, known_refusal,
):
    settings = Settings(_env_file=None)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": "owned refusal"}),
    ), trust_env=False) as client:
        decider = TypeSafeDecider(settings, api_key="owned-inert", client=client,
                                  attempts=attempts)
        with pytest.raises(RuntimeError) as raised:
            decider.decide("owned input", [Question("decision", "Route?", {"a": "A", "b": "B"})])
    assert isinstance(raised.value, TypeSafeAuthenticationRefused) is known_refusal
    assert _uncertain(raised.value) is not known_refusal


def test_definitive_authentication_refusal_keeps_separately_receipted_fallback():
    world = World()
    spend, jev, careful = _jev_then_claude(world)
    jev.fail = TypeSafeAuthenticationRefused()
    result = spend.execute(_settings(), _binding(), "My card was charged twice")
    assert result["answer"] == "technical"
    assert (jev.calls, careful.calls) == (1, 1)
    assert sorted(r["status"] for r in world.receipts.rows.values()) == ["failed", "succeeded"]
    assert world.budget.active_reserved() == 0


@pytest.mark.parametrize("cost", [None, -1, "3", True, 1.5])
@pytest.mark.parametrize("answer", ["billing", None])
def test_missing_or_invalid_paid_cost_never_becomes_a_zero_cost_failure(cost, answer):
    world = World()
    world.decider.cost, world.decider.answer = cost, answer
    with pytest.raises(TypedDecisionReconciliationRequired, match="cost"):
        world.execute()
    assert world.budget.active_reserved() == QUOTE
    assert world.budget.actual == 0
    assert [r["status"] for r in world.receipts.rows.values()] == ["in_progress"]


def test_type_safe_error_echo_is_scrubbed_before_display_and_audit():
    credential = "owned.opaque.credential.without.a.recognized.prefix"
    world = World()
    spend = world.spend()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        500, json={"detail": "upstream echoed " + credential},
    )), trust_env=False) as client:
        decider = TypeSafeDecider(Settings(_env_file=None), api_key=credential,
                                  client=client, attempts=1)
        spend.decider_factory = lambda role, bound: decider
        with pytest.raises(TypedDecisionReconciliationRequired) as raised:
            spend.execute(_settings(), _binding(), "owned input")
    rendered = "".join(traceback.format_exception(raised.value))
    assert credential not in rendered and "[SECRET]" in rendered
    assert credential not in json.dumps([world.events.rows, world.decisions.rows], default=str)


@pytest.fixture()
def canonical_scope():
    url = os.environ.get("BOSS_TYPED_DECISION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires an owned disposable Postgres")
    engine = create_engine(url, connect_args={"options": "-c statement_timeout=8000"})
    project = "typedcontact_" + uuid4().hex[:12]
    settings = Settings(_env_file=None).model_copy(update={
        "local_model_enabled": True, "local_model_id": "owned-fixture",
        "typesafe_api_url": "https://owned-model.example.test/v1/decision",
    })
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": project})
            c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": project})
            c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) "
                           "VALUES(:p,:p,:p,:p)"), {"p": project})
            c.execute(text("INSERT INTO budget_policies(id,scope_type,scope_id,"
                           "daily_cap_usd_micros,monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
                           "VALUES(:p,'project',:p,100000,100000,0)"), {"p": project})
            EntitlementBrokerService(settings).seed_default_sources(c, project)
            c.execute(text("UPDATE entitlement_sources SET entitlement_kind='usage_based_api', "
                           "cost_mode='usage_based_api' WHERE project_id=:p "
                           "AND source_id='approved_local_model_host'"), {"p": project})
        yield engine, project, settings
    finally:
        engine.dispose()


def _spender(scope, decider, attempt=1):
    engine, project, settings = scope
    spend = TypedDecisionSpend.for_runner(
        settings, engine=engine, project_id=project, run_id="owned-run", step_id="route",
        attempt=attempt, actor="owned-operator", worker_id="owned-worker",
    )
    spend.decider_factory = lambda role, bound: decider
    return spend


def _snapshot(scope):
    engine, project, _ = scope
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT i.id AS invocation_id,i.status AS invocation_status,i.error AS invocation_error,"
            "r.id AS receipt_id,r.status AS receipt_status,r.error AS receipt_error,"
            "b.id AS reservation_id,b.status AS reservation_status,b.amount_usd_micros,"
            "l.status AS lease_status,j.status AS job_status "
            "FROM model_gateway_invocations i JOIN receipts r ON r.id=i.receipt_id "
            "JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
            "JOIN budget_reservations b ON b.id=l.budget_reservation_id "
            "JOIN jobs j ON j.id=i.job_id WHERE i.project_id=:p"
        ), {"p": project}).mappings().one()
        costs = c.execute(text("SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p"),
                          {"p": project}).scalars().all()
    return dict(row), costs


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("failure", ["read", "invalid_json"])
def test_post_contact_error_preserves_canonical_authority_without_fabricating_cost(
    canonical_scope, accepted, failure,
):
    scope = canonical_scope
    engine, project, settings = scope
    witness, calls = [], []

    def contact(request):
        calls.append(1)
        row, costs = _snapshot(scope)  # A separate connection sees committed authority.
        assert row["invocation_status"] == row["receipt_status"] == "in_progress"
        assert row["reservation_status"] == row["lease_status"] == "active"
        assert row["job_status"] == "running" and row["amount_usd_micros"] == QUOTE
        assert costs == []
        if accepted:
            witness.append("accepted outside BOSS database")
        if failure == "read":
            raise httpx.ReadError("owned response lost", request=request)
        return httpx.Response(200, content=b"invalid json")

    with httpx.Client(transport=httpx.MockTransport(contact), trust_env=False) as client:
        decider = TypeSafeDecider(settings, api_key="owned-inert", client=client, attempts=1)
        decider.source_id = "approved_local_model_host"
        for attempt in (1, 1, 2):
            engine.dispose()  # Recreate process-local connections between replay attempts.
            with pytest.raises(TypedDecisionReconciliationRequired):
                _spender(scope, decider, attempt).execute(settings, _binding(), "owned input")
    assert len(calls) == 1 and len(witness) == int(accepted)
    row, costs = _snapshot(scope)
    assert row["invocation_status"] == row["receipt_status"] == "in_progress"
    assert row["reservation_status"] == "active" and row["amount_usd_micros"] == QUOTE
    assert costs == []
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id=:p "
                              "AND event_type='typed_decision.reconciliation_required'"),
                         {"p": project}).scalar_one() >= 1
        assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p"),
                         {"p": project}).scalar_one() >= 1


@pytest.mark.parametrize("kind", ["provider_auth_rejected", "cancelled_before_dispatch"])
def test_known_nonacceptance_has_identical_linked_canonical_and_event_evidence(canonical_scope, kind):
    scope = canonical_scope
    engine, project, settings = scope
    calls = []

    def contact(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "invalid API key"})

    with httpx.Client(transport=httpx.MockTransport(contact), trust_env=False) as client:
        decider = TypeSafeDecider(settings, api_key="owned-inert", client=client, attempts=1)
        decider.source_id = "approved_local_model_host"
        spend = _spender(scope, decider)
        if kind == "cancelled_before_dispatch":
            def cancel():
                raise TypedDecisionCancelled("owned cancellation")
            spend.before_provider_call = cancel
        with pytest.raises(TypedDecisionSpendError):
            spend.execute(settings, _binding(), "owned input", fallback=False)
    assert len(calls) == int(kind == "provider_auth_rejected")
    row, costs = _snapshot(scope)
    assert row["invocation_status"] == row["receipt_status"] == "failed"
    assert row["reservation_status"] == "released" and costs == []
    proof = {
        "version": 1, "kind": kind, "project_id": project,
        **{key: row[key] for key in ("invocation_id", "receipt_id", "reservation_id")},
        **({"provider": "typesafe", "http_status": 401} if kind == "provider_auth_rejected" else {}),
    }
    assert row["invocation_error"]["typed_decision_nonacceptance"] == proof
    assert row["receipt_error"]["typed_decision_nonacceptance"] == proof
    event_type = ("typed_decision.attempt_failed" if kind == "provider_auth_rejected"
                  else "typed_decision.cancelled")
    with engine.connect() as c:
        events = c.execute(text("SELECT data FROM events WHERE project_id=:p AND event_type=:e"),
                           {"p": project, "e": event_type}).scalars().all()
    assert len(events) == 1 and events[0]["typed_decision_nonacceptance"] == proof


@pytest.mark.parametrize("answer", ["billing", None])
def test_known_positive_cost_settles_even_when_the_answer_is_unusable(canonical_scope, answer):
    scope = canonical_scope
    _, _, settings = scope

    class Decider:
        source_id = "approved_local_model_host"

        def decide(self, context, questions):
            return DecisionBatch({"decision": Decision(answer, 0.95)}, "owned", "owned", 1,
                                 cost_usd_micros=3000)

    spend = _spender(scope, Decider())
    if answer:
        assert spend.execute(settings, _binding(), "owned input")["answer"] == answer
    else:
        with pytest.raises(TypedDecisionSpendError, match="no usable answer"):
            spend.execute(settings, _binding(), "owned input")
    row, costs = _snapshot(scope)
    assert row["reservation_status"] == "consumed" and costs == [3000]
    assert row["receipt_status"] == ("succeeded" if answer else "failed")
    assert "typed_decision_nonacceptance" not in row["invocation_error"]


@pytest.mark.parametrize("cost", [None, -1, "3", True, 1.5])
@pytest.mark.parametrize("answer", ["billing", None])
def test_unknown_paid_cost_stays_open_on_canonical_pg(canonical_scope, cost, answer):
    scope = canonical_scope
    _, _, settings = scope

    class Decider:
        source_id = "approved_local_model_host"

        def decide(self, context, questions):
            return DecisionBatch({"decision": Decision(answer, 0.95)}, "owned", "owned", 1,
                                 cost_usd_micros=cost)

    with pytest.raises(TypedDecisionReconciliationRequired, match="cost"):
        _spender(scope, Decider()).execute(settings, _binding(), "owned input")
    row, costs = _snapshot(scope)
    assert row["reservation_status"] == "active" and row["amount_usd_micros"] == QUOTE
    assert row["invocation_status"] == row["receipt_status"] == "in_progress" and costs == []


@pytest.mark.parametrize("answer", ["billing", None])
def test_known_included_source_needs_no_per_call_money_evidence(canonical_scope, answer):
    scope = canonical_scope
    engine, project, settings = scope
    with engine.begin() as c:
        c.execute(text("UPDATE entitlement_sources SET entitlement_kind='local_capacity', "
                       "cost_mode='local_capacity' WHERE project_id=:p "
                       "AND source_id='approved_local_model_host'"), {"p": project})

    class Decider:
        source_id = "approved_local_model_host"

        def decide(self, context, questions):
            return DecisionBatch({"decision": Decision(answer, 0.95)}, "owned", "owned", 1)

    spend = _spender(scope, Decider())
    if answer:
        spend.execute(settings, _binding(), "owned input")
    else:
        with pytest.raises(TypedDecisionSpendError, match="no usable answer"):
            spend.execute(settings, _binding(), "owned input")
    row, costs = _snapshot(scope)
    assert row["amount_usd_micros"] == 0
    assert row["reservation_status"] == ("consumed" if answer else "released")
    assert costs == ([0] if answer else [])


@pytest.mark.parametrize("changed", ["cost_mode", "nonzero_reservation", "foreign_source"])
@pytest.mark.parametrize("answer", ["billing", None])
def test_included_missing_cost_rechecks_exact_authority_at_settlement(canonical_scope, changed, answer):
    scope = canonical_scope
    engine, project, settings = scope
    with engine.begin() as c:
        c.execute(text("UPDATE entitlement_sources SET entitlement_kind='local_capacity', "
                       "cost_mode='local_capacity' WHERE project_id=:p "
                       "AND source_id='approved_local_model_host'"), {"p": project})
    foreign_source = None
    if changed == "foreign_source":
        from engine.tests.test_legacy_financial_exposure import new_project

        foreign = new_project(engine)
        with engine.begin() as c:
            EntitlementBrokerService(settings).seed_default_sources(c, foreign)
            foreign_source = c.execute(text("SELECT id FROM entitlement_sources WHERE project_id=:p "
                                           "AND source_id='approved_local_model_host'"),
                                       {"p": foreign}).scalar_one()

    class Decider:
        source_id = "approved_local_model_host"

        def decide(self, context, questions):
            with engine.begin() as c:
                if changed == "cost_mode":
                    c.execute(text("UPDATE entitlement_sources SET entitlement_kind='usage_based_api', "
                                   "cost_mode='usage_based_api' WHERE project_id=:p "
                                   "AND source_id='approved_local_model_host'"), {"p": project})
                elif changed == "nonzero_reservation":
                    c.execute(text("UPDATE budget_reservations SET amount_usd_micros=1 "
                                   "WHERE project_id=:p"), {"p": project})
                else:
                    c.execute(text("UPDATE model_gateway_leases SET entitlement_source_id=:source "
                                   "WHERE project_id=:p"), {"p": project, "source": foreign_source})
            return DecisionBatch({"decision": Decision(answer, 0.95)}, "owned", "owned", 1)

    with pytest.raises(TypedDecisionReconciliationRequired, match="cost"):
        _spender(scope, Decider()).execute(settings, _binding(), "owned input")
    row, costs = _snapshot(scope)
    assert row["reservation_status"] == "active" and costs == []
    assert row["invocation_status"] == row["receipt_status"] == "in_progress"


@pytest.mark.parametrize("mode", [
    "response_echo", "transport_echo", "parse_echo", "decode_echo", "choice_echo", "cancel",
])
def test_credentials_are_absent_from_canonical_failure_surfaces(canonical_scope, mode):
    scope = canonical_scope
    engine, project, settings = scope
    credential = "owned.opaque.credential.without.a.recognized.prefix"
    def response(request):
        if mode == "transport_echo":
            raise httpx.ReadError("upstream echoed " + credential, request=request)
        if mode == "parse_echo":
            return httpx.Response(200, json={"answers": {
                "decision": {"choice": "billing", "confidence": credential},
            }, "usage": {"input_tokens": 2}})
        if mode == "decode_echo":
            return httpx.Response(200, content=credential.encode())
        if mode == "choice_echo":
            return httpx.Response(200, json={"answers": {
                "decision": {"choice": credential, "confidence": 0.9},
            }, "usage": {"input_tokens": 100000}})
        return httpx.Response(500, json={"detail": "upstream echoed " + credential})

    with httpx.Client(transport=httpx.MockTransport(response), trust_env=False) as client:
        decider = TypeSafeDecider(settings, api_key=credential, client=client, attempts=1)
        decider.source_id = "approved_local_model_host"
        spend = _spender(scope, decider)
        if mode == "cancel":
            def cancel():
                raise TypedDecisionCancelled("cancelled with api_key=" + credential)
            spend.before_provider_call = cancel
        expected = (TypedDecisionSpendError if mode in {"cancel", "choice_echo"}
                    else TypedDecisionReconciliationRequired)
        with pytest.raises(expected) as raised:
            spend.execute(settings, _binding(), "owned input", fallback=False)
    assert credential not in "".join(traceback.format_exception(raised.value))
    with engine.connect() as c:
        for table in ("jobs", "model_gateway_invocations", "receipts", "events", "decisions"):
            rows = c.execute(text(f"SELECT to_jsonb(t) FROM {table} t WHERE project_id=:p"),
                             {"p": project}).scalars().all()
            assert credential not in json.dumps(rows, default=str), table
