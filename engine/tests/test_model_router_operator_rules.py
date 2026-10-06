"""The operator's rules, held against the independent tester's adversarial scenarios
(docs/product/MODEL_ROUTER.md, Decisions 14–19). Each test names the tester's scenario id.
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from ._router_fixtures import claude, local, metered, policy, snap
from boss_engine.services.model_router import breaker
from boss_engine.services.model_router.classifier import CLASSIFIER
from boss_engine.services.model_router.escalation import EscalationLedger
from boss_engine.services.model_router.headroom import assess, state_for
from boss_engine.services.model_router.policy import PolicyError, Target, floors_from_profile, \
    from_dict
from boss_engine.services.model_router.router import RouteRequest, Router
from boss_engine.services.model_router.selection import SourceState
from boss_engine.services.typed_decisions import Decision, DecisionBatch

OAUTH = {"title": "Add OAuth login", "instructions": "Add OAuth login with refresh tokens to "
         "the existing app, and keep sessions across restarts."}
MIGR = {"title": "Rotate the payments secret", "instructions": "Rotate the Stripe secret key "
        "and add a DB migration for the payouts table."}
FEATURE = {"title": "CSV export", "instructions": "Add a CSV export button to the leads table "
           "in the existing dashboard."}


@pytest.fixture(autouse=True)
def _clean():
    CLASSIFIER.clear()
    breaker.reset()
    yield
    breaker.reset()


def b(step, spec, **kw):
    return RouteRequest(kind="build_stage", step=step, project_id="p1",
                        unit_id=kw.pop("unit", None), text=spec["title"], spec=spec, **kw)


def go(req, s, ledger=None):
    return Router().route(req, s, ledger=ledger)


class _Jev:
    def __init__(self, answer):
        self.answer = answer

    def decide(self, context, questions):
        return DecisionBatch(decisions={"route_tier": Decision(self.answer, 0.92, "looks small")},
                             backend="jev", model="jev", latency_ms=5)


# ── 1. Local means local ─────────────────────────────────────────────────────────────────────

def test_S3d_a_local_agent_with_no_local_model_refuses_in_words():
    d = go(RouteRequest(kind="agent", step="agent", project_id="p1", text="Summarise the export",
                        agent_policy={"selection_mode": "local"}), snap())
    assert d.refused and not d.engine
    assert d.refusal.startswith("No local model connected")


def test_S3c_an_allow_list_never_falls_back_outside_it():
    d = go(b("implement", FEATURE), snap(pol=replace(policy(), allowed_sources=("ollama",))))
    assert d.refused and d.engine == ""


def test_local_policy_is_obeyed_in_fixed_mode_too():
    s = snap(mode="fixed", sources={"claude_code": claude(), "ollama": local()})
    d = go(RouteRequest(kind="agent", step="agent", project_id="p1", text="Summarise",
                        agent_policy={"selection_mode": "local"}), s)
    assert d.engine == "ollama" and d.binding and d.mode == "fixed"
    refused = go(RouteRequest(kind="agent", step="agent", project_id="p1", text="Summarise",
                              agent_policy={"selection_mode": "local"}),
                 snap(mode="fixed", sources={"claude_code": claude()}))
    assert refused.refused


def test_a_refused_route_raises_instead_of_falling_back(monkeypatch):
    from boss_engine.services import model_routing
    from boss_engine.services.model_router.failover import RouteRefused

    refused = go(RouteRequest(kind="agent", step="agent", project_id="p1", text="x",
                              agent_policy={"selection_mode": "local"}), snap())
    monkeypatch.setattr(model_routing, "route_call", lambda *a, **k: refused)
    with pytest.raises(RouteRefused, match="No local model connected"):
        model_routing.project_chat_adapter(None, object(), "p1")


# ── 3. Never stranded ────────────────────────────────────────────────────────────────────────

def test_a_failing_source_is_skipped_until_its_breaker_closes():
    breaker.trip("claude_code", "503 upstream", seconds=60)
    d = go(b("implement", FEATURE), snap())
    assert d.engine == "openrouter" and d.moved_by_headroom
    assert any("failing" in w for w in d.why)


@pytest.mark.parametrize("sources", [
    {"claude_code": claude(weekly_spent=True), "openrouter": metered("openrouter")},
])
def test_S1c_S1d_an_engine_job_preference_is_moved_by_exhaustion(sources):
    implement = go(b("implement", FEATURE), snap(sources=sources,
                                                 job_pins={"build": ("claude_code", None)}))
    review = go(b("review_code", FEATURE), snap(sources=sources,
                                                job_pins={"review": ("claude_code", None)}))
    assert implement.engine == "openrouter"
    assert (review.tier, review.engine, review.model) == (
        "frontier", "openrouter", "anthropic/claude-opus-4.1")


def test_S1b_a_job_preference_still_obeys_the_headroom_rule():
    d = go(b("implement", FEATURE), snap(sources={"claude_code": claude(0.08),
                                                  "openrouter": metered("openrouter")},
                                         job_pins={"build": ("claude_code", None)}))
    assert d.engine == "openrouter" and d.moved_by_headroom


def test_S12b_S14b_a_session_breaks_when_its_source_is_exhausted():
    s = snap(sources={"claude_code": claude(weekly_spent=True),
                      "openrouter": metered("openrouter")})
    d = go(RouteRequest(kind="chat", step="chat", project_id="p1", text="hi",
                        session_pin=Target("claude_code", "haiku")), s)
    assert d.engine == "openrouter" and "session_broken" in d.rules


def test_S12_a_kept_session_is_recorded_at_its_own_tier():
    ledger = EscalationLedger()
    ledger.record("implement", "tests_failed", 2)
    d = go(b("implement", FEATURE, session_pin=Target("claude_code", "sonnet")), snap(),
           ledger=ledger)
    assert (d.model, d.tier) == ("sonnet", "balanced")


def test_failover_agent_retries_the_same_stage_on_the_next_source():
    from boss_engine.services.model_router.failover import FailoverAgent

    calls = []

    class Agent:
        def __init__(self, name, error=None):
            self.name, self.error = name, error

        def run_role(self, prompt, cwd, *, model=None, resume_session_id=None):
            calls.append((self.name, model, resume_session_id))
            if self.error:
                raise RuntimeError(self.error)
            return {"summary": "ok", "model": model}

    nxt = type("D", (), {"engine": "openrouter", "refused": False})()
    hops = []
    agent = FailoverAgent(Agent("claude", "claude code agent exited 1: You've hit your weekly "
                                          "limit · resets Oct 13 at 10am (America/Los_Angeles)"),
                          "sonnet", "claude_code",
                          reroute=lambda: (Agent("openrouter"), "qwen/qwen3-coder", nxt),
                          on_hop=lambda failed, d, kind: hops.append((failed, kind)))
    out = agent.run_role("p", ".", model="sonnet", resume_session_id="s1")
    assert out["model"] == "qwen/qwen3-coder"
    assert calls == [("claude", "sonnet", "s1"), ("openrouter", "qwen/qwen3-coder", None)]
    assert hops == [("claude_code", "limit")] and breaker.open_reason("claude_code")


def test_failover_does_not_hide_a_real_failure():
    from boss_engine.services.model_router.failover import FailoverAgent

    class Bad:
        def run_role(self, *a, **k):
            raise ValueError("the prompt is malformed")

    agent = FailoverAgent(Bad(), None, "claude_code", reroute=lambda: (_ for _ in ()).throw(
        AssertionError("must not reroute")))
    with pytest.raises(ValueError):
        agent.run_role("p", ".", model=None)


def test_a_chat_call_that_hits_a_503_retries_on_the_next_source():
    from boss_engine.services.model_router.failover import FailoverChatAdapter

    class Down:
        def create_text_response(self, prompt, **kw):
            raise RuntimeError("Error code: 503 - {'error': {'message': 'upstream down'}}")

    class Up:
        def create_text_response(self, prompt, **kw):
            return "ref", "answer", {"model": "haiku"}

    first = type("D", (), {"engine": "openrouter", "model": "x", "id": "r1", "refused": False})()
    second = type("D", (), {"engine": "claude_code", "model": "haiku", "id": "r2",
                            "refused": False})()
    adapter = FailoverChatAdapter(Down(), first, lambda failed: (Up(), second), record=False)
    assert adapter.create_text_response("hi")[1] == "answer"
    assert adapter.route.engine == "claude_code" and breaker.open_reason("openrouter")


# ── 4. Floors from profiles ──────────────────────────────────────────────────────────────────

def test_a_profile_step_pinned_to_opus_is_a_frontier_floor():
    floors = floors_from_profile({"model": {"default": "low_cost",
                                            "review_code": "claude_code:opus"}})
    assert {"step": "review_code"} in [f.match for f in floors if f.tier == "frontier"]


def test_a_profiles_explicit_pin_is_a_preference_within_the_floor():
    d = go(b("implement", FEATURE, preferred=Target("codex", "gpt-5.2-codex")),
           snap(sources={"claude_code": claude(), "codex": SourceState("codex", "subscription")}))
    assert d.engine == "codex" and "prefer:profile" in d.rules


# ── 5. Pin semantics ─────────────────────────────────────────────────────────────────────────

def test_S1_a_floor_raises_a_review_pinned_to_haiku():
    d = go(b("review_code", FEATURE), snap(job_pins={"review": ("claude_code", "haiku")}))
    assert (d.tier, d.engine, d.model) == ("frontier", "claude_code", "opus")
    assert any("Floor raised your pin" in w for w in d.why)


def test_a_hard_pin_is_an_explicit_choice_and_recorded_at_its_real_tier():
    hard = replace(policy(), hard_pins=("build",))
    d = go(b("implement", FEATURE), snap(pol=hard, job_pins={"build": ("claude_code", "sonnet")},
                                         sources={"claude_code": claude(0.05),
                                                  "openrouter": metered("openrouter")}))
    assert (d.engine, d.model, d.tier) == ("claude_code", "sonnet", "balanced")
    assert "pin:job_hard" in d.rules


# ── 6. Frontier never silently goes local ────────────────────────────────────────────────────

def test_S2_frontier_with_only_a_local_model_refuses_in_words():
    d = go(b("review_code", FEATURE), snap(sources={"ollama": local()}))
    assert d.refused and "No Frontier model" in d.refusal


def test_S2b_frontier_goes_subscription_then_metered_then_refuses():
    sources = {"claude_code": claude(weekly_spent=True),
               "openrouter": metered("openrouter", working=False), "ollama": local()}
    assert go(b("security_check", OAUTH), snap(sources=sources)).refused
    allowed = replace(policy(), frontier_allows_local=True)
    d = go(b("security_check", OAUTH), snap(pol=allowed, sources=sources))
    assert d.engine == "ollama"


# ── 7. Budget red + near limit ───────────────────────────────────────────────────────────────

def test_S11_a_spent_budget_never_moves_work_to_metered_openrouter():
    s = snap(budget="red", sources={"claude_code": claude(0.08),
                                    "openrouter": metered("openrouter")})
    implement = go(b("implement", OAUTH), s)
    chat = go(RouteRequest(kind="chat", step="chat", project_id="p1", text="What is this?"), s)
    assert implement.engine == "claude_code" and chat.engine == "claude_code"


def test_a_spent_budget_with_the_subscription_exhausted_waits_in_words():
    s = snap(budget="red", sources={"claude_code": claude(weekly_spent=True),
                                    "openrouter": metered("openrouter")})
    d = go(b("implement", FEATURE), s)
    assert d.refused and d.refusal.startswith("Waiting for budget or limit reset")


# ── 8. Classifier safety ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", [OAUTH, MIGR])
def test_S4_jev_can_raise_sensitive_work_but_never_lower_it(spec):
    d = go(b("implement", spec), snap(jev_state="ready", jev_factory=lambda: _Jev("fast")))
    assert d.tier == "frontier" and d.classification.classifier == "jev"


def test_S4d_risk_tier_zero_cannot_override_sensitive_words():
    assert go(b("implement", OAUTH, risk_tier=0), snap()).tier == "frontier"


def test_S15_chat_respects_sensitive_words():
    d = go(RouteRequest(kind="chat", step="chat", project_id="p1",
                        text="rotate the prod database password and run the migration"), snap())
    assert d.tier in ("balanced", "frontier")


def test_a_hung_classifier_does_not_queue_the_next_route():
    from boss_engine.services.model_router.classifier import Classifier, TaskSignal

    class Slow:
        def decide(self, *a):
            time.sleep(3)

    class Instant:
        def decide(self, *a):
            raise RuntimeError("instant")

    clf = Classifier()
    cfg = replace(policy().classifier, jev_timeout_ms=100)
    for i in range(6):
        clf.classify(TaskSignal(kind="chat", step="chat", text=f"q{i}"), cfg, jev=Slow(),
                     jev_state="ready")
    CLASSIFIER.clear()  # resets the cooldown the timeouts started
    started = time.perf_counter()
    clf.classify(TaskSignal(kind="chat", step="chat", text="another"), cfg, jev=Instant(),
                 jev_state="ready")
    assert time.perf_counter() - started < 0.2


def test_a_timed_out_jev_is_not_waited_on_by_every_stage():
    from boss_engine.services.model_router.classifier import Classifier, TaskSignal

    class Slow:
        def decide(self, *a):
            time.sleep(2)

    clf = Classifier()
    cfg = replace(policy().classifier, jev_timeout_ms=150)
    clf.classify(TaskSignal(kind="chat", step="chat", text="one"), cfg, jev=Slow(),
                 jev_state="ready")
    started = time.perf_counter()
    second = clf.classify(TaskSignal(kind="chat", step="chat", text="two"), cfg, jev=Slow(),
                          jev_state="ready")
    assert time.perf_counter() - started < 0.1 and "Jev slow" in second.fallback_note


# ── 13. Hysteresis and explicit validation ───────────────────────────────────────────────────

def test_S6_headroom_does_not_flap_at_the_threshold():
    th = {"tight": 0.40, "near_limit": 0.15}
    state = state_for(0.149, th)
    for left in (0.151, 0.149, 0.151, 0.18):
        state = state_for(left, th, state)
        assert state == "near_limit"
    assert state_for(0.21, th, state) == "tight"


@pytest.mark.parametrize("doc,needle", [
    ({"escalation": [{"signal": "tests_failed", "count": 0, "steps": ["implement"]}]}, "count"),
    ({"budget": {"red": 5}}, "budget.red"),
    ({"allowed_sources": []}, "at least one"),
    ({"tiers": {"frontier": [{"engine": "openrouter", "model": "deepseek/deepseek-chat"}]}},
     "not a Frontier"),
    ({"classifier": {"jev_timeout_ms": 99999}}, "jev_timeout_ms"),
])
def test_S17_nonsense_is_refused_not_clamped(doc, needle):
    with pytest.raises(PolicyError, match=needle):
        from_dict(doc, base=policy())


# ── 12. Units ────────────────────────────────────────────────────────────────────────────────

def test_utilization_is_a_fraction_and_used_percentage_a_percent():
    from boss_engine.services.model_router.headroom import windows_from_rate_limit_event as w

    assert w({"rateLimitType": "seven_day", "utilization": 1})["seven_day"]["used"] == 1.0
    assert w({"rateLimitType": "seven_day", "used_percentage": 1})["seven_day"]["used"] == 0.01
    assert w({"rateLimitType": "seven_day", "utilization": 45})["seven_day"]["used"] == 0.45
    assert assess("claude_code", {"windows": {"seven_day": {"used": 0.45}}},
                  thresholds={"tight": 0.4, "near_limit": 0.15}).state == "plenty"


def test_a_monthly_limit_is_half_open_after_a_while():
    """A monthly spend limit is lifted by a person; routed-away work would never find out, so
    after LIMIT_PROBE_S the source may be tried once (a success then clears it)."""
    from datetime import datetime, timedelta, timezone

    from boss_engine.services.model_router import headroom

    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    window = {"used": 1.0, "status": "rejected", "resets_at": "2026-11-01T00:00:00+00:00"}
    th = {"tight": 0.4, "near_limit": 0.15}
    fresh = {"windows": {"monthly": {**window, "observed_at": now.isoformat()}}}
    old = {"windows": {"monthly": {**window, "observed_at": (
        now - timedelta(seconds=headroom.LIMIT_PROBE_S + 1)).isoformat()}}}
    assert headroom.assess("claude_code", fresh, thresholds=th, now=now).state == "exhausted"
    assert headroom.assess("claude_code", old, thresholds=th, now=now).state != "exhausted"
