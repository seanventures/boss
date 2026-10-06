"""The realistic examples in docs/product/MODEL_ROUTER.md, each an executable test.

Every row of the doc's table is a case here, with the route the doc promises. When the doc and
the router disagree, one of them is wrong — this file is how that is noticed.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from ._router_fixtures import claude, local, metered, policy, snap
from boss_engine.services.model_router.classifier import CLASSIFIER
from boss_engine.services.model_router.escalation import EscalationLedger
from boss_engine.services.model_router.router import RouteRequest, Router
from boss_engine.services.model_router.selection import SourceState
from boss_engine.services.model_router.stages import StageRouter
from boss_engine.services.pipeline_profiles import (
    apply_budget_pressure, materialize_route, resolve_profile, route_for_step,
)

COPY_FIX = {"title": "Fix the typo in the footer copy",
            "instructions": "Change 'Copyrigth' to 'Copyright' in the existing footer."}
FULL_STACK = {"title": "Lead tracker",
              "instructions": "Build a new full-stack app: a React frontend and a FastAPI "
                              "backend for tracking sales leads, stored in Postgres, with a "
                              "dashboard and CSV export."}
OAUTH = {"title": "Add OAuth login",
         "instructions": "Add OAuth login with refresh tokens to the existing app, and keep "
                         "sessions across restarts."}


@pytest.fixture(autouse=True)
def _fresh_cache():
    CLASSIFIER.clear()
    yield
    CLASSIFIER.clear()


def build(step: str, spec: dict, **kw) -> RouteRequest:
    return RouteRequest(kind="build_stage", step=step, project_id="p1", unit_id=kw.pop("unit", None),
                        text=spec["title"], spec=spec, **kw)


def route(request, snapshot=None, ledger=None):
    return Router().route(request, snapshot or snap(), ledger=ledger)


def where(d) -> tuple:
    return d.tier, d.engine, d.model


# 1 ─ a one-line copy fix on a small repo
def test_1_a_copy_fix_is_fast_on_haiku():
    d = route(build("implement", COPY_FIX, repo_files=40))
    assert where(d) == ("fast", "claude_code", "haiku")
    assert d.classification.classifier == "rules"


# 2 ─ a new full-stack app: balanced build, frontier adversarial review
def test_2_a_new_full_stack_app_builds_balanced():
    assert where(route(build("implement", FULL_STACK))) == ("balanced", "claude_code", "sonnet")


def test_2b_its_adversarial_review_is_frontier_by_floor():
    d = route(build("review_code", FULL_STACK))
    assert where(d) == ("frontier", "claude_code", "opus")
    assert "floor" in d.rules and any("Adversarial review" in w for w in d.why)


# 3 ─ Thursday, Claude at 8% of its weekly window
def test_3_near_the_weekly_limit_routine_docs_move_to_openrouter():
    s = snap(sources={"claude_code": claude(0.08), "openrouter": metered("openrouter")})
    d = route(RouteRequest(kind="workflow_step", step="docs", project_id="p1",
                           text="Write the release notes for this week's changes."), s)
    assert where(d) == ("fast", "openrouter", "deepseek/deepseek-chat")
    assert d.moved_by_headroom and "headroom" in d.rules


def test_3b_the_same_week_a_security_review_keeps_opus_on_the_subscription():
    s = snap(sources={"claude_code": claude(0.08), "openrouter": metered("openrouter")})
    d = route(build("security_check", FULL_STACK), s)
    assert where(d) == ("frontier", "claude_code", "opus")
    assert not d.moved_by_headroom


# 4 ─ tests failed twice → implement escalates
def test_4_tests_failing_twice_escalates_implement():
    ledger = EscalationLedger()
    first = route(build("implement", FULL_STACK), ledger=ledger)
    assert first.tier == "balanced"
    ledger.record("implement", "tests_failed")
    assert route(build("implement", FULL_STACK), ledger=ledger).tier == "balanced"
    ledger.record("implement", "tests_failed")
    third = route(build("implement", FULL_STACK), ledger=ledger)
    assert where(third) == ("frontier", "claude_code", "opus")
    assert third.escalation_signal == "tests_failed"


# 5 ─ the monthly API budget is red: downgrade, except floors
def test_5_a_red_budget_drops_a_tier():
    d = route(build("implement", OAUTH), snap(budget="red"))
    assert d.classified_tier == "frontier" and d.tier == "balanced" and d.budget_lite


def test_5b_a_red_budget_never_drops_below_a_floor():
    assert route(build("review_code", OAUTH), snap(budget="red")).tier == "frontier"


# 6 ─ Jev times out → the rules answer
def test_6_a_jev_timeout_falls_back_to_the_rules():
    class SlowJev:
        def decide(self, context, questions):
            time.sleep(0.5)
            raise AssertionError("should have timed out")

    pol = policy()
    from dataclasses import replace

    pol = replace(pol, classifier=replace(pol.classifier, jev_timeout_ms=100))
    s = snap(pol=pol, jev_state="ready", jev_factory=SlowJev)
    started = time.perf_counter()
    d = route(build("implement", COPY_FIX), s)
    assert time.perf_counter() - started < 0.45
    assert d.classification.classifier == "rules"
    assert d.classification.fallback_note == "Jev timed out"
    assert where(d) == ("fast", "claude_code", "haiku")


def test_6b_jev_held_says_paused_and_the_rules_answer():
    d = route(build("implement", COPY_FIX), snap(jev_state="held"))
    assert d.classification.fallback_note == "Jev paused"


# 7 ─ an agent pinned to a local model
def test_7_an_agent_pinned_local_is_respected():
    s = snap(sources={"claude_code": claude(), "openrouter": metered("openrouter"),
                      "ollama": local()})
    d = route(RouteRequest(kind="agent", step="agent", project_id="p1",
                           text="Design the architecture of a distributed consensus service.",
                           agent_policy={"selection_mode": "local",
                                         "pinned_model": "ollama:llama3.1"}), s)
    assert (d.engine, d.model) == ("ollama", "llama3.1")
    assert "pin:agent_local" in d.rules


def test_7b_a_local_agent_without_a_named_model_still_stays_local():
    s = snap(sources={"claude_code": claude(), "ollama": local()})
    d = route(RouteRequest(kind="agent", step="agent", project_id="p1", text="Summarise this.",
                           agent_policy={"selection_mode": "local"}), s)
    assert d.engine == "ollama"


# 8 ─ Claude near its limit and OpenRouter down: never stranded
def test_8_openrouter_down_stays_on_the_subscription():
    s = snap(sources={"claude_code": claude(0.08),
                      "openrouter": metered("openrouter", working=False)})
    d = route(build("implement", FULL_STACK), s)
    assert where(d) == ("balanced", "claude_code", "sonnet")
    assert any("stayed" in w for w in d.why)


# 9 ─ a chat question
def test_9_a_chat_question_is_fast():
    d = route(RouteRequest(kind="chat", step="chat", project_id="p1",
                           text="What does this error mean: KeyError 'user_id'?"))
    assert where(d) == ("fast", "claude_code", "haiku")


# 10 ─ a panel: the facilitator is frontier, a panelist balanced
def test_10_a_panel_facilitator_is_frontier():
    d = route(RouteRequest(kind="panel", step="panel_facilitator", project_id="p1",
                           text="A booking app for dog walkers"))
    assert where(d) == ("frontier", "claude_code", "opus")


def test_10b_a_panelist_is_balanced():
    d = route(RouteRequest(kind="panel", step="panel_member", project_id="p1",
                           text="A booking app for dog walkers"))
    assert where(d) == ("balanced", "claude_code", "sonnet")


# 11 ─ research synthesis
def test_11_research_synthesis_is_balanced():
    d = route(RouteRequest(kind="research", step="research_synthesis", project_id="p1",
                           text="Is there demand for a dog-walker booking app in Leeds?"))
    assert where(d) == ("balanced", "claude_code", "sonnet")


# 12 ─ Fixed mode: exactly the legacy answer
def _settings(**kw):
    base = dict(code_agent_strong_engine="", code_agent_low_cost_engine="",
                code_agent_strong_model="", code_agent_low_cost_model="",
                code_agent_model="", code_review_model="")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("profile", ["lean", "balanced", "strict", "vag"])
@pytest.mark.parametrize("step", ["implement", "write_tests", "review_code", "security_check"])
@pytest.mark.parametrize("budget", ["green", "red"])
def test_12_fixed_mode_is_the_legacy_route(profile, step, budget):
    settings = _settings(code_agent_strong_engine="claude_code", code_agent_strong_model="opus",
                         code_agent_low_cost_engine="openrouter",
                         code_agent_low_cost_model="qwen/qwen3-coder")
    prof = resolve_profile(profile)
    stages = StageRouter(transaction=None, settings=settings, project_id="p1", run_id="r1",
                         spec=dict(FULL_STACK), title=FULL_STACK["title"], profile=prof,
                         build_engine="claude_code", review_engine="codex", budget_state=budget)
    r = route_for_step(prof, step, None)
    r, _ = apply_budget_pressure(r, step, budget)
    expected = materialize_route(
        r, build_engine="claude_code", review_engine="codex",
        strong_engine="claude_code", low_cost_engine="openrouter",
        strong_model="opus", low_cost_model="qwen/qwen3-coder", build_model="", review_model="")
    request = RouteRequest(kind="build_stage", step=step, project_id="p1",
                           legacy=lambda: stages.legacy(step))
    d = Router().route(request, snap(mode="fixed"))
    assert d.mode == "fixed" and (d.engine, d.model) == expected
    assert d.reason.startswith("Fixed:")


def test_12b_fixed_mode_escalation_is_the_hard_problems_lane():
    settings = _settings(code_agent_strong_engine="claude_code", code_agent_strong_model="opus")
    stages = StageRouter(transaction=None, settings=settings, project_id="p1", run_id="r1",
                         spec={}, title="t", profile=resolve_profile("balanced"),
                         build_engine="codex", review_engine="codex", budget_state="green")
    legacy = stages.legacy("implement", escalated=True, previous={"engine": "codex"})
    assert (legacy.engine, legacy.model) == ("claude_code", "opus")


# 13 ─ a job pinned to Codex in Smart mode
def test_13_a_build_pinned_to_codex_runs_on_codex():
    s = snap(sources={"claude_code": claude(), "codex": SourceState("codex", "subscription")},
             job_pins={"build": ("codex", None)})
    d = route(build("implement", FULL_STACK), s)
    assert d.engine == "codex" and d.tier == "balanced" and "pin:job" in d.rules


def test_13b_a_job_pinned_to_a_model_is_a_hard_pin():
    d = route(build("implement", COPY_FIX), snap(job_pins={"build": ("claude_code", "opus")}))
    assert (d.engine, d.model) == ("claude_code", "opus") and d.tier == "fast"


# 14 ─ the weekly limit is hit: frontier leaves for the next frontier target
def test_14_weekly_limit_hit_moves_frontier_to_openrouter_opus():
    s = snap(sources={"claude_code": claude(weekly_spent=True),
                      "openrouter": metered("openrouter")})
    d = route(build("security_check", FULL_STACK), s)
    assert where(d) == ("frontier", "openrouter", "anthropic/claude-opus-4.1")
    assert any("exhausted" in w for w in d.why)


# 15 ─ write_tests wrote nothing → frontier
def test_15_no_test_files_escalates_write_tests_to_frontier():
    ledger = EscalationLedger()
    assert route(build("write_tests", FULL_STACK), ledger=ledger).tier == "balanced"
    ledger.record("write_tests", "no_output")
    assert where(route(build("write_tests", FULL_STACK), ledger=ledger)) == (
        "frontier", "claude_code", "opus")


# 16 ─ a sensitive surface is frontier work
def test_16_an_oauth_change_is_frontier():
    d = route(build("implement", OAUTH))
    assert where(d) == ("frontier", "claude_code", "opus")
    assert d.classification.sensitive


# 17 ─ Opus's own window closes; Sonnet keeps running
def test_17_an_opus_limit_moves_only_opus_work():
    s = snap(sources={"claude_code": claude(0.6, opus_spent=True),
                      "openrouter": metered("openrouter")})
    review = route(build("review_code", FULL_STACK), s)
    implement = route(build("implement", FULL_STACK), s)
    assert where(review) == ("frontier", "openrouter", "anthropic/claude-opus-4.1")
    assert where(implement) == ("balanced", "claude_code", "sonnet")


# 18 ─ the same task twice: the second classification is cached
def test_18_the_same_task_classifies_once():
    first = route(build("plan", FULL_STACK))
    second = route(build("implement", FULL_STACK))
    assert not first.classification.cached and second.classification.cached
