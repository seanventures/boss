"""A model only ever runs on an engine of its own family — whatever mix of jobs the operator sets.

Observed on boss-1, 2026-11-03: Build was switched to Codex · gpt-6-sol while Routine work stayed on
Claude Code · sonnet (the operator moves work between a subscription and an API key several
times a day). Fixed mode took the ENGINE from the Routine (low-cost) lane and the MODEL from
Build and launched `claude --model gpt-6-sol`; four builds failed in three minutes with "There's
an issue with the selected model (gpt-6-sol)". These tests try every mix of engines across the
jobs and every lane, at each layer that pairs an engine with a model.
"""
from __future__ import annotations

import itertools

import pytest

from boss_engine.services.model_router.compat import fits, fitted, model_family
from boss_engine.services.model_router.router import RouteDecision
from boss_engine.services.model_router.store import _fit_model
from boss_engine.services.pipeline_profiles import materialize_route

ENGINES = {
    "claude_code": ("sonnet", "opus", "haiku", "claude-sonnet-5"),
    "codex": ("gpt-6-sol", "gpt-5.2-codex", "o3"),
    "gemini": ("gemini-3-pro",),
    "openrouter": ("anthropic/claude-opus-4.1", "qwen/qwen3-coder"),
}
CHOICES = [(e, m) for e, ms in ENGINES.items() for m in ("",) + ms]


def test_the_families():
    assert fits("claude_code", "sonnet") and fits("claude_code", "claude-opus-4-8")
    assert not fits("claude_code", "gpt-6-sol") and not fits("codex", "opus")
    assert fits("codex", "gpt-6-sol") and fits("codex", "o3")
    assert fits("openrouter", "anthropic/claude-opus-4.1"), "a gateway runs any provider"
    assert fits("claude_code", None) and fits("claude_code", ""), "no model = engine default"
    assert fits("claude_code", "my-custom-deployment"), "an unknown name is allowed"
    assert model_family("anthropic/claude-opus-4.1") == "anthropic"


def test_the_boss1_case_runs_claude_on_its_own_model():
    """Build = Codex · gpt-6-sol, Routine work = Claude Code · sonnet, implement on the
    low-cost lane: the exact routing that failed four builds."""
    engine, model = materialize_route(
        {"model_class": "low_cost", "lane": "build"}, build_engine="codex",
        review_engine="claude_code", low_cost_engine="claude_code", low_cost_model="sonnet",
        strong_engine="claude_code", strong_model="opus", build_model="gpt-6-sol",
        review_model="sonnet")
    assert (engine, model) == ("claude_code", "sonnet")


def test_a_lane_whose_engine_runs_the_step_keeps_its_model():
    engine, model = materialize_route(
        {"model_class": "low_cost", "lane": "build"}, build_engine="codex",
        review_engine="claude_code", low_cost_engine="codex", low_cost_model="",
        build_model="gpt-6-sol")
    assert (engine, model) == ("codex", "gpt-6-sol")


def test_the_reviewer_model_still_beats_the_class_model_on_the_same_engine():
    """The reason the lane model wins (the reviewer card's model) still holds."""
    engine, model = materialize_route(
        {"model_class": "strong", "lane": "review"}, build_engine="claude_code",
        review_engine="claude_code", strong_engine="claude_code", strong_model="opus",
        review_model="sonnet")
    assert (engine, model) == ("claude_code", "sonnet")


@pytest.mark.parametrize("cls,lane", [("low_cost", "build"), ("strong", "build"),
                                      ("strong", "review"), (None, "build"), (None, "review")])
def test_every_mix_of_jobs_gives_a_runnable_pair(cls, lane):
    route = {"model_class": cls, "lane": lane} if cls else {"lane": lane}
    bad = []
    for (be, bm), (re_, rm), (le, lm), (se, sm) in itertools.product(
            CHOICES[::2], CHOICES[1::3], CHOICES[::3], CHOICES[1::2]):
        engine, model = materialize_route(
            route, build_engine=be, review_engine=re_, low_cost_engine=le,
            low_cost_model=lm, strong_engine=se, strong_model=sm, build_model=bm,
            review_model=rm)
        if not fits(engine, model):
            bad.append((be, bm, re_, rm, le, lm, se, sm, engine, model))
    assert not bad, f"{len(bad)} impossible pairs, e.g. {bad[:3]}"


def _decision(engine, model, tier="balanced"):
    return RouteDecision(id="route_x", mode="smart", kind="build_stage", step="implement",
                         tier=tier, engine=engine, model=model, reason="Balanced · picked",
                         why=("picked",))


def test_a_route_naming_another_engines_model_is_corrected_and_says_why():
    fixed = _fit_model(_decision("claude_code", "gpt-6-sol"))
    assert (fixed.engine, fixed.model) == ("claude_code", "sonnet")
    assert "model_fit" in fixed.rules and "gpt-6-sol" in fixed.why[-1]
    assert "cannot run on claude_code" in fixed.reason


def test_a_route_on_an_engine_without_tier_names_falls_back_to_its_default():
    fixed = _fit_model(_decision("codex", "opus", tier="frontier"))
    assert (fixed.engine, fixed.model) == ("codex", None)


def test_a_route_that_fits_is_untouched():
    d = _decision("codex", "gpt-6-sol")
    assert _fit_model(d) is d


def test_fitted_never_returns_a_mismatch():
    for engine in ENGINES:
        for other in ENGINES.values():
            for model in other:
                for tier in ("fast", "balanced", "frontier", None):
                    got, _ = fitted(engine, model, tier)
                    assert fits(engine, got), (engine, model, tier, got)


def test_the_cli_launchers_never_get_another_engines_model():
    from boss_engine.services import cli_harness

    for name, wrong, right in (("claude_code", "gpt-6-sol", "sonnet"),
                               ("codex", "opus", "gpt-6-sol")):
        agent = cli_harness.GenericCliCodeAgent.__new__(cli_harness.GenericCliCodeAgent)
        agent.spec = cli_harness.BUILTIN_CLI_HARNESSES[name]
        agent._resolve_binary = lambda: "cli"
        assert wrong not in agent._argv("hi", read_only=True, model=wrong), name
        argv = agent._argv("hi", read_only=True, model=right)
        assert argv[argv.index(agent.spec.model_flag) + 1] == right, name
