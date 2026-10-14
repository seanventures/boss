"""The pre-check asks the engine routing will ACTUALLY use whether it can start
(`sandbox.engine`), and reports the model each step routes to, on a source that is really
connected (`sandbox.model`).

The HelpDesk build that motivated it: Build pinned to claude_agent_sdk on the API key, every
command-running stage failed with "Sandbox dependencies not available: socat not installed",
and the pre-check had said "a model is connected: claude_code (subscription)" — the chat job's
answer, on a disconnected subscription.

No database, no network, no model: routes, source rows, binaries and the probe are stubbed."""
from __future__ import annotations

from typing import Any

import pytest

from boss_engine.services import engine_startup as es
from boss_engine.services import harness_registry as hr
from boss_engine.services import workflow_precheck as wp
from boss_engine.services.engine_startup import EngineRoute


class _Point:
    def rollback(self) -> None:
        pass


class _Conn:
    """Enough of a connection for Context.savepoint; reads are stubbed at the function."""

    engine = None

    def begin_nested(self) -> _Point:
        return _Point()

    def execute(self, *_a, **_k):
        raise RuntimeError("no database in this test")


class _Settings:
    plan_budget_usd_micros = 1_500_000
    code_task_budget_usd_micros = 2_000_000
    code_agent_max_concurrency = 1


def _step(sid: str, capability: str, **inputs: Any) -> dict[str, Any]:
    return {"id": sid, "kind": "capability", "next": [],
            "binding": {"capability": capability, "inputs": inputs}}


def _ctx(*steps: dict[str, Any], offline: bool = True) -> wp.Context:
    steps = steps or (_step("build", "build"),)
    return wp.Context(connection=_Conn(), settings=_Settings(), project_id="p1", actor="ceo",
                      spec={"steps": list(steps), "entry_step": steps[0]["id"]}, run_input={},
                      offline=offline)


SDK = EngineRoute("implement", "claude_agent_sdk", "claude-sonnet-5", "setup-anthropic",
                  "pinned in Repo and models")
SDK_REVIEW = EngineRoute("review_code", "claude_agent_sdk", "claude-sonnet-5",
                         "setup-anthropic", "pinned in Repo and models")
CHAT = EngineRoute("chat", "claude_code", None, "anthropic_claude_code", "auto: subscription")


@pytest.fixture
def routed(monkeypatch):
    """Routing answers: build → the SDK on the key; chat → the subscription."""
    calls: dict[str, int] = {"build": 0, "chat": 0}

    def build_routes(connection, settings, project_id, spec=None, stages=es.BUILD_STAGES):
        calls["build"] += 1
        return [SDK, SDK_REVIEW]

    def chat_route(connection, settings, project_id):
        calls["chat"] += 1
        return CHAT

    monkeypatch.setattr(es, "build_routes", build_routes)
    monkeypatch.setattr(es, "chat_route", chat_route)
    states = {"setup-anthropic": (True, "setup-anthropic key stored"),
              "anthropic_claude_code": (False, "anthropic_claude_code is disconnected")}
    monkeypatch.setattr(es, "source_state", lambda c, p, s: states.get(s, (None, str(s))))
    return calls


# ── registry ──────────────────────────────────────────────────────────────────────────────────

def test_every_agent_step_names_its_route():
    declares = {k for k, v in wp.CAPABILITY_PRECHECKS.items() if "sandbox.engine" in v.checks}
    assert declares == set(wp.AGENT_ROUTE)
    for key in ("build", "review", "more_tests", "architecture", "meeting"):
        assert "sandbox.engine" in wp.precheck_for(key).checks, key


# ── sandbox.model: what routing will use ──────────────────────────────────────────────────────

def test_the_model_check_reports_the_build_route_not_the_chat_answer(routed):
    found = wp.check_model(_ctx(_step("build", "build")), {})
    assert found["verdict"] == "ready", found
    assert ("build: implement, review_code → claude_agent_sdk / claude-sonnet-5 via "
            "setup-anthropic (pinned in Repo and models)") in found["evidence"]
    assert "claude_code" not in found["evidence"]


def test_a_disconnected_source_is_never_reported_connected(routed):
    found = wp.check_model(_ctx(_step("plan", "meeting"), _step("build", "build")), {})
    assert found["verdict"] == "will_fail"
    assert "anthropic_claude_code is disconnected" in found["evidence"]
    assert "plan:chat routes to claude_code" in found["evidence"]
    assert "connected: claude_code" not in found["evidence"]
    assert "Reconnect anthropic_claude_code" in found["fix"]["action"]


