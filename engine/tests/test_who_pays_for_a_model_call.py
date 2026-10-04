"""Who pays for a model call is decided in one place, and every settlement asks it.

boss-1 (2026-10-03): a Claude Code and a Codex subscription, no API key, and a $10 budget that
went red on "actual" spend nobody paid — the Claude CLI's `total_cost_usd` (list price, reported
on a subscription too) settled as money everywhere a call was settled. included_spend.py decides
whether a call is money; these pin the deciders and the rule that every settlement asks them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import included_spend
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.services.codex_cli_adapter import CodexCliAdapter
from boss_engine.services.llm_telemetry import engine_call_is_included
from boss_engine.services.model_failures import budget_hold_sentence, explain
from boss_engine.services.model_routing import ModelChoice, turn_is_included
from boss_engine.services.openai_adapter import OpenAIAdapter

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"


def test_entitlement_kinds_that_are_not_money():
    for kind in ("subscription", "licensed_seat", "local_capacity"):
        assert included_spend.source_is_included({"entitlement_kind": kind})
    for kind in ("usage_based_api", "api_key_capacity", "hosted_usage_based_gateway", ""):
        assert not included_spend.source_is_included({"entitlement_kind": kind})
    assert not included_spend.source_is_included(None)
    assert included_spend.split(250_000, True) == (0, 250_000)
    assert included_spend.split(250_000, False) == (250_000, None)


def test_the_claude_cli_says_subscription_on_its_login_and_metered_on_a_key(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t" * 12)    # the login, not a store lookup
    data = {"usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.25,
            "modelUsage": {"claude-sonnet-4-5": {}}}
    adapter = ClaudeCliAdapter(Settings(claude_code_oauth_token="t" * 12))
    adapter._child_env()
    usage = adapter._usage(data)
    assert usage["billing"] == "subscription"
    assert usage["cost_usd_micros"] == 250_000, "the list-price value is still reported"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k" * 12)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential",
                        lambda *a, **k: None)
    keyed = ClaudeCliAdapter(Settings(claude_code_oauth_token=""))
    env = keyed._child_env()
    expected = "metered" if env.get("ANTHROPIC_API_KEY") else "subscription"
    assert keyed._usage(data)["billing"] == expected


def test_codex_and_local_models_are_included_hosted_endpoints_are_money():
    codex = CodexCliAdapter(Settings())
    assert codex._usage({"stdout": ""})["billing"] == "subscription"
    for provider, billing in (("ollama", "local"), ("vllm", "local"),
                              ("openai", "metered"), ("openrouter", "metered")):
        usage: dict = {}
        OpenAIAdapter(Settings(), provider=provider)._stamp_billing(usage)
        assert usage["billing"] == billing, provider


@pytest.mark.parametrize("engine,usage,included", [
    ("claude_code", None, True),
    ("codex", {}, True),
    ("ollama", None, True),
    ("native", None, False),
    ("claude_code", {"billing": "metered"}, False),   # the adapter's word wins
    ("native", {"billing": "local"}, True),
])
def test_the_router_and_the_ledger_agree_on_who_pays(engine, usage, included):
    assert turn_is_included(ModelChoice(engine, "t"), usage) is included
    assert engine_call_is_included(engine, usage) is included


def test_held_work_is_said_as_held_with_the_numbers():
    sentence = budget_hold_sentence("red", 0.9, 10_000_000)
    assert sentence.startswith("Held — the budget is red (90% of $10 used)")
    said = explain(f"{sentence} (job discovery.run, priority p2, risk 0; red_halts_non_p0_work)")
    assert said["kind"] == "budget_hold"
    assert said["headline"] == "Held — the budget is red (90% of $10 used), so only P0 work runs."
    assert said["href"] == "/?area=budget"
    # The old wording, still on jobs deferred before this change, reads the same way.
    old = explain("Budget state red deferred job discovery.run (priority p2, risk 0).")
    assert old["kind"] == "budget_hold" and "no worker" not in old["headline"].lower()


#: The names a model's reported cost travels under to a settlement.
_REPORTED_COST_NAMES = {"worth", "actual_cost", "cost", "actual_cost_usd_micros",
                        "reported_actual"}


def _settle_calls_from_usage(tree: ast.AST):
    """Every `.settle(` / `.consume(` call whose cost argument is `estimate_cost_usd_micros(...)`
    — a cost read off a model's usage, the one that is list price on a subscription."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("settle", "consume")):
            continue
        priced = any(isinstance(arg, ast.Call) and getattr(arg.func, "id", "")
                     == "estimate_cost_usd_micros" for arg in node.args)
        names = {getattr(arg, "id", "") for arg in node.args}
        names |= {getattr(arg.args[0], "id", "") for arg in node.args
                  if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "int"
                  and arg.args}
        if priced or _REPORTED_COST_NAMES & names:
            yield node


def test_the_scan_sees_every_door_that_settles_a_model_cost():
    found = {path.name for path in SERVICES.glob("*.py")
             for _ in _settle_calls_from_usage(ast.parse(path.read_text(encoding="utf-8")))}
    assert {"model_gateway.py", "model_routing.py", "skill_runtime.py", "discovery_service.py",
            "meeting_service.py", "intent_router.py"} <= found, found


def test_every_settlement_of_a_model_cost_says_who_paid():
    """THE CLASS, NOT THE INSTANCE. A settlement that books a model's reported cost without
    saying whether a subscription answered is how list price became money; each one passes
    `included_in_subscription` (included_spend.py)."""
    offenders = []
    for path in sorted(SERVICES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _settle_calls_from_usage(tree):
            if not any(k.arg == "included_in_subscription" for k in call.keywords):
                offenders.append(f"{path.name}:{call.lineno}")
    assert not offenders, (
        "these settle a model's reported cost without saying whether it was money: "
        + ", ".join(offenders))
