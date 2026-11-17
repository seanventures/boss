"""BOSSAPP-1344: an operator's job pin (Setup › Who does what) beats a profile lane.

Review pinned to codex · gpt-6-sol ran on claude_code · sonnet because the lean profile puts
review_code on the low-cost lane. A job the operator pinned decides every stage it covers, in
Fixed and in Smart; a job left on Auto keeps the profile's routes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services.model_router import store
from boss_engine.services.model_router.router import ROUTER
from boss_engine.services.model_router.stages import StageRouter, pin_for_step, setup_job_pins
from boss_engine.services.pipeline_profiles import (
    PROFILES, materialize_route, resolve_profile, route_for_step)

from ._router_fixtures import claude, metered, snap

REVIEW_STEPS = ("review_code", "security_check", "consistency_check")
BUILD_STEPS = ("write_tests", "implement")
PINS = {"review": ("codex", "gpt-6-sol"), "build": ("openrouter", "deepseek-v4")}


def _settings():
    return SimpleNamespace(code_agent_strong_engine="", code_agent_low_cost_engine="claude_code",
                           code_agent_strong_model="", code_agent_low_cost_model="sonnet",
                           code_agent_model="", code_review_model="")


def _router(profile, mode, pins, monkeypatch):
    sources = {"claude_code": claude(), "openrouter": metered("openrouter"),
               "codex": metered("codex")}
    monkeypatch.setattr(store, "route", lambda c, s, request, ledger=None: ROUTER.route(
        request, snap(sources=sources, mode=mode), ledger=ledger))
    import contextlib

    router = StageRouter(transaction=lambda: contextlib.nullcontext(None), settings=_settings(),
                         project_id="p1", run_id="r1", spec={"routing_mode": mode, "title": "t"},
                         profile=resolve_profile(profile), title="t",
                         build_engine="claude_code", review_engine="codex",
                         budget_state="green")
    router._pins = pins
    return router


@pytest.mark.parametrize("mode", ["fixed", "smart"])
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_a_pinned_review_and_build_decide_their_stages(profile, mode, monkeypatch):
    router = _router(profile, mode, PINS, monkeypatch)
    for step in REVIEW_STEPS:
        d = router.route(step)
        assert (d.engine, d.model) == ("codex", "gpt-6-sol"), (profile, mode, step, d.reason)
        assert "pinned in setup: review → codex · gpt-6-sol" in d.reason.lower()
    for step in BUILD_STEPS:
        d = router.route(step)
        assert (d.engine, d.model) == ("openrouter", "deepseek-v4"), (profile, mode, step)
        assert "build → openrouter · deepseek-v4" in d.reason.lower()


def _today(profile, step):
    """What the profile alone routes, computed from the same functions (the pre-pin answer)."""
    s = _settings()
    return materialize_route(
        route_for_step(resolve_profile(profile), step), build_engine="claude_code",
        review_engine="codex", low_cost_engine=s.code_agent_low_cost_engine,
        low_cost_model=s.code_agent_low_cost_model)


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_a_job_left_on_auto_keeps_the_profile_lane(profile, monkeypatch):
    for pins in ({}, {"build": PINS["build"]}):
        router = _router(profile, "fixed", pins, monkeypatch)
        legacy = router.legacy("review_code")
        assert "Setup" not in legacy.why
        assert (legacy.engine, legacy.model) == _today(profile, "review_code")
    only_review = _router(profile, "fixed", {"review": PINS["review"]}, monkeypatch)
    assert (only_review.legacy("implement").engine,
            only_review.legacy("implement").model) == _today(profile, "implement")


def test_the_lean_profile_no_longer_sends_a_pinned_review_to_sonnet(monkeypatch):
    router = _router("lean", "fixed", {"review": ("codex", "gpt-6-sol")}, monkeypatch)
    d = router.route("review_code")
    assert (d.engine, d.model) == ("codex", "gpt-6-sol")
    assert "lean profile" not in d.reason


def test_a_task_pin_is_still_above_the_job_pin(monkeypatch):
    router = _router("lean", "fixed", PINS, monkeypatch)
    router.spec["model_config"] = {"steps": {"implement": {"engine": "claude_code",
                                                           "model": "sonnet"}}}
    assert router.legacy("implement").engine == "claude_code"
    assert router.legacy("implement").why == "the task pins implement"


def test_a_pin_without_a_model_pins_the_engine_only(monkeypatch):
    router = _router("balanced", "smart", {"review": ("codex", None)}, monkeypatch)
    d = router.route("review_code")
    assert d.engine == "codex"


def test_the_hard_problems_pin_names_itself_on_escalation(monkeypatch):
    router = _router("balanced", "fixed", {"hard": ("codex", "gpt-6-sol")}, monkeypatch)
    legacy = router.legacy("implement", escalated=True)
    assert (legacy.engine, legacy.model) == ("codex", "gpt-6-sol")
    assert "Hard problems → codex · gpt-6-sol" in legacy.why


def test_pin_for_step_splits_jobs_by_step():
    assert pin_for_step(PINS, "review_code")[0] == "codex"
    assert pin_for_step(PINS, "implement")[0] == "openrouter"
    assert pin_for_step({"review": PINS["review"]}, "implement") is None


def test_only_a_knob_that_was_set_is_a_pin():
    """setup_job_pins reads the engine knob the operator wrote, not the lane's default."""
    class Conn:
        def begin_nested(self):
            import contextlib
            return contextlib.nullcontext()

    from boss_engine.services import project_settings

    original = project_settings.get_all
    try:
        project_settings.get_all = lambda c, p: {"code_review_engine": "codex",
                                                 "code_review_model": "gpt-6-sol",
                                                 "code_agent_engine": ""}
        assert setup_job_pins(Conn(), "p1") == {"review": ("codex", "gpt-6-sol")}
    finally:
        project_settings.get_all = original


def test_smart_escalation_runs_on_the_hard_problems_pin(monkeypatch):
    pins = {"build": ("claude_code", "sonnet"), "hard": ("codex", "gpt-6-sol")}
    router = _router("balanced", "smart", pins, monkeypatch)
    first = router.route("implement")
    assert (first.engine, first.model) == ("claude_code", "sonnet")
    d = router.route("implement", signal="final_attempt")
    assert d.escalation, d.reason
    assert (d.engine, d.model) == ("codex", "gpt-6-sol"), d.reason
    assert "hard problems → codex · gpt-6-sol" in d.reason.lower()


def test_smart_escalation_without_a_hard_pin_keeps_the_build_pin(monkeypatch):
    router = _router("balanced", "smart", {"build": ("claude_code", "sonnet")}, monkeypatch)
    d = router.route("implement", signal="final_attempt")
    assert (d.engine, d.model) == ("claude_code", "sonnet")


@pytest.mark.parametrize("mode", ["fixed", "smart"])
def test_a_model_in_the_profile_step_map_is_not_a_task_pin(mode, monkeypatch):
    custom = {**PROFILES["balanced"], "name": "custom",
              "model": {"default": "strong", "review_code": "claude_code:sonnet"}}
    monkeypatch.setitem(PROFILES, "custom", custom)
    router = _router("custom", mode, {"review": ("codex", "gpt-6-sol")}, monkeypatch)
    d = router.route("review_code")
    assert (d.engine, d.model) == ("codex", "gpt-6-sol"), d.reason
