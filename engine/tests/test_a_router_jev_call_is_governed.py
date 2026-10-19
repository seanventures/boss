"""The model router's Jev calls lift from BOSS-451's hold only behind a setting, and only
through the governed typed-decision path (BOSSAPP-579).

Four conditions before a live Jev call may spend, one test each: a committed budget
reservation for the attempt, an active model lease, a deduplicated receipt, a cost settlement.
A missing one refuses the call and records why. With `BOSS_ROUTER_JEV_LIVE` off (the default)
nothing changes: Jev stays "held" and the rules classify.

The doubles are test_typed_decision_spend's: a budget that counts active reservations against
its cap, a gateway that refuses a lease without a job lease and a reservation, receipts keyed
by dedupe. The router's spend is TypedDecisionBatchSpend on those same doubles.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ._router_fixtures import policy
from .test_typed_decision_spend import PROJECT, QUOTE, Scripted, World
from boss_engine.services.model_router.classifier import (
    Classifier, TaskSignal, _from_batch, jev_state, tier_question,
)
from boss_engine.services.model_router.router import Snapshot
from boss_engine.services.model_router.store import GovernedJevDecider, _wire_deciders
from boss_engine.services.pipeline_profiles import _rigor_questions
from boss_engine.services.typed_decision_spend import (
    TypedDecisionBatchSpend, TypedDecisionSpendError,
)
from boss_engine.services.typed_decisions import (
    Decision, DecisionBatch, TypeSafeAuthenticationRefused, TypeSafeDecider,
)

CONTEXT = "Change the button colour to blue on the existing settings page.\n\n(Step: implement)"


def _questions():
    return [tier_question(policy().classifier), *_rigor_questions()]


class _Jev(Scripted):
    """Jev as the router sees it: several answers from one call, named backend and source."""

    def __init__(self, world: World) -> None:
        super().__init__(world.budget, world.gateway, lambda: world.open_transactions > 0)
        self.backend, self.source_id = "typesafe", "typesafe_jev"
        self.answers = {"route_tier": ("fast", 0.91), "tier": ("0", 0.88), "sensitive": ("no", 0.95)}
        self.asked: list[list[str]] = []

    def decide(self, context, questions):
        assert self.events_open() is False, "provider call ran inside the uncommitted transaction"
        assert self.budget.active_reserved() >= QUOTE, "provider call without a live reservation"
        assert self.gateway.active_lease_count() >= 1, "provider call without a live model lease"
        self.calls += 1
        self.asked.append([q.key for q in questions])
        if self.fail is not None:
            raise self.fail
        return DecisionBatch(
            {q.key: Decision(*self.answers.get(q.key, (None, None))) for q in questions},
            "typesafe", "jev-1", 7, cost_usd_micros=self.cost, usage={"input_tokens": 900},
        )


def _world(cap: int = 50_000) -> World:
    world = World(cap=cap)
    world.jev = _Jev(world)
    return world


def _spend(world: World, *, before_provider_call=None) -> TypedDecisionBatchSpend:
    return TypedDecisionBatchSpend(
        project_id=PROJECT, run_id="model_router", step_id="classify-abc", attempt=1,
        actor="model_router", worker_id="model_router:1",
        budget=world.budget, jobs=world.jobs, gateway=world.gateway, receipts=world.receipts,
        events=world.events, decisions=world.decisions,
        decider_factory=lambda role, settings: world.jev, commit=world.commit,
        org_id="org", workspace_id="ws", before_provider_call=before_provider_call,
        ttl_seconds=120,
    )


def _decide(world: World, *, before_provider_call=None) -> DecisionBatch:
    return _spend(world, before_provider_call=before_provider_call).decide(
        SimpleNamespace(), CONTEXT, _questions())


# ── The setting ────────────────────────────────────────────────────────────────────────────────

def _settings(live: bool):
    return SimpleNamespace(decision_backend="typesafe", typesafe_api_key="tk",
                           typesafe_api_url="https://ts.invalid/v1", typesafe_model="jev",
                           router_jev_live=live)


def test_the_hold_lifts_only_behind_the_setting_which_is_off_by_default():
    from boss_engine.config import Settings

    assert Settings(_env_file=None).router_jev_live is False
    assert jev_state(_settings(False)) == "held"
    assert jev_state(_settings(True)) == "ready"
    assert jev_state(SimpleNamespace(decision_backend="claude", typesafe_api_key="",
                                     router_jev_live=True)) == "absent", "no key is no Jev"


def test_with_the_setting_off_the_router_is_wired_exactly_as_before():
    snap = Snapshot(project_id="p", policy=policy(), sources={},
                    jev_state=jev_state(_settings(False)))
    _wire_deciders(snap, SimpleNamespace(engine=object()), _settings(False), "p")
    assert snap.jev_state == "held" and snap.jev_factory is None
    assert snap.model_factory is not None


def test_with_the_setting_on_the_router_gets_a_governed_decider_never_a_bare_client():
    snap = Snapshot(project_id="p", policy=policy(), sources={},
                    jev_state=jev_state(_settings(True)))
    _wire_deciders(snap, SimpleNamespace(engine=object()), _settings(True), "p")
    assert snap.jev_state == "ready"
    jev = snap.jev_factory()
    assert isinstance(jev, GovernedJevDecider) and not isinstance(jev, TypeSafeDecider)
    assert jev.source_id == "typesafe_jev", "capacity is booked on TypeSafe's own source"


def test_a_router_jev_call_goes_through_the_spend_path(monkeypatch):
    seen: dict = {}

    class _Spend:
        decider_factory = None

        def decide(self, settings, context, questions):
            seen["context"], seen["questions"] = context, [q.key for q in questions]
            return DecisionBatch({"route_tier": Decision("fast", 0.9, reason="trivial")},
                                 "typesafe", "jev-1", 5, cost_usd_micros=40)

    def for_runner(settings, **kwargs):
        seen["for_runner"] = kwargs
        seen["spend"] = _Spend()
        return seen["spend"]

    monkeypatch.setattr(TypedDecisionBatchSpend, "for_runner", for_runner)
    monkeypatch.setattr(TypeSafeDecider, "decide",
                        lambda *a, **k: pytest.fail("TypeSafe was called outside the spend path"))
    engine = object()
    signal = TaskSignal(kind="build_stage", step="implement",
                        spec={"title": "", "instructions": CONTEXT})
    c = Classifier().classify(signal, policy().classifier,
                              jev=GovernedJevDecider(engine, _settings(True), "p"),
                              jev_state="ready")
    assert (c.classifier, c.tier) == ("jev", "fast")
    wired = seen["for_runner"]
    assert wired["engine"] is engine and wired["project_id"] == "p"
    assert wired["run_id"] == "model_router" and wired["step_id"].startswith("classify-")
    assert seen["questions"][0] == "route_tier" and "tier" in seen["questions"], (
        "routing and rigor ride on one call")
    built = seen["spend"].decider_factory("primary", _settings(True))
    assert isinstance(built, TypeSafeDecider) and built._attempts == 1, (
        "only Jev answers, once: a retry inside one receipt would be a second call")


def test_without_a_project_or_connection_jev_refuses_before_anything_is_reserved(monkeypatch):
    monkeypatch.setattr(TypedDecisionBatchSpend, "for_runner",
                        lambda *a, **k: pytest.fail("a spend was built with nowhere to commit"))
    with pytest.raises(RuntimeError, match="rules classify instead"):
        GovernedJevDecider(None, _settings(True), "p").decide(CONTEXT, _questions())
    with pytest.raises(RuntimeError, match="rules classify instead"):
        GovernedJevDecider(object(), _settings(True), None).decide(CONTEXT, _questions())


# ── The four conditions ────────────────────────────────────────────────────────────────────────

def test_condition_1_no_committed_reservation_means_no_call_and_a_recorded_refusal():
    world = _world(cap=1_000)
    with pytest.raises(TypedDecisionSpendError, match="Budget cap exceeded"):
        _decide(world)
    assert world.jev.calls == 0
    assert world.gateway.acquire_calls == 0
    assert world.budget.active_reserved() == 0 and world.receipts.rows == {}
    assert world.budget.decisions, "a refused reservation leaves a decision"
    assert "typed_decision.spend_refused" in world.events.types()


def test_condition_2_no_active_lease_means_no_call_and_the_reservation_is_released():
    world = _world()

    def expire() -> None:
        world.clock.advance(121)

    with pytest.raises(TypedDecisionSpendError, match="expired"):
        _decide(world, before_provider_call=expire)
    assert world.jev.calls == 0
    assert world.budget.active_reserved() == 0
    assert all(row["status"] != "in_progress" for row in world.receipts.rows.values())
    assert "typed_decision.lease_expired" in world.events.types()


def test_condition_3_the_same_questions_about_the_same_context_replay_one_receipt():
    world = _world()
    first = _decide(world)
    second = _decide(world)
    assert first.decisions["route_tier"].answer == "fast"
    assert second.decisions["route_tier"].answer == "fast"
    assert second.usage["receipt_id"] == first.usage["receipt_id"]
    assert world.jev.calls == 1 and len(world.receipts.rows) == 1
    receipt = next(iter(world.receipts.rows.values()))
    assert receipt["dedupe_key"].startswith(f"model-gateway:{PROJECT}:typed_decision:{PROJECT}:model_router:classify-abc:")


def test_condition_4_the_actual_cost_is_settled_against_the_reservation():
    world = _world(cap=50_000)
    batch = _decide(world)
    assert batch.cost_usd_micros == 4_000
    assert world.budget.active_reserved() == 0
    assert world.budget.actual == 4_000 and world.budget.available() == 46_000
    reservation = next(iter(world.budget.reservations.values()))
    assert reservation == {**reservation, "amount": QUOTE, "status": "consumed", "actual": 4_000}
    receipt = next(iter(world.receipts.rows.values()))
    assert receipt["status"] == "succeeded"
    assert receipt["result"]["answers"]["sensitive"]["answer"] == "no", (
        "the receipt keeps every answer, not only the lead one")
    assert "typed_decision.settled" in world.events.types()
    assert world.gateway.acquired_for == [("typesafe_jev", "model_router")]


# ── What the router gets back, and how a failure is retried ────────────────────────────────────

def test_every_question_rides_on_one_call_and_comes_back_for_the_classifier():
    world = _world()
    batch = _decide(world)
    assert world.jev.asked == [["route_tier", "tier", "sensitive"]]
    signal = TaskSignal(kind="build_stage", step="implement",
                        spec={"title": "", "instructions": CONTEXT})
    c = _from_batch(batch, signal, "jev")
    assert (c.tier, c.classifier, c.risk_tier, c.sensitive) == ("fast", "jev", 0, False)


def test_a_rejected_key_fails_the_attempt_visibly_and_the_next_ask_is_the_next_attempt():
    world = _world()
    world.jev.fail = TypeSafeAuthenticationRefused()
    with pytest.raises(TypedDecisionSpendError, match="authentication refused"):
        _decide(world)
    assert world.budget.active_reserved() == 0
    assert [row["status"] for row in world.receipts.rows.values()] == ["failed"]

    world.jev.fail = None
    batch = _decide(world)
    assert batch.decisions["route_tier"].answer == "fast" and batch.usage["attempt"] == 2
    assert world.jev.calls == 2 and len(world.receipts.rows) == 2
    assert "typed_decision.retry" in world.events.types(), "the retry is an event, not a secret"
    assert world.budget.actual == 4_000


def test_an_uncertain_outcome_keeps_its_hold_and_is_not_asked_again():
    from boss_engine.services.typed_decision_spend import TypedDecisionReconciliationRequired

    world = _world()
    world.jev.fail = TimeoutError("read timed out")
    with pytest.raises(TypedDecisionReconciliationRequired):
        _decide(world)
    assert world.budget.active_reserved() == QUOTE, "it may have been charged"
    with pytest.raises(TypedDecisionReconciliationRequired):
        _decide(world)
    assert world.jev.calls == 1 and world.decisions.rows


def test_no_answer_to_the_lead_question_settles_as_failed_with_its_cost():
    world = _world()
    world.jev.answers["route_tier"] = (None, None)
    with pytest.raises(TypedDecisionSpendError, match="no usable answer"):
        _decide(world)
    assert world.jev.calls == 1
    assert "typed_decision.attempt_failed" in world.events.types()
    assert all(row["status"] != "in_progress" for row in world.receipts.rows.values())
    assert world.budget.active_reserved() == 0 and world.budget.actual == 4_000, (
        "a call that was made is paid for even when it answered nothing")
