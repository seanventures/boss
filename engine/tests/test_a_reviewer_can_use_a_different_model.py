"""Choosing a MODEL for a lane, not only a harness.

The reviewer setting exists because "a reviewer that is the same model rarely disagrees with
itself". Until the lane model existed, that is exactly what it produced: the lanes chose a
harness and the CLASSES chose a model, so picking claude_code as the reviewer when the builder
was also claude_code gave the same model on both sides. The setting could express the intent
and not the thing the intent was for.
"""
import pytest

from boss_engine.services import project_settings as ps
from boss_engine.services.pipeline_profiles import materialize_route


REVIEW = {"lane": "review", "model_class": "strong"}
BUILD = {"lane": "build", "model_class": "strong"}
ROUTINE = {"lane": "build", "model_class": "low_cost"}


def _route(route, **kwargs):
    return materialize_route(route, build_engine="claude_code", review_engine="claude_code",
                             **kwargs)


def test_the_reviewer_can_now_run_a_different_model_from_the_builder():
    """The whole point. Same harness on both sides, different model."""
    build_engine, build_model = _route(BUILD, strong_model="sonnet", review_model="opus")
    review_engine, review_model = _route(REVIEW, strong_model="sonnet", review_model="opus")
    assert (build_engine, review_engine) == ("claude_code", "claude_code")
    assert build_model == "sonnet"
    assert review_model == "opus", "the reviewer must not inherit the builder's model"


def test_the_lane_model_beats_the_class_model_and_that_order_is_the_point():
    """Review steps are classed `strong`.

    So if the class model won, setting a reviewer model would do nothing in the one case it was
    added for — the operator would set Opus on the reviewer card and get whatever
    code_agent_strong_model said.
    """
    _, model = _route(REVIEW, strong_model="haiku", review_model="opus")
    assert model == "opus"


def test_an_explicit_per_step_pin_still_wins_over_both():
    """model_config is the operator naming a model for one step. Nothing outranks that."""
    _, model = _route({**REVIEW, "model": "fable"}, strong_model="haiku", review_model="opus")
    assert model == "fable"


def test_an_empty_lane_model_changes_nothing_at_all():
    """The knob is new and every existing install has it unset.

    If this test fails, the change rewrote the routing of every project that never asked for it.
    """
    for route in (REVIEW, BUILD, ROUTINE):
        before = _route(route, strong_model="opus", low_cost_model="haiku")
        after = _route(route, strong_model="opus", low_cost_model="haiku",
                       review_model="", build_model="")
        assert before == after, route


def test_the_build_lane_model_does_not_leak_into_review_or_the_other_way():
    _, review = _route(REVIEW, build_model="haiku")
    assert review is None, "a build-lane model must not decide what reviews the code"
    _, build = _route(BUILD, review_model="opus")
    assert build is None


# --- what the console offers ------------------------------------------------------------------

class _Settings:
    def __init__(self, **kw):
        self.code_agent_engine = kw.get("code_agent_engine", "claude_code")
        self.code_review_engine = kw.get("code_review_engine", "")


def test_a_model_picker_offers_the_models_of_the_harness_that_lane_runs():
    """A hardcoded Claude list was shown for every model knob.

    So an operator reviewing with codex was offered four Claude names, none of which codex
    accepts — and choosing one failed later, inside a build, looking like a bad model.
    """
    spec = ps.KNOBS["code_review_model"]
    codex = ps.offered_choices(None, _Settings(code_review_engine="codex"), "p", spec)
    assert "gpt-5.2-codex" in codex
    assert "opus" not in codex, "codex does not accept Claude's model names"

    claude = ps.offered_choices(None, _Settings(code_review_engine="claude_code"), "p", spec)
    assert {"opus", "fable", "sonnet", "haiku"} <= set(claude)
    assert "gpt-5.2-codex" not in claude


def test_an_empty_reviewer_follows_the_implementer_because_that_is_what_empty_means():
    """Empty reviewer = the implementer reviews its own work, so its models are the right list."""
    spec = ps.KNOBS["code_review_model"]
    offered = ps.offered_choices(
        None, _Settings(code_agent_engine="codex", code_review_engine=""), "p", spec)
    assert "gpt-5.2-codex" in offered


def test_a_codex_model_name_boss_has_never_heard_of_is_still_accepted():
    """`codex --model` takes any name OpenAI offers.

    A closed list would reject a model that works the week after it ships, and the operator
    would read "BOSS cannot drive that" about a model their own CLI runs fine.
    """
    assert ps.coerce("codex_cli_model", "gpt-6-codex-preview") == "gpt-6-codex-preview"
    assert ps.model_choice_is_open(ps.KNOBS["codex_cli_model"], "codex") is True


def test_claude_s_list_is_closed_because_boss_resolves_those_names_itself():
    """opus/sonnet/fable/haiku are aliases BOSS maps. A name outside the set resolves to nothing."""
    assert ps.model_choice_is_open(ps.KNOBS["code_review_model"], "claude_code") is False


def test_the_codex_model_is_reachable_from_the_console_at_all():
    """It was wired into codex_cli_adapter and offered by nothing.

    An operator could choose codex as the harness and had no way to choose its model — the
    "built and unwired" shape this settings file exists to stop.
    """
    assert "codex_cli_model" in ps.KNOBS
    assert ps.KNOBS["codex_cli_model"]["attr"] == "codex_cli_model"


def test_every_model_knob_points_at_a_real_settings_attribute():
    """A knob whose attr does not exist stores a value nothing ever reads."""
    from boss_engine.config import Settings

    for key, spec in ps.KNOBS.items():
        if "model" in key:
            assert spec["attr"] in Settings.model_fields, f"{key} -> {spec['attr']}"