def test_nothing_to_route_to_will_fail(monkeypatch, routed):
    monkeypatch.setattr(es, "build_routes", lambda *a, **k: [
        EngineRoute("implement", "", None, None, "nothing this project holds can answer")])
    found = wp.check_model(_ctx(), {})
    assert found["verdict"] == "will_fail" and "Connect a model" in found["fix"]["action"]


def test_unresolvable_routing_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("settings table unreadable")
    monkeypatch.setattr(es, "build_routes", boom)
    assert wp.check_model(_ctx(), {})["verdict"] == "unknown"


def test_the_task_spec_routes_a_literal_pin():
    step = _step("build", "build", model_config={"default": "codex:gpt-5"}, profile="{{x}}")
    assert wp._task_spec(step) == {"model_config": {"default": "codex:gpt-5"}}


# ── sandbox.engine: can it start ──────────────────────────────────────────────────────────────

def test_a_missing_sandbox_binary_will_fail_naming_the_package(routed, monkeypatch):
    monkeypatch.setattr(hr, "missing_runtime", lambda engine: [
        hr.RuntimeBinary("socat", "socat", "apt", "the Claude CLI's sandbox network proxy")]
        if engine == "claude_agent_sdk" else [])
    found = wp.check_engine(_ctx(), _step("build", "build"))
    assert found["verdict"] == "will_fail"
    assert "build: implement, review_code → claude_agent_sdk / claude-sonnet-5 via " \
           "setup-anthropic (pinned in Repo and models)" in found["evidence"]
    assert "claude_agent_sdk needs socat" in found["evidence"]
    assert "apt-get install socat" in found["fix"]["action"]


def test_offline_is_deterministic_and_never_starts_the_engine(routed, monkeypatch):
    monkeypatch.setattr(hr, "missing_runtime", lambda engine: [])

    def never(*a, **k):
        raise AssertionError("the probe ran offline")
    monkeypatch.setattr(es, "probe", never)
    found = wp.check_engine(_ctx(offline=True), _step("build", "build"))
    assert found["verdict"] == "ready"
    assert "runtime binaries on PATH" in found["evidence"] and "offline" in found["evidence"]


def test_live_the_engine_is_started_once_per_engine_and_its_failure_is_its_own(routed,
                                                                             monkeypatch):
    monkeypatch.setattr(hr, "missing_runtime", lambda engine: [])
    started: list[tuple] = []

    def probe(route, settings, connection, project_id, **_k):
        started.append(route.key())
        return es.ProbeResult("will_fail", "RuntimeError: claude agent sdk session failed: "
                                           "Sandbox is required but failed to initialize")
    monkeypatch.setattr(es, "probe", probe)
    ctx = _ctx(_step("build", "build"), _step("more", "more_tests"), offline=False)
    first = wp.check_engine(ctx, ctx.spec["steps"][0])
    second = wp.check_engine(ctx, ctx.spec["steps"][1])
    assert started == [SDK.key()]                       # one session for both steps
    for found in (first, second):
        assert found["verdict"] == "will_fail"
        assert "Sandbox is required but failed to initialize" in found["evidence"]


def test_a_step_that_runs_no_agent_has_no_engine_check(routed):
    assert wp.check_engine(_ctx(), _step("deploy", "deploy")) is None


def test_chat_steps_check_the_chat_engine(routed, monkeypatch):
    monkeypatch.setattr(hr, "missing_runtime", lambda engine: [])
    found = wp.check_engine(_ctx(_step("plan", "meeting")), _step("plan", "meeting"))
    assert "plan: chat → claude_code" in found["evidence"]
    assert routed["chat"] == 1 and routed["build"] == 0


# ── engine_startup.probe ──────────────────────────────────────────────────────────────────────

class _Agent:
    def __init__(self, summary: str = "", exc: Exception | None = None) -> None:
        self.summary, self.exc, self.calls = summary, exc, []

    def run_role(self, prompt, cwd, **kw):
        self.calls.append((prompt, kw))
        if self.exc:
            raise self.exc
        return {"summary": self.summary, "turns": 2, "cost_usd_micros": 1800}


