"""Unit tests for each concern of the model router (docs/product/MODEL_ROUTER.md): tiers, the
classifier, policy, headroom, selection and escalation. The realistic examples live in
test_model_router_examples.py; the week simulation and the overhead benchmark have their own.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ._router_fixtures import NOW, claude, local, metered, policy, snap
from boss_engine.services.model_router import tiers
from boss_engine.services.model_router.classifier import (
    CLASSIFIER, Classifier, TaskSignal, jev_state, rules_classify,
)
from boss_engine.services.model_router.escalation import EscalationLedger, seed_from_lineage
from boss_engine.services.model_router.headroom import (
    assess, windows_from_limit_message, windows_from_rate_limit_event,
)
from boss_engine.services.model_router.policy import (
    PolicyError, Target, floors_from_profile, from_dict, preset_floors, seed,
)
from boss_engine.services.model_router.router import RouteRequest, Router
from boss_engine.services.model_router.selection import SourceState, select
from boss_engine.services.pipeline_profiles import resolve_profile
from boss_engine.services.typed_decisions import Decision, DecisionBatch


@pytest.fixture(autouse=True)
def _fresh_cache():
    CLASSIFIER.clear()
    yield


# ── tiers ──────────────────────────────────────────────────────────────────────────────────────

def test_tier_arithmetic_is_bounded():
    assert tiers.up("frontier") == "frontier" and tiers.down("fast") == "fast"
    assert tiers.up("fast", 2) == "frontier"
    assert tiers.at_least("fast", "balanced") == "balanced"
    assert tiers.at_least("frontier", "balanced") == "frontier"
    assert tiers.highest(["fast", None, "balanced"]) == "balanced"


def test_the_legacy_classes_map_onto_tiers():
    assert tiers.LEGACY_CLASS == {"low_cost": "balanced", "strong": "frontier"}


# ── (a) classifier ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,tier", [
    ("Change the button colour to blue on the existing settings page.", "fast"),
    ("Fix the bug where the existing invoice total ignores discounts.", "frontier"),  # billing
    ("Build a new app that tracks reading habits.", "balanced"),
    ("Redesign the architecture of the existing job scheduler for concurrency.", "frontier"),
    ("Update the existing CSV export to include the created date column.", "balanced"),
])
def test_the_rules_read_the_task_like_rigor_does(text, tier):
    c = rules_classify(TaskSignal(kind="build_stage", step="implement",
                                  spec={"title": "", "instructions": text}))
    assert c.tier == tier, c.reason


def test_the_rules_never_read_boss_boilerplate():
    """The DEPLOYABILITY note says "deployability"; it must not read as a deploy change."""
    spec = {"title": "Change the heading colour on the existing page",
            "instructions": "Change the heading colour on the existing page.\n\n"
                            "DEPLOYABILITY (required for any network service): token auth"}
    assert rules_classify(TaskSignal(kind="build_stage", step="implement", spec=spec)).tier == "fast"


def test_an_explicit_risk_tier_wins():
    c = rules_classify(TaskSignal(kind="build_stage", step="implement", risk_tier=3,
                                  spec={"title": "rename a label", "instructions": "x"}))
    assert (c.tier, c.risk_tier, c.confidence) == ("frontier", 3, 0.9)


def test_too_little_to_judge_is_balanced_and_unsure():
    c = rules_classify(TaskSignal(kind="workflow_step", step="model_call"))
    assert c.tier == "balanced" and c.confidence < 0.5


def test_a_large_repo_lifts_a_fast_change():
    spec = {"title": "", "instructions": "Fix the typo in the existing footer label."}
    small = rules_classify(TaskSignal(kind="build_stage", step="implement", spec=spec,
                                      repo_files=50))
    big = rules_classify(TaskSignal(kind="build_stage", step="implement", spec=spec,
                                    repo_files=5000))
    assert small.tier == "fast" and big.tier == "balanced"


def test_chat_long_or_thinking_requests_are_balanced():
    assert rules_classify(TaskSignal(kind="chat", step="chat", text="x " * 900)).tier == "balanced"
    assert rules_classify(TaskSignal(kind="chat", step="chat",
                                     text="Compare Postgres and SQLite for this")).tier == "balanced"
    assert rules_classify(TaskSignal(kind="chat", step="chat",
                                     text="Do a threat model of our login")).tier == "frontier"


def test_the_fingerprint_is_the_task_not_just_its_title():
    a = TaskSignal(kind="build_stage", step="implement",
                   spec={"title": "Fix", "instructions": "rename a label"})
    b = TaskSignal(kind="build_stage", step="implement",
                   spec={"title": "Fix", "instructions": "rewrite the billing engine"})
    assert a.fingerprint() != b.fingerprint()
    # Every build stage shares one classification of the task.
    assert a.fingerprint() == replace(a, step="plan").fingerprint()


class _Decider:
    def __init__(self, answer="frontier", confidence=0.9, fail=False):
        self.answer, self.confidence, self.fail, self.calls = answer, confidence, fail, 0

    def decide(self, context, questions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        decisions = {q.key: Decision(None, None) for q in questions}
        decisions["route_tier"] = Decision(self.answer, self.confidence, "judged")
        return DecisionBatch(decisions=decisions, backend="stub", model="stub", latency_ms=1)


def test_jev_answers_when_ready():
    jev = _Decider("frontier")
    c = Classifier().classify(TaskSignal(kind="chat", step="chat", text="hello"),
                              policy().classifier, jev=jev, jev_state="ready")
    assert (c.classifier, c.tier) == ("jev", "frontier") and jev.calls == 1


def test_a_failing_jev_falls_back_and_is_cached_briefly():
    """A failure is remembered for a short while (FALLBACK_TTL_S), so every stage of a build
    does not ask — and wait on — a broken classifier again."""
    from boss_engine.services.model_router import classifier as clf_mod

    jev = _Decider(fail=True)
    clf = Classifier()
    signal = TaskSignal(kind="chat", step="chat", text="hello there")
    first = clf.classify(signal, policy().classifier, jev=jev, jev_state="ready")
    second = clf.classify(signal, policy().classifier, jev=jev, jev_state="ready")
    assert first.classifier == "rules" and first.fallback_note == "Jev failed"
    assert second.cached and jev.calls == 1
    assert clf._cache[next(iter(clf._cache))][0] - __import__("time").monotonic() \
        <= clf_mod.FALLBACK_TTL_S


def test_the_cheap_model_is_asked_only_when_the_rules_are_unsure_and_it_is_on():
    pol = policy()
    off = pol.classifier
    on = replace(off, model_fallback=True)
    model = _Decider("fast", 0.8)
    unsure = TaskSignal(kind="workflow_step", step="model_call", text="")
    sure = TaskSignal(kind="chat", step="chat", text="What time is it?")
    assert Classifier().classify(unsure, off, model=lambda: model).classifier == "rules"
    assert model.calls == 0
    assert Classifier().classify(sure, on, model=lambda: model).classifier == "rules"
    assert model.calls == 0
    judged = Classifier().classify(unsure, on, model=lambda: model)
    assert (judged.classifier, judged.tier) == ("model", "fast") and model.calls == 1


def test_a_failing_cheap_model_keeps_the_rules_answer():
    on = replace(policy().classifier, model_fallback=True)
    c = Classifier().classify(TaskSignal(kind="workflow_step", step="model_call"), on,
                              model=lambda: _Decider(fail=True))
    assert c.classifier == "rules" and "cheap model failed" in c.fallback_note


def test_rules_only_never_asks_anyone():
    rules_only = replace(policy().classifier, backend="rules")
    jev = _Decider()
    c = Classifier().classify(TaskSignal(kind="chat", step="chat", text="hi"), rules_only,
                              jev=jev, jev_state="ready")
    assert c.classifier == "rules" and jev.calls == 0


def test_jev_is_held_while_boss_451_stands():
    settings = SimpleNamespace(decision_backend="typesafe", typesafe_api_key="tk",
                               router_jev_live=False)
    assert jev_state(settings) == "held"
    assert jev_state(SimpleNamespace(decision_backend="claude", typesafe_api_key="")) == "absent"


# ── (b) policy ─────────────────────────────────────────────────────────────────────────────────

def test_a_partial_update_merges():
    base = policy()
    merged = from_dict({"floors": [{"match": {"step": "implement"}, "tier": "balanced"}]},
                       base=base)
    assert merged.targets("frontier") == base.targets("frontier")
    assert [f.match for f in merged.floors] == [{"step": "implement"}]


@pytest.mark.parametrize("doc,needle", [
    ({"mode": "clever"}, "mode"),
    ({"tiers": {"huge": []}}, "tier"),
    ({"tiers": {"fast": [{"model": "x"}]}}, "engine"),
    ({"floors": [{"match": {}, "tier": "fast"}]}, "match"),
    ({"floors": [{"match": {"colour": "red"}, "tier": "fast"}]}, "colour"),
    ({"headroom": [{"tiers": ["frontier"], "when": "near_limit"}]}, "Frontier"),
    ({"thresholds": {"tight": 0.1, "near_limit": 0.2}}, "near_limit"),
    ({"escalation": [{"signal": "vibes"}]}, "signal"),
    ({"classifier": {"backend": "oracle"}}, "backend"),
    ({"classifier": {"criteria": {"fast": "  "}}}, "criteria"),
])
def test_a_bad_policy_is_refused_with_its_field(doc, needle):
    with pytest.raises(PolicyError) as err:
        from_dict(doc, base=policy())
    assert needle.lower() in str(err.value).lower()


def test_a_policy_round_trips():
    p = policy()
    assert from_dict(p.to_dict()).to_dict() == p.to_dict()


def test_profiles_are_floor_presets():
    assert {f.describe() for f in preset_floors("balanced")} >= {
        "Adversarial review", "Security check", "Consistency check"}
    assert not any(f.tier == "frontier" and f.match.get("step") == "implement"
                   for f in preset_floors("balanced"))
    assert preset_floors("strict")[0].match == {"kind": "build_stage"}
    vag = {f.match.get("step") for f in preset_floors("vag")}
    assert "implement" not in vag and "write_tests" not in vag and "spec" in vag
    with pytest.raises(PolicyError):
        preset_floors("nonsense")


def test_a_custom_profile_converts_the_same_way():
    custom = {"name": "mine", "model": {"default": "low_cost", "implement": "strong"}}
    assert [f.match for f in floors_from_profile(custom) if f.tier == "frontier"][0] == \
        {"step": "implement"}


def test_seeding_reads_the_existing_lanes():
    settings = SimpleNamespace(code_agent_strong_engine="codex", code_agent_strong_model="gpt-5",
                               code_agent_low_cost_engine="", code_agent_low_cost_model="")
    p = seed(settings=settings, connected={"claude_code": {}, "codex": {}},
             build_engine="claude_code", profile=resolve_profile("balanced"))
    assert p.targets("frontier") == (Target("codex", "gpt-5"),)
    assert p.targets("balanced") == (Target("claude_code", "sonnet"),)
    assert p.targets("fast") == (Target("claude_code", "haiku"),)
    assert p.headroom == ()          # no OpenRouter, so nothing to move to
    assert p.mode == "fixed"         # seeding never switches an install to Smart


# ── headroom ───────────────────────────────────────────────────────────────────────────────────

def test_a_rate_limit_event_is_read_in_every_shape_seen():
    single = windows_from_rate_limit_event({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.86,
        "resetsAt": 1760000000}})
    assert single["seven_day"]["used"] == 0.86 and single["seven_day"]["status"] == "allowed_warning"
    multi = windows_from_rate_limit_event({"rate_limit_info": {"status": "allowed",
        "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1760000000},
                           "seven_day": {"used_percentage": 64, "resetsAt": 1760500000}}}})
    assert multi["five_hour"]["used"] == 0.2 and multi["seven_day"]["used"] == 0.64
    assert windows_from_rate_limit_event({"rate_limit_info": {"status": "allowed"}}) == {}


def test_claude_limit_messages_close_their_window_until_the_reset():
    weekly = windows_from_limit_message("You've hit your weekly limit · resets Mon 12:00am", NOW)
    reset = datetime.fromisoformat(weekly["seven_day"]["resets_at"])
    assert reset.weekday() == 0 and reset > NOW and reset.hour == 0
    session = windows_from_limit_message("You've hit your session limit · resets 3:45pm", NOW)
    assert datetime.fromisoformat(session["five_hour"]["resets_at"]) == \
        NOW.replace(hour=15, minute=45)
    opus = windows_from_limit_message("You've hit your Opus limit · resets 9pm", NOW)
    assert "seven_day_opus" in opus
    monthly = windows_from_limit_message("You've hit your org's monthly spend limit", NOW)
    assert monthly["monthly"]["status"] == "rejected"
    assert windows_from_limit_message("syntax error near line 3", NOW) == {}


def test_a_zoned_reset_is_read_in_its_zone():
    w = windows_from_limit_message(
        "You've hit your session limit · resets 3pm (Europe/London)", NOW)
    assert datetime.fromisoformat(w["five_hour"]["resets_at"]).hour == 14  # BST is UTC+1


@pytest.mark.parametrize("left,state", [(0.9, "plenty"), (0.3, "tight"), (0.08, "near_limit")])
def test_headroom_states(left, state):
    assert claude(left).headroom.state == state


def test_a_warning_is_at_least_tight_and_a_rolled_window_is_forgotten():
    assert claude(0.9, status="allowed_warning").headroom.state == "tight"
    past = assess("claude_code", {"windows": {"seven_day": {
        "used": 1.0, "resets_at": (NOW - timedelta(hours=1)).isoformat()}}},
        thresholds={"tight": 0.4, "near_limit": 0.15}, now=NOW)
    assert past.state == "plenty"


def test_the_estimate_and_the_learned_allowance():
    th = {"tight": 0.4, "near_limit": 0.15}
    assert assess("claude_code", {}, thresholds=th, used_usd=90, allowance_usd=100,
                  now=NOW).state == "near_limit"
    learned = assess("claude_code", {"learned_weekly_allowance_usd": 400}, thresholds=th,
                     used_usd=100, now=NOW)
    assert learned.basis == "estimate" and round(learned.left, 2) == 0.75
    assert assess("claude_code", {}, thresholds=th, used_usd=100, now=NOW).state == "plenty"


def test_provider_limits_now_recognise_claudes_weekly_limit():
    from boss_engine.services.provider_limits import classify_limit

    signal = classify_limit("You've hit your weekly limit · resets Mon 12:00am")
    assert signal is not None and signal.retry_at is not None
    assert classify_limit("You've hit your Opus limit · resets 3pm") is None  # one family only


# ── (c) selection ──────────────────────────────────────────────────────────────────────────────

def test_selection_walks_past_what_cannot_run():
    sources = {"claude_code": SourceState("claude_code", "subscription", working=False),
               "openrouter": metered("openrouter")}
    chosen = select("balanced", policy(), sources)
    assert chosen.target.engine == "openrouter" and "Claude Code failing" in chosen.notes


def test_an_allow_list_is_honoured():
    chosen = select("balanced", policy(), {"claude_code": claude(), "ollama": local()},
                    allowed={"ollama"})
    assert chosen.target.engine == "ollama" and chosen.fallback


def test_an_engine_pin_takes_the_tiers_model_on_that_source():
    chosen = select("frontier", policy(), {"claude_code": claude()}, engine_pin="claude_code")
    assert chosen.target == Target("claude_code", "opus")


def test_nothing_connected_names_nothing():
    assert select("fast", policy(), {}).target is None


def test_frontier_is_never_moved_by_a_headroom_rule():
    sources = {"claude_code": claude(0.05), "openrouter": metered("openrouter")}
    assert select("frontier", policy(), sources).target.engine == "claude_code"
    assert select("balanced", policy(), sources).target.engine == "openrouter"


# ── (d) escalation ─────────────────────────────────────────────────────────────────────────────

def test_the_ledger_escalates_at_its_count_and_reads_aliases():
    ledger = EscalationLedger()
    rules = policy().escalation
    ledger.record("solo", "tests_failed", 2)
    hit = ledger.escalate("implement", "fast", rules, steps_alias=("solo",))
    assert hit and hit.tier == "balanced" and hit.signal == "tests_failed"
    assert ledger.escalate("review_code", "balanced", rules) is None


def test_lineage_seeds_the_ledger():
    seeded = seed_from_lineage({"attempt": 3, "prior_failing_gates": ["code_review_passed"],
                                "route_escalations": {"write_tests": {"no_output": 1}}})
    assert seeded["implement"]["changes_requested"] == 2
    assert "gate_failed" not in seeded["implement"]
    assert seeded["write_tests"] == {"no_output": 1}
    gates = seed_from_lineage({"attempt": 2, "prior_failing_gates": ["tests_strong_enough"]})
    assert gates["implement"] == {"gate_failed": 1}
    assert seed_from_lineage({"attempt": 1}) == {}


def test_two_review_rounds_move_implement_up():
    ledger = EscalationLedger(seed_from_lineage({"attempt": 3,
                                                 "prior_failing_gates": ["code_review_passed"]}))
    d = Router().route(RouteRequest(kind="build_stage", step="implement", project_id="p1",
                                    spec={"title": "", "instructions": "Build a new app."}),
                       snap(), ledger=ledger)
    assert d.tier == "frontier" and d.escalation_signal == "changes_requested"


# ── the facade ─────────────────────────────────────────────────────────────────────────────────

def test_the_same_answer_for_a_unit_keeps_its_route_id():
    router = Router()
    req = RouteRequest(kind="build_stage", step="review_code", project_id="p1", unit_id="r1",
                       spec={"title": "", "instructions": "Build a new app."})
    assert router.route(req, snap()).id == router.route(req, snap()).id


def test_a_session_pin_keeps_the_model_whatever_changed():
    s = snap(sources={"claude_code": claude(0.05), "openrouter": metered("openrouter")})
    d = Router().route(RouteRequest(kind="chat", step="chat", project_id="p1", text="hi",
                                    session_pin=Target("claude_code", "haiku")), s)
    assert (d.engine, d.model) == ("claude_code", "haiku") and "pin:session" in d.rules


def test_a_decision_explains_itself():
    d = Router().route(RouteRequest(kind="build_stage", step="review_code", project_id="p1",
                                    spec={"title": "", "instructions": "Build a new app."}),
                       snap())
    body = d.to_dict()
    assert body["tier_label"] == "Frontier" and body["label"] == "Claude Code · opus"
    assert body["why"][0].startswith("Task:") and body["classifier"] == "rules"


def test_a_builds_own_profile_adds_its_floors():
    """A task that asked for `balanced` keeps review on Frontier on a `lean` project."""
    lean = snap(pol=policy(profile="lean"))
    spec = {"title": "", "instructions": "Build a new app."}
    plain = Router().route(RouteRequest(kind="build_stage", step="review_code",
                                        project_id="p1", spec=spec), lean)
    assert plain.tier == "balanced"
    with_profile = Router().route(RouteRequest(
        kind="build_stage", step="review_code", project_id="p1", spec=spec,
        extra_floors=floors_from_profile(resolve_profile("balanced"))), lean)
    assert with_profile.tier == "frontier" and "floor" in with_profile.rules
