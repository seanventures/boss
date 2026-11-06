"""A workflow typed decision may call a provider only inside its receipt.

Claude CLI, the worker's OpenAI fallback, TypeSafe, and a second opinion each
have to use the spend path's job lease. A retry or fallback is another receipt.
A hidden retry on the first key is a second spend.
"""
from __future__ import annotations

import shutil
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from boss_engine.config import Settings
from boss_engine.services import openai_adapter
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.services.jobs import JobDeferredError
from boss_engine.services.typed_decision_spend import (
    TypedDecisionReconciliationRequired,
    TypedDecisionSpendError,
    governed_decider,
)
from boss_engine.services.typed_decisions import TypeSafeAuthenticationRefused
from engine.tests.test_typed_decision_spend import (
    QUOTE,
    World,
    _binding,
    _jev_then_claude,
    _settings,
)


def _quiet_sleep(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)


def _no_cli(monkeypatch) -> None:
    monkeypatch.setattr(ClaudeCliAdapter, "_claude_bin", lambda self: "claude")
    monkeypatch.setattr(shutil, "which", lambda name: None)


def _openai_settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY="owned-typed-decision-test",
        OPENAI_BASE_URL="",
        AZURE_OPENAI_KEY="",
        AZURE_OPENAI_ENDPOINT="",
        AZURE_OPENAI_DEPLOYMENT="",
        BOSS_DECISION_BACKEND="claude",
    )


def test_budget_or_lease_denial_does_not_call_the_provider():
    """The break: reserve or lease_owned_job stops refusing, and decide() runs."""
    denied = World(cap=1_000)
    calls = []

    class Counting:
        source_id = "openai_api"
        backend = "openai"

        def decide(self, context, questions, precedent=""):
            calls.append(context)
            raise AssertionError("provider ran after a budget denial")

    spend = denied.spend()
    spend.decider_factory = lambda role, settings: Counting()
    with pytest.raises(TypedDecisionSpendError, match="Budget cap exceeded"):
        spend.execute(_settings(), _binding(), "My card was charged twice")

    leased = World()

    def deny_lease(*args, **kwargs):
        raise JobDeferredError("red_halts_non_p0_work")

    leased.jobs.lease_owned_job = deny_lease
    spend = leased.spend()
    spend.decider_factory = lambda role, settings: Counting()
    with pytest.raises(TypedDecisionSpendError, match="red_halts_non_p0_work"):
        spend.execute(_settings(), _binding(), "My card was charged twice")
    assert calls == []
    assert denied.budget.active_reserved() == 0
    assert leased.budget.active_reserved() == 0


def test_openai_worker_fallback_does_not_retry_on_the_same_receipt(monkeypatch):
    """The break: a connection error sends another OpenAI request on one receipt."""
    _quiet_sleep(monkeypatch)
    _no_cli(monkeypatch)
    constructed, calls = [], []

    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise APIConnectionError(request=httpx.Request("POST", "https://owned.invalid"))

    class FakeClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.responses = Responses()

    monkeypatch.setattr(openai_adapter, "OpenAI", FakeClient)
    world = World()
    spend = world.spend()
    settings = _openai_settings()
    spend.decider_factory = lambda role, bound: governed_decider(bound, role)
    with pytest.raises(TypedDecisionReconciliationRequired):
        spend.execute(settings, _binding(), "My card was charged twice")
    assert len(calls) == 1
    assert constructed and constructed[0].get("max_retries") == 0
    assert len(world.receipts.rows) == 1
    assert world.budget.active_reserved() == QUOTE
    assert "typed_decision.fallback" not in world.events.types()


def test_claude_login_recheck_does_not_start_a_second_process(monkeypatch):
    """The break: one 'Not logged in' starts another claude process on the same receipt."""
    _quiet_sleep(monkeypatch)
    monkeypatch.setattr(ClaudeCliAdapter, "_claude_bin", lambda self: "claude")
    monkeypatch.setattr(shutil, "which", lambda name: "/owned/claude")
    monkeypatch.setattr(
        "boss_engine.services.typed_decisions._claude_source",
        lambda settings: "anthropic_claude_code",
    )
    invocations = []

    def once(self, prompt, *, system_prompt=None):
        invocations.append(system_prompt)
        raise RuntimeError("claude CLI exited 1: Not logged in. Please run /login")

    monkeypatch.setattr(ClaudeCliAdapter, "_invoke_once", once)
    world = World()
    spend = world.spend()
    settings = Settings(_env_file=None, BOSS_DECISION_BACKEND="claude")
    spend.decider_factory = lambda role, bound: governed_decider(bound, role)
    with pytest.raises(TypedDecisionReconciliationRequired):
        spend.execute(settings, _binding(), "My card was charged twice")
    assert len(invocations) == 1
    assert len(world.receipts.rows) == 1
    assert world.budget.active_reserved() == QUOTE


def test_typesafe_rate_limit_is_one_request_on_the_receipt(monkeypatch):
    """The break: a 429 is posted again inside the same TypeSafe attempt."""
    _quiet_sleep(monkeypatch)
    posts = []

    class FakeClient:
        def __init__(self, timeout=30):
            self.timeout = timeout

        def post(self, url, json=None, headers=None):
            posts.append(url)
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow"})

        def close(self):
            return None

    monkeypatch.setattr(httpx, "Client", FakeClient)
    world = World()
    spend = world.spend()
    settings = Settings(
        _env_file=None,
        TYPESAFE_API_KEY="owned-typesafe-test",
        BOSS_DECISION_BACKEND="typesafe",
    )
    spend.decider_factory = lambda role, bound: governed_decider(bound, role)
    with pytest.raises(TypedDecisionReconciliationRequired):
        spend.execute(settings, _binding(), "My card was charged twice")
    assert posts == ["https://api.typesafe.ai/v1/systemone"]
    assert len(world.receipts.rows) == 1
    assert world.gateway.acquired_for == [("typesafe_jev", "workflow")]


def test_second_opinion_and_fallback_are_separate_receipts():
    """The break: the careful call is written onto the first attempt's dedupe key."""
    world = World()
    spend, jev, claude = _jev_then_claude(world)
    jev.confidence = 0.2
    settings = SimpleNamespace(
        decision_backend="typesafe", decision_model="opus", decision_fallback_below=0.7,
    )
    result = spend.execute(settings, _binding(), "My card was charged twice")
    keys = sorted(row["dedupe_key"] for row in world.receipts.rows.values())
    assert len(keys) == 2 and keys[0] != keys[1]
    assert keys[0].count(":second") == 0 and keys[1].endswith(":second")
    assert result["receipt_id"] != result["second_receipt_id"]
    assert (jev.calls, claude.calls) == (1, 1)
    assert world.budget.active_reserved() == 0
    assert "typed_decision.second_opinion" in world.events.types()
    assert world.events.types().count("typed_decision.settled") == 2

    again = World()
    spend, jev, claude = _jev_then_claude(again)
    jev.fail = TypeSafeAuthenticationRefused()
    fallback = spend.execute(_settings(), _binding(), "My card was charged twice")
    failed = [row for row in again.receipts.rows.values() if row["status"] == "failed"]
    succeeded = [row for row in again.receipts.rows.values() if row["status"] == "succeeded"]
    assert len(failed) == 1 and len(succeeded) == 1
    assert failed[0]["dedupe_key"] != succeeded[0]["dedupe_key"]
    assert succeeded[0]["dedupe_key"].endswith(":second")
    assert fallback["receipt_id"] == succeeded[0]["id"]
    assert (jev.calls, claude.calls) == (1, 1)
    assert "typed_decision.fallback" in again.events.types()
