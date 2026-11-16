"""BOSSAPP-1023: an Ollama name:tag is a model, not an engine.

Governed routing split every colon. `ollama:llama3.1:8b` was cut down to the
model `llama3.1:8b`, and that was split again into engine `llama3.1`, which
`get_harness` rejects as UnknownHarness. A prefix is an engine only when it is
a registered harness. Otherwise the whole string is the model.
"""
from __future__ import annotations

import pytest

from boss_engine.services.harness_registry import UnknownHarness, get_harness
from boss_engine.services.model_router.policy import PolicyError, _target
from boss_engine.services.model_router.router import _parse_pin
from boss_engine.services.pipeline_profiles import (
    PROFILES,
    _parse_route,
    materialize_route,
    route_for_step,
)


def test_a_name_tag_without_a_harness_prefix_stays_the_whole_model():
    for tag in ("llama3.1:8b", "qwen2.5-coder:7b", "llama3.2:latest"):
        parsed = _parse_route(tag)
        assert parsed["engine"] is None, tag
        assert parsed["model"] == tag


def test_ollama_with_a_name_tag_routes_on_the_ollama_harness():
    parsed = _parse_route("ollama:llama3.1:8b")
    assert (parsed["engine"], parsed["model"]) == ("ollama", "llama3.1:8b")
    route = route_for_step(
        PROFILES["balanced"], "implement", {"default": "ollama:llama3.1:8b"})
    engine, model = materialize_route(
        route, build_engine="claude_code", review_engine="claude_code")
    assert (engine, model) == ("ollama", "llama3.1:8b")
    get_harness(engine)  # UnknownHarness is the failure this ticket names


def test_the_tag_left_after_the_harness_is_split_off_is_still_a_model():
    """harness_bench keeps `ollama` and hands the governed default `llama3.1:8b`."""
    route = route_for_step(
        PROFILES["balanced"], "implement", {"default": "llama3.1:8b"})
    engine, model = materialize_route(
        route, build_engine="ollama", review_engine="ollama")
    assert engine == "ollama"
    assert model == "llama3.1:8b"
    with pytest.raises(UnknownHarness):
        get_harness("llama3.1")


def test_a_registered_harness_prefix_still_splits_once():
    assert _parse_route("native:deepseek-chat") == {
        "engine": "native", "model": "deepseek-chat", "model_class": None}
    assert _parse_route("claude_code:") == {
        "engine": "claude_code", "model": None, "model_class": None}
    assert _parse_route("openrouter:qwen/qwen3-coder") == {
        "engine": "openrouter", "model": "qwen/qwen3-coder", "model_class": None}
    assert _parse_route("ollama:llama3.1") == {
        "engine": "ollama", "model": "llama3.1", "model_class": None}
    assert _parse_route("deepseek/deepseek-chat") == {
        "engine": None, "model": "deepseek/deepseek-chat", "model_class": None}


def test_an_agent_pin_keeps_a_tagged_local_model_on_ollama():
    bare = _parse_pin("llama3.1:8b", "ollama")
    assert (bare.engine, bare.model) == ("ollama", "llama3.1:8b")
    full = _parse_pin("ollama:llama3.1:8b", None)
    assert (full.engine, full.model) == ("ollama", "llama3.1:8b")


def test_a_policy_target_does_not_invent_an_engine_from_a_model_tag():
    routed = _target("ollama:llama3.1:8b", "tiers.fast")
    assert (routed.engine, routed.model) == ("ollama", "llama3.1:8b")
    with pytest.raises(PolicyError):
        _target("llama3.1:8b", "tiers.fast")