def test_the_probe_runs_one_command_read_only_with_a_tiny_turn_cap():
    agent = _Agent("boss-engine-42")
    out = es.probe(SDK, _Settings(), None, "p1", make_agent=lambda: agent)
    assert out.verdict == "ready" and out.cost_usd_micros == 1800
    prompt, kw = agent.calls[0]
    assert es.PROBE_COMMAND in prompt
    assert kw == {"read_only": True, "model": "claude-sonnet-5", "max_turns": es.PROBE_MAX_TURNS}


@pytest.mark.parametrize("message,verdict", [
    ("Sandbox dependencies not available: socat not installed", "will_fail"),
    ("authentication_error: invalid x-api-key", "will_fail"),
    ("Your credit balance is too low to access the Anthropic API", "will_fail"),
    ("overloaded_error: Overloaded", "unknown"),
    ("claude agent sdk session timed out after 120s", "unknown"),
])
def test_the_probe_reports_the_engines_own_failure(message, verdict):
    out = es.probe(SDK, _Settings(), None, "p1",
                   make_agent=lambda: _Agent(exc=RuntimeError(message)))
    assert out.verdict == verdict and message in out.detail


def test_a_probe_that_never_shows_the_output_is_unknown():
    out = es.probe(SDK, _Settings(), None, "p1", make_agent=lambda: _Agent("done"))
    assert out.verdict == "unknown" and "did not show" in out.detail


def test_the_probe_does_not_leak_its_metering_context():
    from boss_engine.services.llm_telemetry import get_llm_context, set_llm_context

    set_llm_context(stage="implement")
    try:
        es.probe(SDK, _Settings(), None, "p1", make_agent=lambda: _Agent("boss-engine-42"))
        assert get_llm_context().get("stage") == "implement"
    finally:
        set_llm_context(stage=None)


# ── engine_startup.source_state ───────────────────────────────────────────────────────────────

class _Rows:
    def __init__(self, row):
        self.row = row

    def execute(self, *_a, **_k):
        return self

    def mappings(self):
        return self

    def first(self):
        return self.row


@pytest.mark.parametrize("row,usable", [
    ({"status": "active", "connection_status": "connected", "credential_secret_ref_id": None},
     True),
    ({"status": "active", "connection_status": "not_connected",
      "credential_secret_ref_id": "sref_1"}, True),
    ({"status": "active", "connection_status": "disconnected",
      "credential_secret_ref_id": "sref_1"}, False),
    ({"status": "active", "connection_status": "revoked", "credential_secret_ref_id": None},
     False),
    ({"status": "missing_credentials", "connection_status": "connected",
      "credential_secret_ref_id": None}, False),
    ({"status": "declared", "connection_status": "not_connected",
      "credential_secret_ref_id": None}, False),
])
def test_source_state(row, usable):
    assert es.source_state(_Rows(row), "p1", "src")[0] is usable


def test_no_source_is_the_environment():
    assert es.source_state(_Rows(None), "p1", None) == (None, "the install environment")


# ── engine_startup.build_routes: the worker's own routing ─────────────────────────────────────

def test_build_routes_reads_the_pipelines_lane_engines_and_the_job_pin(monkeypatch):
    from boss_engine.services import model_routing, setup_service
    from boss_engine.services import pipeline_profile_service as pps
    from boss_engine.services.model_router import stages, store

    seen = {}

    class S:
        code_agent_strong_engine = code_agent_low_cost_engine = ""
        code_agent_strong_model = code_agent_low_cost_model = ""
        code_agent_model = code_review_model = ""
        agent_sdk_model = ""

    def lane_engines(connection, settings, project_id, spec):
        seen["spec"] = spec
        return S(), "claude_agent_sdk", "claude_agent_sdk"
    monkeypatch.setattr(stages, "lane_engines", lane_engines)
    monkeypatch.setattr(pps, "resolve_for_run", lambda *a: {"name": "balanced", "model": {}})
    monkeypatch.setattr(store, "mode_of", lambda *a: "fixed")
    monkeypatch.setattr(setup_service, "connected_engines", lambda *a: {
        "claude_agent_sdk": {"source_id": "setup-anthropic"}})
    monkeypatch.setattr(model_routing, "resolve_job", lambda c, s, p, job: model_routing
                        .JobResolution("claude_agent_sdk", None, model_routing.CHOSEN,
                                       "setup-anthropic", "metered"))
    routes = es.build_routes(object(), S(), "p1", {"title": "t"}, ("implement",))
    assert routes[0].describe() == ("implement → claude_agent_sdk / claude-sonnet-5 via "
                                    "setup-anthropic (pinned in Repo and models)")
    assert seen["spec"] == {"title": "t"}
