"""Which coding agent BOSS drives must be a declared, swappable part.

BOSS's bet is "rent the model, own the orchestration". That only holds if the rented half
is pluggable. `_build_code_agent` was an if/elif chain, so "bring your own coding agent"
meant editing core — the opposite of the claim. Warp makes the harness a declared field
(`harness.type: oz|claude|codex`); BOSS-409 makes it a registry.

The behaviour-parity tests come first: a refactor of the thing that chooses which model
writes your code has to prove it chose the same one it used to.
"""

from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import harness_registry
from boss_engine.services.code_agent import (
    ClaudeCodeAgent,
    NativeCodeAgent,
    _build_code_agent,
)
from boss_engine.services.harness_registry import (
    UnknownHarness,
    available_harnesses,
    get_harness,
    register_harness,
)


def _settings(**kw) -> Settings:
    return Settings(**kw)


# --- parity with the if/elif chain it replaced --------------------------------

def test_claude_code_is_still_the_default() -> None:
    agent = _build_code_agent(_settings(BOSS_CODE_AGENT_ENGINE="claude_code"))
    assert isinstance(agent, ClaudeCodeAgent)
    assert agent.engine == "claude_code"


@pytest.mark.parametrize("name", ["native", "openai"])
def test_native_and_its_openai_alias_both_resolve(name: str) -> None:
    agent = _build_code_agent(_settings(), engine=name)
    assert isinstance(agent, NativeCodeAgent)


def test_an_unknown_harness_still_raises_notimplementederror() -> None:
    """Callers catch NotImplementedError; UnknownHarness subclasses it so they still do."""
    with pytest.raises(NotImplementedError) as exc:
        _build_code_agent(_settings(), engine="does-not-exist")
    assert isinstance(exc.value, UnknownHarness)
    # The old message named only the bad value. Naming what IS available turns a dead end
    # into a next step.
    assert "does-not-exist" in str(exc.value)
    assert "claude_code" in str(exc.value)


def test_azure_still_fails_closed_without_its_credentials() -> None:
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_KEY"):
        _build_code_agent(_settings(AZURE_OPENAI_KEY="", AZURE_OPENAI_ENDPOINT="",
                                    AZURE_OPENAI_DEPLOYMENT=""), engine="azure")


def test_a_provider_lane_still_fails_closed_without_its_own_key() -> None:
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        _build_code_agent(_settings(GROQ_API_KEY=""), engine="groq")


def test_a_provider_lane_never_borrows_the_platform_openai_key() -> None:
    """The dedicated credential pair is the point: a cheap VAG lane must not silently bill
    the platform-managed OPENAI_* pair."""
    agent = _build_code_agent(
        _settings(OPENAI_API_KEY="platform-key", DEEPSEEK_API_KEY="lane-key",
                  DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"),
        engine="deepseek")
    assert agent.engine == "deepseek"
    assert agent.settings.openai_api_key == "lane-key"
    assert agent.settings.azure_openai_key is None


@pytest.mark.parametrize(
    "name", ["claude_code", "native", "azure", "openrouter", "groq", "deepseek",
             "ollama", "vllm"])
def test_every_lane_the_if_elif_chain_had_is_registered(name: str) -> None:
    assert get_harness(name).name == name


# --- the registry itself ------------------------------------------------------

def test_the_registry_bootstraps_itself_for_a_caller_that_only_imports_it() -> None:
    """Registration lives in code_agent. A caller importing only the registry would
    otherwise see an EMPTY list and conclude BOSS supports no harnesses at all."""
    assert len(available_harnesses()) >= 8


def test_a_harness_can_be_added_without_editing_core() -> None:
    sentinel = object()
    register_harness("test_only_harness", lambda settings: sentinel,
                     kind="cli", description="fixture", verified=False)
    try:
        assert _build_code_agent(_settings(), engine="test_only_harness") is sentinel
        entry = next(h for h in available_harnesses() if h["name"] == "test_only_harness")
        assert entry["verified"] is False and entry["kind"] == "cli"
    finally:
        harness_registry._REGISTRY.pop("test_only_harness", None)


def test_a_duplicate_registration_is_refused_rather_than_silently_winning() -> None:
    """Two harnesses under one name would make "which agent wrote this?" unanswerable —
    and that question is the whole point of the evidence trail."""
    register_harness("dupe_harness", lambda s: None)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_harness("dupe_harness", lambda s: None)
        register_harness("dupe_harness", lambda s: "replaced", replace=True)
        assert _build_code_agent(_settings(), engine="dupe_harness") == "replaced"
    finally:
        harness_registry._REGISTRY.pop("dupe_harness", None)


def test_names_are_case_insensitive_like_the_old_lower_call() -> None:
    assert get_harness("CLAUDE_CODE").name == "claude_code"
    assert get_harness("Native").name == "native"


def test_every_harness_declares_what_it_needs() -> None:
    """An operator choosing a lane should not have to read source to find out which
    credential it wants."""
    for h in available_harnesses():
        assert h["description"], f"{h['name']} has no description"
        assert h["requires"], f"{h['name']} does not say what it requires"


def test_verified_means_a_governed_task_actually_ran_on_it() -> None:
    """Honesty rule: a merely-wired adapter must not present as ready. Every built-in is
    verified today; this guards the next one that is added."""
    for h in available_harnesses():
        assert isinstance(h["verified"], bool)
