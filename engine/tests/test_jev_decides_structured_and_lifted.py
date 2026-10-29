"""BOSSAPP-579 is done; this change lifts the leftover router hold and lets structured
decisions (the run supervisor, L2a) use Jev for the choice.

Covers: jev_state reads "ready"/"absent" with no held state and no env-var-only gate (it also
honours a key stored through Setup's vault, not just the environment variable); a structured
decision (argument-schema candidates) asks Jev for the action and records `by.backend`; low
confidence still falls back to the second-opinion/routed-model path; the router view never
shows "paused".
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services.model_router.classifier import jev_state


def _settings(**overrides):
    base = dict(decision_backend="typesafe", typesafe_api_key="", decision_fallback_below=0.0)
    base.update(overrides)
    return SimpleNamespace(**base)


# P0 — the leftover hold is gone: jev_state is ready/absent only, never "held", and a vault
# key (not just the env var) counts.
def test_jev_state_has_no_held_state_and_honours_a_vault_held_key(monkeypatch):
    from boss_engine.services import typed_decisions

    assert not hasattr(typed_decisions, "GOVERNANCE_HOLD") or jev_state(
        _settings(typesafe_api_key="")) != "held"

    monkeypatch.setattr(typed_decisions, "stored_credential",
                        lambda settings, name, *a, **k: "vault-held-key", raising=False)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential",
                        lambda settings, name, *a, **k: "vault-held-key", raising=False)

    ready_settings = _settings(typesafe_api_key="")
    assert jev_state(ready_settings) == "ready", (
        "a key stored through Setup's vault must make Jev ready with no env var set")


# P0 — no key at all: absent, and it stays absent whatever a leftover live/hold flag says.
def test_jev_state_is_absent_with_no_key_anywhere(monkeypatch):
    from boss_engine.services import typed_decisions

    monkeypatch.setattr(typed_decisions, "stored_credential",
                        lambda settings, name, *a, **k: None, raising=False)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential",
                        lambda settings, name, *a, **k: None, raising=False)

    settings = _settings(typesafe_api_key="", router_jev_live=True)
    assert jev_state(settings) == "absent"


# P1 — the router view never shows the removed "paused" state for Jev.
def test_router_view_classifier_jev_field_never_says_paused():
    from boss_engine.services.model_router import view

    assert "paused" not in view.BACKEND_LABELS.values()
    for value, label in view.BACKEND_LABELS.items():
        assert label.lower() != "jev · paused"


# P1 — a structured decision (candidates carry argument schemas, as the run supervisor's do)
# asks Jev for the CHOICE and records by.backend, instead of only the routed chat model.
def test_structured_decision_asks_jev_for_the_choice_and_records_backend(monkeypatch):
    from boss_engine.services import agent_decision
    from boss_engine.services.agent_decision import Candidate

    monkeypatch.setattr(agent_decision, "_jev_ready", lambda settings: True)

    candidates = [
        Candidate(key="steer", means="steer the next round",
                 arguments={"properties": {"note": {"type": "string",
                                                     "description": "what to try"}}}),
        Candidate(key="stop_report", means="stop and report",
                 arguments={"properties": {"summary": {"type": "string",
                                                        "description": "what happened"}}}),
    ]

    class FakeJevDecider:
        backend = "typesafe"

        def decide(self, context, questions):
            from boss_engine.services.typed_decisions import Decision, DecisionBatch

            return DecisionBatch(
                decisions={q.key: Decision("steer", 0.93, "clear signal") for q in questions},
                backend="typesafe", model="jev-1", latency_ms=5, cost_usd_micros=10)

    class FakeAdapter:
        def create_text_response(self, prompt, system_prompt=""):
            return '{"action": "steer", "confidence": 0.9, "reason": "", ' \
                   '"arguments": {"note": "narrow the scope"}, "evidence": []}'

    decision = agent_decision.decide(
        connection=None, settings=_settings(), project_id="proj1",
        situation="{agent} watches a run", message="Run abc has stopped making progress.",
        candidates=candidates, agent="boss run supervisor", ref="run_abc",
        decider=FakeJevDecider(), adapter=FakeAdapter())

    assert decision.decided
    assert decision.action == "steer"
    assert decision.by.get("backend") == "typesafe", (
        "a structured decision must record that Jev, not the routed model, chose the action")
    assert decision.arguments.get("note") == "narrow the scope", (
        "the routed model still fills the chosen action's arguments")


# P1 — below the fallback threshold, the second-opinion path is used instead of Jev alone (the
# existing SecondOpinionDecider), so a low-confidence choice still gets a careful re-check.
def test_low_confidence_structured_choice_falls_back_to_second_opinion(monkeypatch):
    from boss_engine.services import agent_decision
    from boss_engine.services.agent_decision import Candidate
    from boss_engine.services.typed_decisions import Decision, DecisionBatch, SecondOpinionDecider

    monkeypatch.setattr(agent_decision, "_jev_ready", lambda settings: True)

    candidates = [
        Candidate(key="steer", means="steer the next round",
                 arguments={"properties": {"note": {"type": "string", "description": "x"}}}),
        Candidate(key="stop_report", means="stop and report",
                 arguments={"properties": {"summary": {"type": "string", "description": "x"}}}),
    ]

    class FastUnsure:
        backend = "typesafe"

        def decide(self, context, questions):
            return DecisionBatch(
                decisions={q.key: Decision("steer", 0.2, "unsure") for q in questions},
                backend="typesafe", model="jev-1", latency_ms=5, cost_usd_micros=10)

    class CarefulAdapter:
        backend = "model"

        def create_text_response(self, prompt, system_prompt=""):
            return '{"action": "stop_report", "confidence": 0.95, "reason": "", ' \
                   '"arguments": {"summary": "no progress"}, "evidence": []}'

        def create_chat_completion(self, messages, tools=None):
            return {"content": '{"action": "stop_report", "confidence": 0.95, "reason": "", '
                                '"arguments": {"summary": "no progress"}, "evidence": []}'}, {}

    class CarefulDecider:
        backend = "claude"

        def decide(self, context, questions):
            return DecisionBatch(
                decisions={q.key: Decision("stop_report", 0.9, "clear on review")
                          for q in questions},
                backend="claude", model="claude-x", latency_ms=5, cost_usd_micros=4)

    second_opinion = SecondOpinionDecider(FastUnsure(), CarefulDecider(), below=0.5)

    decision = agent_decision.decide(
        connection=None, settings=_settings(), project_id="proj1",
        situation="{agent} watches a run", message="Run abc has stopped making progress.",
        candidates=candidates, agent="boss run supervisor", ref="run_abc",
        decider=second_opinion, adapter=CarefulAdapter())

    assert decision.decided
    assert decision.action == "stop_report", (
        "a low-confidence Jev answer must be overruled by the careful second opinion")
    assert "claude" in decision.by.get("backend", ""), (
        "the fallback's backend must show it, not just the fast one"
    )
