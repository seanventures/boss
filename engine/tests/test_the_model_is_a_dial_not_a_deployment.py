"""Which model builds is a choice somebody makes, not a value frozen at deploy time.

An installation can have several subscriptions connected at once — the credentials are already
stored — and could still only use the one its containers were started with. Switching meant
editing an environment variable and recreating the containers, which makes "try this project on
Codex" an ops task and, in practice, means nobody tries.

`project_settings` already existed for exactly this shape of problem, in its own words: the
environment is "the right home for a secret and the wrong home for a dial somebody wants to try
at 20 and then at 5". The engine simply was not one of its knobs.
"""
from __future__ import annotations

import pytest

from boss_engine.services.project_settings import (
    KNOBS,
    UnknownKnob,
    _allowed_choices,
    coerce,
)


def test_the_choices_come_from_the_registry_not_a_list() -> None:
    """A hardcoded list drifts the moment a harness is added.

    The failure that causes is the worst kind: a provider the operator has connected and
    cannot select, with nothing saying why.
    """
    from boss_engine.services.harness_registry import available_harnesses

    offered = _allowed_choices(KNOBS["code_agent_engine"])
    registered = sorted(h["name"] for h in available_harnesses())

    assert offered == registered
    assert "codex" in offered and "claude_code" in offered


def test_a_harness_can_be_chosen_by_name() -> None:
    assert coerce("code_agent_engine", "codex") == "codex"
    assert coerce("code_review_engine", "claude_code") == "claude_code"


def test_the_name_is_normalised() -> None:
    """Somebody typing into a console adds spaces and capitals."""
    assert coerce("code_agent_engine", "  CODEX ") == "codex"


def test_a_name_BOSS_cannot_drive_is_refused_with_the_alternatives() -> None:
    """"Invalid value" sends somebody to the source to learn what is valid."""
    with pytest.raises(ValueError) as exc:
        coerce("code_agent_engine", "gpt5")

    message = str(exc.value)
    assert "gpt5" in message
    assert "codex" in message and "claude_code" in message


def test_an_unknown_knob_is_still_refused() -> None:
    """The allowlist is the point: a settings table that accepts anything collects typos."""
    with pytest.raises(UnknownKnob):
        coerce("code_agent_engien", "codex")


def test_the_builder_and_the_reviewer_are_separately_choosable() -> None:
    """A reviewer that is the same model rarely disagrees with itself."""
    assert KNOBS["code_agent_engine"]["attr"] == "code_agent_engine"
    assert KNOBS["code_review_engine"]["attr"] == "code_review_engine"


def test_the_pipeline_layers_the_project_choices_over_the_deployment() -> None:
    """Structural: the knob is worthless if the run still reads the environment alone.

    The project's choices are layered onto `settings` ONCE, before any engine is picked, so
    every `settings.*_engine` read — here and in the functions this calls — sees them.
    Threading a per-key lookup through each read instead would leave the ones that got missed
    quietly using the deployment default while the console showed the operator's choice.
    """
    import inspect

    from boss_engine.services import governed_code_task_service as gcts

    source = inspect.getsource(gcts)
    build_at = source.find("build_engine = (")
    assert build_at != -1

    window = source[max(0, build_at - 800):build_at]
    assert "_applied_settings" in window, (
        "the run still takes the engine from the environment alone")
    assert "settings = _applied_settings(" in window, (
        "the layered settings are computed but not used")


def test_the_environment_remains_the_default(monkeypatch) -> None:
    """A project that has chosen nothing must behave exactly as it did before this existed."""
    from boss_engine.config import Settings
    from boss_engine.services.project_settings import effective

    settings = Settings().model_copy(update={"code_agent_engine": "claude_code"})

    # No connection and no project: `effective` must fall through rather than raise.
    assert effective(None, None, settings, "code_agent_engine") == "claude_code"
