"""BOSSAPP-1025: a model pin belongs to the engine it was given with.

A task-level pin was copied onto every step. Review and test arbitration
resolve on the review lane, so they ran claude_code with model local-llama.
A stage whose engine is not the pin's engine drops the pin. The escalated
hard-problems lane must not inherit the previous engine's model either.
"""
from __future__ import annotations

from types import SimpleNamespace

from boss_engine.services.model_router.stages import StageRouter
from boss_engine.services.pipeline_profiles import (
    PROFILES,
    materialize_route,
    resolve_profile,
    route_for_step,
)


def _settings(**kw):
    base = dict(code_agent_strong_engine="", code_agent_low_cost_engine="",
                code_agent_strong_model="", code_agent_low_cost_model="",
                code_agent_model="", code_review_model="")
    base.update(kw)
    return SimpleNamespace(**base)


def _review(model_config, *, build_engine="ollama", review_engine="claude_code",
            review_model="opus"):
    route = route_for_step(PROFILES["balanced"], "review_code", model_config)
    return materialize_route(
        route, build_engine=build_engine, review_engine=review_engine,
        review_model=review_model, build_model="local-llama")


def test_review_does_not_run_claude_code_with_the_build_models_pin():
    engine, model = _review({"default": "local-llama"})
    assert engine == "claude_code"
    assert model != "local-llama"
    assert model == "opus"


def test_test_arbitration_is_the_review_route_and_drops_the_same_pin():
    """Arbitration calls `_route("review_code")`. The pin must already be gone."""
    route = route_for_step(
        PROFILES["balanced"], "review_code", {"default": "local-llama"})
    engine, model = materialize_route(
        route, build_engine="ollama", review_engine="claude_code",
        review_model="opus")
    assert (engine, model) == ("claude_code", "opus")


def test_the_build_step_keeps_the_model_on_the_engine_it_was_given_with():
    route = route_for_step(
        PROFILES["balanced"], "implement", {"default": "local-llama"})
    engine, model = materialize_route(
        route, build_engine="ollama", review_engine="claude_code",
        review_model="opus")
    assert (engine, model) == ("ollama", "local-llama")


def test_a_pin_that_names_its_engine_is_dropped_when_review_resolves_another():
    engine, model = _review({"default": {"engine": "ollama", "model": "local-llama"}})
    assert engine == "claude_code"
    assert model != "local-llama"


def test_a_review_step_pin_still_names_the_reviewers_model():
    route = route_for_step(
        PROFILES["balanced"], "review_code",
        {"default": "local-llama", "steps": {"review_code": "fable"}})
    engine, model = materialize_route(
        route, build_engine="ollama", review_engine="claude_code",
        review_model="opus")
    assert (engine, model) == ("claude_code", "fable")


def test_an_engine_only_task_override_still_moves_the_build_lane():
    route = route_for_step(
        PROFILES["balanced"], "implement", {"default": {"engine": "openai"}})
    engine, _model = materialize_route(
        route, build_engine="claude_code", review_engine="claude_code")
    assert engine == "openai"


def test_the_same_engine_on_both_lanes_may_keep_the_task_model():
    route = route_for_step(
        PROFILES["balanced"], "review_code", {"default": "haiku"})
    engine, model = materialize_route(
        route, build_engine="claude_code", review_engine="claude_code",
        review_model="opus")
    assert (engine, model) == ("claude_code", "haiku")


def test_escalation_does_not_bleed_the_prior_model_onto_another_engine():
    stages = StageRouter(
        transaction=None, settings=_settings(code_agent_strong_engine="claude_code"),
        project_id="p1", run_id="r1", spec={}, title="t",
        profile=resolve_profile("balanced"), build_engine="ollama",
        review_engine="claude_code", budget_state="green")
    legacy = stages.legacy(
        "implement", escalated=True,
        previous={"engine": "ollama", "model": "local-llama"})
    assert legacy.engine == "claude_code"
    assert legacy.model != "local-llama"
    assert not legacy.model


def test_escalation_keeps_the_prior_model_when_the_engine_does_not_change():
    stages = StageRouter(
        transaction=None, settings=_settings(),
        project_id="p1", run_id="r1", spec={}, title="t",
        profile=resolve_profile("balanced"), build_engine="ollama",
        review_engine="claude_code", budget_state="green")
    legacy = stages.legacy(
        "implement", escalated=True,
        previous={"engine": "ollama", "model": "local-llama"})
    assert (legacy.engine, legacy.model) == ("ollama", "local-llama")


def test_a_named_strong_model_still_wins_on_the_hard_problems_lane():
    stages = StageRouter(
        transaction=None,
        settings=_settings(code_agent_strong_engine="claude_code",
                           code_agent_strong_model="opus"),
        project_id="p1", run_id="r1", spec={}, title="t",
        profile=resolve_profile("balanced"), build_engine="codex",
        review_engine="codex", budget_state="green")
    legacy = stages.legacy(
        "implement", escalated=True, previous={"engine": "codex", "model": "gpt-5.2-codex"})
    assert (legacy.engine, legacy.model) == ("claude_code", "opus")
