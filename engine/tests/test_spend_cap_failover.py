"""Claude monthly spend-cap engine failover — once, never invented success.

Seams:
  * is_claude_monthly_spend_limit / plan_spend_cap_failover (pure)
  * apply_spend_cap_failover (persist + receipt + single requeue)
  * official POST payload.engine → start_code_task model_config
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from apps.api.app.request_bodies import BenchmarkAppLaunchBody

from boss_engine.services.spend_cap_failover import (
    OFFICIAL_ENGINES,
    apply_spend_cap_failover,
    is_claude_monthly_spend_limit,
    pin_engine_model_config,
    plan_spend_cap_failover,
    resolve_spend_cap_failover_engine,
)


def _settings(**overrides):
    base = dict(
        code_agent_engine="claude_code",
        code_agent_failover_engine="",
        code_agent_low_cost_engine="",
        openai_api_key=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Classification — monthly spend only, not every rate limit
# --------------------------------------------------------------------------- #
def test_classifies_org_and_plain_monthly_spend_limit():
    assert is_claude_monthly_spend_limit(
        "claude code agent exited 1: You've hit your org's monthly spend limit"
    )
    assert is_claude_monthly_spend_limit(
        "You've hit your monthly spend limit. Upgrade or wait."
    )


def test_generic_rate_limit_is_not_monthly_spend_failover():
    assert not is_claude_monthly_spend_limit("rate limit exceeded (429)")
    assert not is_claude_monthly_spend_limit("too many requests")
    assert not is_claude_monthly_spend_limit("tests failed: missing timeout")
    assert not is_claude_monthly_spend_limit("")
    assert not is_claude_monthly_spend_limit(None)


# --------------------------------------------------------------------------- #
# Resolve alternate engine
# --------------------------------------------------------------------------- #
def test_failover_defaults_to_openai_when_key_is_set():
    assert (
        resolve_spend_cap_failover_engine(
            _settings(openai_api_key="sk-test"), current_engine="claude_code"
        )
        == "openai"
    )


def test_explicit_failover_engine_wins():
    assert (
        resolve_spend_cap_failover_engine(
            _settings(
                code_agent_failover_engine="native",
                openai_api_key="sk-test",
            ),
            current_engine="claude_code",
        )
        == "native"
    )


def test_low_cost_engine_used_when_not_claude():
    assert (
        resolve_spend_cap_failover_engine(
            _settings(code_agent_low_cost_engine="openai", openai_api_key=None),
            current_engine="claude_code",
        )
        == "openai"
    )


def test_no_alternate_when_openai_unconfigured():
    assert (
        resolve_spend_cap_failover_engine(
            _settings(), current_engine="claude_code"
        )
        is None
    )


def test_no_alternate_when_current_is_already_not_claude():
    assert (
        resolve_spend_cap_failover_engine(
            _settings(openai_api_key="sk-test"), current_engine="openai"
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Plan — once only, honest None when we cannot switch
# --------------------------------------------------------------------------- #
def test_plan_failsover_once_on_claude_spend_cap():
    plan = plan_spend_cap_failover(
        error="You've hit your org's monthly spend limit",
        current_engine="claude_code",
        settings=_settings(openai_api_key="sk-test"),
        already_failed_over=False,
        run_id="crun_1",
    )
    assert plan is not None
    assert plan["from_engine"] == "claude_code"
    assert plan["to_engine"] == "openai"
    assert plan["reason"] == "claude_monthly_spend_limit"
    assert plan["dedupe_key"] == "code_task:spend_cap_failover:crun_1"
    assert plan["model_config"]["default"]["engine"] == "openai"


def test_plan_none_when_already_failed_over_prevents_loop():
    assert (
        plan_spend_cap_failover(
            error="You've hit your monthly spend limit",
            current_engine="claude_code",
            settings=_settings(openai_api_key="sk-test"),
            already_failed_over=True,
            run_id="crun_1",
        )
        is None
    )


def test_plan_none_without_alternate_stays_honest():
    assert (
        plan_spend_cap_failover(
            error="You've hit your org's monthly spend limit",
            current_engine="claude_code",
            settings=_settings(),
            already_failed_over=False,
            run_id="crun_1",
        )
        is None
    )


def test_pin_engine_rewrites_claude_step_pins():
    pinned = pin_engine_model_config(
        "openai",
        {"default": {"engine": "claude_code"}, "steps": {"implement": {"engine": "claude_code"}}},
    )
    assert pinned["default"]["engine"] == "openai"
    assert pinned["steps"]["implement"]["engine"] == "openai"


# --------------------------------------------------------------------------- #
# Persist + requeue — receipted, once
# --------------------------------------------------------------------------- #
class _Jobs:
    def __init__(self):
        self.calls = []

    def create_job(self, connection, **kwargs):
        self.calls.append(kwargs)
        return {"id": "job_failover"}


class _Receipts:
    def __init__(self):
        self.created = []
        self.succeeded = []

    def create_or_reuse(self, *args, **kwargs):
        self.created.append((args, kwargs))
        return {"id": "rcpt_failover"}, True

    def mark_succeeded(self, *args, **kwargs):
        self.succeeded.append((args, kwargs))


class _Conn:
    def __init__(self):
        self.params = []

    def execute(self, _stmt, params=None):
        self.params.append(params)
        return self

    def mappings(self):
        return self

    def first(self):
        return {"org_id": "org_1", "workspace_id": "ws_1"}


def test_apply_requeues_once_and_receipts(monkeypatch):
    events = []
    monkeypatch.setattr(
        "boss_engine.services.spend_cap_failover.emit_event",
        lambda *a, **k: events.append((a, k)) or "evt_1",
    )
    jobs = _Jobs()
    receipts = _Receipts()
    conn = _Conn()
    out = apply_spend_cap_failover(
        conn,
        jobs=jobs,
        project_id="boss-dogfood",
        run_id="crun_cabf",
        task_id="tsk_1",
        settings=_settings(openai_api_key="sk-test"),
        current_engine="claude_code",
        error="You've hit your org's monthly spend limit",
        spec={},
        receipts=receipts,
    )
    assert out["status"] == "requeued"
    assert out["done"] is True
    assert out["invented_success"] is False
    assert out["to_engine"] == "openai"
    assert out["receipt_id"] == "rcpt_failover"
    assert jobs.calls[0]["kind"] == "code.task"
    assert jobs.calls[0]["payload"]["run_id"] == "crun_cabf"
    assert jobs.calls[0]["dedupe_key"] == "code.task:spend_cap_failover:crun_cabf"
    assert receipts.created
    assert receipts.succeeded
    assert any(
        (a[2] if len(a) > 2 else None) == "code_task.spend_cap_failover"
        for a, _k in events
    )

    # Same run cannot failover again — no second job.
    jobs2 = _Jobs()
    out2 = apply_spend_cap_failover(
        conn,
        jobs=jobs2,
        project_id="boss-dogfood",
        run_id="crun_cabf",
        task_id="tsk_1",
        settings=_settings(openai_api_key="sk-test"),
        current_engine="claude_code",
        error="You've hit your monthly spend limit",
        spec={"spend_cap_failover": {"to_engine": "openai"}},
        receipts=_Receipts(),
    )
    assert out2 is None
    assert jobs2.calls == []


def test_apply_none_without_alternate_does_not_requeue(monkeypatch):
    monkeypatch.setattr(
        "boss_engine.services.spend_cap_failover.emit_event",
        lambda *a, **k: "evt_1",
    )
    jobs = _Jobs()
    out = apply_spend_cap_failover(
        _Conn(),
        jobs=jobs,
        project_id="boss-dogfood",
        run_id="crun_noalt",
        task_id=None,
        settings=_settings(),
        current_engine="claude_code",
        error="You've hit your monthly spend limit",
        spec={},
        receipts=_Receipts(),
    )
    assert out is None
    assert jobs.calls == []


# --------------------------------------------------------------------------- #
# Official / benchmark POST engine override
# --------------------------------------------------------------------------- #
@pytest.fixture()
def benchmark_endpoint(monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main, "database_is_reachable", lambda _e: True)
    monkeypatch.setattr(api_main, "require_project_actor", lambda *a, **k: None)

    class _NoRows:
        def execute(self, *a, **k):
            return self

        def mappings(self):
            return self

        def first(self):
            return None

        def all(self):
            return []

    class _Ctx:
        def __enter__(self):
            return _NoRows()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(api_main, "begin", lambda _e: _Ctx())
    return api_main


def test_official_post_engine_openai_reaches_start_code_task(benchmark_endpoint, monkeypatch):
    started: dict = {}

    def _start(connection, **kwargs):
        started.update(kwargs)
        return "crun_openai"

    monkeypatch.setattr("apps.api.app.services.code_task_service.start_code_task", _start)
    result = benchmark_endpoint.start_qa_benchmark_app_endpoint(
        "bugbox",
        BenchmarkAppLaunchBody.model_validate({
            "project_id": "boss-dogfood",
            "actor": "executive_console",
            "route": "direct",
            "engine": "openai",
            "official": True,
        }),
    )
    assert result["status"] == "queued"
    assert result["run_id"] == "crun_openai"
    assert started["model_config"]["default"]["engine"] == "openai"


def test_unknown_engine_is_422(benchmark_endpoint):
    with pytest.raises(HTTPException) as exc:
        benchmark_endpoint.start_qa_benchmark_app_endpoint(
            "bugbox",
            BenchmarkAppLaunchBody.model_validate({
                "project_id": "boss-dogfood",
                "route": "direct",
                "engine": "not_a_real_engine",
            }),
        )
    assert exc.value.status_code == 422
    assert "unknown engine" in str(exc.value.detail).lower()


def test_simulated_official_post_never_spends(benchmark_endpoint, monkeypatch):
    started: dict = {}

    def _start(connection, **kwargs):
        started.update(kwargs)
        return "crun_sim"

    monkeypatch.setattr("apps.api.app.services.code_task_service.start_code_task", _start)
    result = benchmark_endpoint.start_qa_benchmark_app_endpoint(
        "hookwatch",
        BenchmarkAppLaunchBody.model_validate({
            "project_id": "boss-dogfood",
            "actor": "executive_console",
            "simulated": True,
            "engine": "openai",
            "official": True,
        }),
    )
    assert result["status"] == "simulated"
    assert result["passed"] is False
    assert result["job_enqueued"] is False
    assert started["simulated"] is True
    assert started.get("model_config") is None


def test_official_engines_are_the_documented_three():
    assert OFFICIAL_ENGINES == frozenset({"openai", "native", "claude_code"})


def test_model_config_engine_override_wins_over_lane_default():
    """route_for_step / materialize_route honor payload.engine via model_config."""
    from boss_engine.services.pipeline_profiles import (
        PROFILES,
        materialize_route,
        route_for_step,
    )

    route = route_for_step(
        PROFILES["balanced"], "implement", {"default": {"engine": "openai"}}
    )
    engine_name, _model = materialize_route(
        route, build_engine="claude_code", review_engine="claude_code"
    )
    assert engine_name == "openai"
