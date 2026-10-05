"""Every governance control enforces exactly as it did before the Govern redesign.

THE PARITY GATE. The Govern screen was rebuilt (one board, effect previews, explicit Save), and
the rule for that rebuild was that no control is loosened, removed or re-routed. This suite was
written and run GREEN on the base commit first, then run unchanged on the redesign branch; the
two runs must match test for test.

Each test sets a control the way the console does — the same HTTP call, the same body — and then
asserts the ENGINE's effect, not the screen's. Where the effect lives inside a long pipeline
function (the review panel's parallelism, the review-round count), the test evaluates the exact
expression on the line the pipeline runs, read from the module's source, in the module's own
namespace — so a later refactor that moves the arithmetic into a helper is still held to the
same numbers.

What this suite deliberately does NOT cover, because the operator decided to change it (and it is
pinned elsewhere): the token-policy write gaining `budget_manage`
(test_the_decided_governance_tightenings.py) and optional If-Match on writes
(test_govern_writes_guard_a_stale_edit.py).
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from apps.api.app.db import begin
from boss_engine.services import governed_code_task_service as gcts
from boss_engine.services import project_settings as ps
from boss_engine.services.budget import BudgetGovernor, release_budget_holds
from boss_engine.services.pipeline_profiles import (
    PROFILES,
    apply_budget_pressure,
    route_for_step,
)

PROJECT = "boss-dogfood"
NOW = datetime(2031, 5, 20, 12, 0, tzinfo=timezone.utc)
CONTROL_KEYS = ("code_agent_max_concurrency", "code_agent_max_turns",
                "captain_leads_projects", "code_review_inrun_max_rounds")


# ── helpers ─────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def knobs(engine):
    """Every control knob back to 'not set' before and after."""
    def clear():
        with engine.begin() as c:
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p "
                           "AND key = ANY(:k)"), {"p": PROJECT, "k": list(CONTROL_KEYS)})
    clear()
    yield
    clear()


def _put_knob(client, key: str, value, *, headers=None, actor: str = "ceo"):
    """The console's request: PUT /v1/settings/{key} with {key, project_id, actor, value}."""
    return client.put(f"/v1/settings/{key}", headers=headers or {},
                      json={"key": key, "project_id": PROJECT, "actor": actor, "value": value})


def _stored(engine, key: str):
    with engine.begin() as c:
        return c.execute(text("SELECT value FROM project_settings WHERE project_id = :p "
                              "AND key = :k"), {"p": PROJECT, "k": key}).scalar()


def _pipeline_expression(target: str, must_mention: str) -> ast.Expression:
    """The right-hand side of `target = …` on the pipeline line that mentions `must_mention`."""
    tree = ast.parse(inspect.getsource(gcts))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == target
                and must_mention in ast.unparse(node.value)):
            found.append(node.value)
    assert len(found) == 1, f"expected one `{target} = …` mentioning {must_mention}: {found}"
    return ast.Expression(found[0])


def _eval_pipeline(target: str, must_mention: str, **local_names):
    expr = _pipeline_expression(target, must_mention)
    code = compile(ast.fix_missing_locations(expr), "<pipeline line>", "eval")
    return eval(code, dict(vars(gcts)), dict(local_names))  # noqa: S307 — our own source


def _effective(engine, key: str):
    with engine.begin() as c:
        return ps.effective(c, PROJECT, get_settings(), key)


def _applied(engine):
    with engine.begin() as c:
        return ps.applied(c, PROJECT, get_settings())


@pytest.fixture
def ledger(engine):
    """Clean money and holds; `caps(daily, monthly, burst)` in dollars."""
    def clean():
        with engine.begin() as c:
            c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
            c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"),
                      {"p": PROJECT})
            c.execute(text("DELETE FROM budget_burst_waiters WHERE project_id = :p"),
                      {"p": PROJECT})

    def caps(daily: float, monthly: float, burst: float = 0) -> None:
        with engine.begin() as c:
            c.execute(text(
                "UPDATE budget_policies SET daily_cap_usd_micros = :d, "
                "monthly_cap_usd_micros = :m, burst_cap_usd_micros_per_min = :b "
                "WHERE scope_type = 'project' AND scope_id = :p"),
                {"d": int(daily * 1e6), "m": int(monthly * 1e6), "b": int(burst * 1e6),
                 "p": PROJECT})

    clean()
    caps(10, 200)
    yield caps
    clean()


def _receipt(c, amount: int, at=None, included: bool = False) -> None:
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
        "estimated_usd_micros, actual_usd_micros, status, created_at, included_in_subscription) "
        "VALUES (:i, :p, 'model', 'char.test', :a, :a, 'actual', COALESCE(:at, now()), :inc)"),
        {"i": f"cost_{uuid4().hex[:12]}", "p": PROJECT, "a": amount, "at": at, "inc": included})


# ── how hard agents work ────────────────────────────────────────────────────────────────────

def test_review_lanes_floor_is_six(client, engine, knobs):
    """`Agents at once` is review-panel parallelism with a floor of 6, capped by the panel."""
    nine_checks = list(range(9))
    for stored in (1, 2, 3, 4, 5, 6):
        assert _put_knob(client, "code_agent_max_concurrency", stored).status_code == 200
        assert _stored(engine, "code_agent_max_concurrency") == stored
        lanes = _effective(engine, "code_agent_max_concurrency")
        assert lanes == stored
        assert _eval_pipeline("max_workers", "_lanes", checks=nine_checks, _lanes=lanes) == 6
    assert _put_knob(client, "code_agent_max_concurrency", 10).status_code == 200
    lanes = _effective(engine, "code_agent_max_concurrency")
    assert _eval_pipeline("max_workers", "_lanes", checks=list(range(12)), _lanes=lanes) == 10
    # Capped by the size of the panel.
    assert _eval_pipeline("max_workers", "_lanes", checks=list(range(4)), _lanes=lanes) == 4
    # Not set: the deployment default (1 here) behaves as the floor too.
    assert _put_knob(client, "code_agent_max_concurrency", None).status_code == 200
    assert _stored(engine, "code_agent_max_concurrency") is None
    lanes = _effective(engine, "code_agent_max_concurrency")
    assert _eval_pipeline("max_workers", "_lanes", checks=nine_checks, _lanes=lanes) == 6
    assert _eval_pipeline("max_workers", "_lanes", checks=nine_checks, _lanes=None) == 6


def test_turn_budget_reaches_the_agent(client, engine, knobs, monkeypatch, tmp_path):
    """The project's turn budget is the `--max-turns` the coding CLI is started with."""
    from boss_engine.services import code_agent as code_agent_module

    class _Stop(BaseException):
        pass

    seen: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        seen.append([str(part) for part in cmd])
        raise _Stop()

    monkeypatch.setattr(code_agent_module.subprocess, "Popen", fake_popen)

    def max_turns_used() -> int:
        seen.clear()
        agent = code_agent_module.ClaudeCodeAgent(_applied(engine))
        with pytest.raises(_Stop):
            agent._invoke("characterize the turn budget", tmp_path)
        argv = seen[-1]
        return int(argv[argv.index("--max-turns") + 1])

    assert _put_knob(client, "code_agent_max_turns", 37).status_code == 200
    assert _applied(engine).code_agent_max_turns == 37
    assert max_turns_used() == 37
    assert _put_knob(client, "code_agent_max_turns", "").status_code == 200
    assert _stored(engine, "code_agent_max_turns") is None
    assert _applied(engine).code_agent_max_turns == 90
    assert max_turns_used() == 90


def test_captain_binds_only_when_set(client, engine, knobs, monkeypatch):
    from apps.api.app.services.jobs import JobLeaseService
    from boss_engine.services import agent_process
    from boss_engine.services.code_task_service import start_code_task

    bound: list[str] = []

    def fake_bind(connection, **kwargs):
        bound.append(kwargs.get("attach_run_id"))
        return {"bound": True}

    monkeypatch.setattr(agent_process, "bind_captain_swarm", fake_bind)
    jobs = JobLeaseService(get_settings())

    def start() -> str:
        with begin(engine) as c:
            return start_code_task(
                c, project_id=PROJECT, task_id=None, title=f"char {uuid4().hex[:6]}",
                instructions=f"characterize the captain {uuid4().hex}",
                acceptance_criteria=[], test_commands=[], owner="o", repo="r",
                base_branch="main", finalize_ctx=None, jobs=jobs, substrate="local",
                allow_duplicate=True)

    assert _put_knob(client, "captain_leads_projects", 1).status_code == 200
    run = start()
    assert bound == [run]
    bound.clear()
    assert _put_knob(client, "captain_leads_projects", 0).status_code == 200
    start()
    assert bound == []
    assert _put_knob(client, "captain_leads_projects", None).status_code == 200
    start()
    assert bound == []


def test_review_rounds_zero_reads_as_two(client, engine, knobs):
    for stored, used in ((0, 2), (1, 1), (3, 3), (5, 5)):
        assert _put_knob(client, "code_review_inrun_max_rounds", stored).status_code == 200
        settings = _applied(engine)
        assert _eval_pipeline("inrun_max", "code_review_inrun_max_rounds",
                              settings=settings) == used
    assert _put_knob(client, "code_review_inrun_max_rounds", None).status_code == 200
    assert _eval_pipeline("inrun_max", "code_review_inrun_max_rounds",
                          settings=_applied(engine)) == 2


def test_settings_bounds_and_permission(client, engine, knobs, monkeypatch):
    cases = {
        ("code_agent_max_concurrency", 0): "Agents at once has to be between 1 and 16.",
        ("code_agent_max_concurrency", 17): "Agents at once has to be between 1 and 16.",
        ("code_agent_max_turns", 9): "Turn budget per agent has to be between 10 and 200.",
        ("code_agent_max_turns", 201): "Turn budget per agent has to be between 10 and 200.",
        ("captain_leads_projects", 2): "Captain leads every project has to be between 0 and 1.",
        ("code_review_inrun_max_rounds", 6): "Review rounds has to be between 0 and 5.",
        ("code_agent_max_turns", "lots"): "Turn budget per agent has to be a whole number.",
    }
    for (key, value), sentence in cases.items():
        response = _put_knob(client, key, value)
        assert response.status_code == 422, (key, value, response.text)
        assert response.json()["detail"] == sentence
        assert _stored(engine, key) is None
    unknown = _put_knob(client, "no_such_knob", 3)
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "'no_such_knob' is not a setting BOSS knows about."

    # Empty deletes the row (the project hands the knob back to the default).
    assert _put_knob(client, "code_agent_max_turns", 50).status_code == 200
    assert _stored(engine, "code_agent_max_turns") == 50
    cleared = _put_knob(client, "code_agent_max_turns", "")
    assert cleared.status_code == 200 and cleared.json()["source"] == "deployment default"
    assert _stored(engine, "code_agent_max_turns") is None

    # providerops_manage is required: a member without it is refused, with nothing written.
    _authenticated(monkeypatch)
    _member(engine, "char_budget_only", ["budget_manage"])
    refused = _put_knob(client, "code_agent_max_turns", 60,
                        headers=_auth("budget-key"), actor="char_budget_only")
    assert refused.status_code == 403
    assert _stored(engine, "code_agent_max_turns") is None
    _member(engine, "char_provider", ["providerops_manage"])
    allowed = _put_knob(client, "code_agent_max_turns", 60,
                        headers=_auth("provider-key"), actor="char_provider")
    assert allowed.status_code == 200, allowed.text
    assert _stored(engine, "code_agent_max_turns") == 60


# ── spend caps ──────────────────────────────────────────────────────────────────────────────

def test_daily_and_monthly_caps_refuse_as_before(engine, ledger):
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        _receipt(c, 9_500_000)
        refused = governor.reserve(c, PROJECT, "model", 1_000_000, "char daily", 60)
    assert refused["status"] == "denied" and refused["refusal"] == "cap_exceeded"
    assert refused["transient"] is False and refused["decision_id"]
    assert refused["period"] == "day"
    assert refused["message"].startswith("Budget cap exceeded: today's budget is 95% used")
    assert "It resets at 00:00 UTC" in refused["message"]
    assert refused["message"].endswith("Decision created.")
    with engine.begin() as c:
        title = c.execute(text("SELECT question FROM decisions WHERE id = :d"),
                          {"d": refused["decision_id"]}).scalar()
    assert title == "Budget reservation denied"

    # Other work's holds fill the cap: transient headroom, no decision.
    ledger(10, 200)
    with engine.begin() as c:
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        held = governor.reserve(c, PROJECT, "model", 9_000_000, "char hold", 60)
        assert held["status"] == "reserved", held
        headroom = governor.reserve(c, PROJECT, "model", 2_000_000, "char second", 60)
    assert headroom["refusal"] == "headroom" and headroom["transient"] is True
    assert headroom["retry_after_seconds"] == 60 and headroom["decision_id"] is None

    # The monthly cap measures the month: a quiet day does not hide a spent month.
    ledger(100, 20)
    with engine.begin() as c:
        c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"), {"p": PROJECT})
        _receipt(c, 19_500_000, datetime.now(timezone.utc).replace(day=1, hour=0, minute=5)
                 if datetime.now(timezone.utc).day > 1 else None)
        monthly = governor.reserve(c, PROJECT, "model", 1_000_000, "char month", 60)
    assert monthly["refusal"] == "cap_exceeded" and monthly["period"] == "month"
    assert "this month's budget" in monthly["message"]


def test_burst_cap_makes_work_wait(engine, ledger):
    governor = BudgetGovernor(api_main.settings)
    ledger(100, 2000, burst=0.10)
    with engine.begin() as c:
        _receipt(c, 150_000)  # $0.15 settled in the trailing minute, over $0.10/min
        first = governor.reserve(c, PROJECT, "model", 10_000, "char burst a", 60)
    assert first["status"] == "denied" and first["refusal"] == "burst"
    assert first["transient"] is True and first["decision_id"] is None
    assert 5 <= first["retry_after_seconds"] <= 60
    assert first["message"] == "Burst cap exceeded ($/min). Retry shortly."
    # A subscription call is not money and never fills the window.
    with engine.begin() as c:
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM budget_burst_waiters WHERE project_id = :p"), {"p": PROJECT})
        _receipt(c, 5_000_000, included=True)
        ok = governor.reserve(c, PROJECT, "model", 10_000, "char burst b", 60)
    assert ok["status"] == "reserved", ok
    # FIFO: an earlier waiter keeps its place once the window has room.
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO budget_burst_waiters (id, project_id, waiter_key, job_id, "
            "first_refused_at, retry_at) VALUES ('bwait_char', :p, 'model:someone else', NULL, "
            "clock_timestamp() - interval '20 seconds', clock_timestamp() + interval '20 seconds')"),
            {"p": PROJECT})
        queued = governor.reserve(c, PROJECT, "model", 10_000, "char burst c", 60)
    assert queued["refusal"] == "burst" and queued["transient"] is True
    assert queued["message"].startswith("Waiting for the per-minute budget")


# Golden values computed on the base commit (budget.set_intensity), never re-derived here.
PACE_GOLDEN = {
    0: (5_000_000, 100_000_000, 500_000, 1, 1, ["gpt-4o-mini"]),
    33: (168_000_000, 3_360_000_000, 3_635_000, 7, 11, ["gpt-4o-mini"]),
    34: (173_000_000, 3_460_000_000, 3_730_000, 7, 11, ["gpt-4o-mini", "gpt-4o"]),
    50: (252_000_000, 5_040_000_000, 5_250_000, 11, 15, ["gpt-4o-mini", "gpt-4o"]),
    66: (332_000_000, 6_640_000_000, 6_770_000, 14, 20, ["gpt-4o-mini", "gpt-4o"]),
    67: (337_000_000, 6_740_000_000, 6_865_000, 14, 20,
         ["gpt-4o-mini", "gpt-4o", "gpt-5.2-codex"]),
    100: (500_000_000, 10_000_000_000, 10_000_000, 20, 30,
          ["gpt-4o-mini", "gpt-4o", "gpt-5.2-codex"]),
}


def test_pace_writes_the_same_envelope(client, engine):
    for intensity, golden in PACE_GOLDEN.items():
        response = client.post("/v1/budget/set_intensity",
                               json={"scope_id": PROJECT, "intensity": intensity, "actor": "ceo"})
        assert response.status_code == 200, response.text
        body = response.json()
        got = (body["daily_cap_usd_micros"], body["monthly_cap_usd_micros"],
               body["burst_cap_usd_micros_per_min"], body["max_concurrent_jobs"],
               body["max_workers_total"], body["allowed_model_tiers"])
        assert got == golden, (intensity, got)
        with engine.begin() as c:
            row = c.execute(text(
                "SELECT daily_cap_usd_micros, monthly_cap_usd_micros, "
                "burst_cap_usd_micros_per_min, max_concurrent_jobs, max_workers_total, "
                "allowed_model_tiers, operational_intensity FROM budget_policies "
                "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT}).mappings().one()
            event = c.execute(text(
                "SELECT actor, data FROM events WHERE project_id = :p "
                "AND event_type = 'budget.intensity_set' ORDER BY created_at DESC LIMIT 1"),
                {"p": PROJECT}).mappings().one()
        assert (row["daily_cap_usd_micros"], row["monthly_cap_usd_micros"],
                row["burst_cap_usd_micros_per_min"], row["max_concurrent_jobs"],
                row["max_workers_total"], list(row["allowed_model_tiers"])) == golden
        assert row["operational_intensity"] == intensity
        assert event["actor"] == "ceo" and event["data"]["intensity"] == intensity
    # Out-of-range is clamped, not refused.
    clamped = client.post("/v1/budget/set_intensity",
                          json={"scope_id": PROJECT, "intensity": 140, "actor": "ceo"})
    assert clamped.json()["operational_intensity"] == 100


def test_monthly_cap_has_one_writer_semantics(client, engine, ledger):
    with engine.begin() as c:
        job_id = f"job_char_{uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, priority, payload, run_after, "
            "last_error) VALUES (:id, :p, 'docs.update', 'queued', 50, '{}'::jsonb, "
            "now() + interval '10 minutes', '{\"kind\": \"budget_hold\"}'::jsonb)"),
            {"id": job_id, "p": PROJECT})
    for bad, sentence in ((0, "Between $0.01 and $1,000,000."),
                          (1_000_001, "Between $0.01 and $1,000,000."),
                          ("x", "monthly_cap_usd")):
        response = client.put("/v1/setup/budget",
                              json={"project_id": PROJECT, "monthly_cap_usd": bad})
        assert response.status_code == 422, response.text
        assert sentence in json.dumps(response.json())
    response = client.put("/v1/setup/budget", json={"project_id": PROJECT, "monthly_cap_usd": 150})
    assert response.status_code == 200, response.text
    with engine.begin() as c:
        cap = c.execute(text("SELECT monthly_cap_usd_micros FROM budget_policies "
                             "WHERE scope_type = 'project' AND scope_id = :p"),
                        {"p": PROJECT}).scalar_one()
        run_after_now = c.execute(text("SELECT run_after <= now() FROM jobs WHERE id = :j"),
                                  {"j": job_id}).scalar_one()
        event = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND event_type = "
            "'budget.monthly_cap_set' ORDER BY created_at DESC LIMIT 1"),
            {"p": PROJECT}).scalar_one()
        c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
    assert cap == 150_000_000
    assert run_after_now is True, "a cap change releases budget holds"
    assert event["monthly_cap_usd_micros"] == 150_000_000


def test_thresholds_act_as_before(engine, ledger):
    governor = BudgetGovernor(api_main.settings)
    states = {}
    for spent, expected in ((0, "green"), (7_000_000, "yellow"), (8_500_000, "orange"),
                            (9_000_000, "red")):
        with engine.begin() as c:
            c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
            if spent:
                _receipt(c, spent)
            states[spent] = governor.status_for_project(c, PROJECT)["budget_state"]
        assert states[spent] == expected, (spent, states)
    # Cheaper builds: a strong build step goes low-cost under orange/red; verification never.
    route = route_for_step(PROFILES["strict"], "implement", None)
    assert route["model_class"] == "strong"
    for state, lite in (("green", False), ("yellow", False), ("orange", True), ("red", True)):
        out, degraded = apply_budget_pressure(route, "implement", state)
        assert degraded is lite and out["model_class"] == ("low_cost" if lite else "strong")
    review = route_for_step(PROFILES["balanced"], "review_code", None)
    assert apply_budget_pressure(review, "review_code", "red") == (review, False)
    # Risky work stops at 95%: tier-2 refused, tier-1 still flows.
    with engine.begin() as c:
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        _receipt(c, 9_550_000)
        status = governor.status_for_project(c, PROJECT)
        assert status["tier2_frozen"] is True
        frozen = governor.reserve(c, PROJECT, "model", 10_000, "char t2", 60, risk_tier=2)
        low = governor.reserve(c, PROJECT, "model", 10_000, "char t1", 60, risk_tier=1)
    assert frozen["refusal"] == "tier2_frozen" and frozen["decision_id"]
    assert low["status"] == "reserved", low
    # The job gate: orange defers non-critical work 300s; red defers non-p0 900s.
    from boss_engine.services.jobs import JobLeaseService
    gate = JobLeaseService(get_settings())
    with engine.begin() as c:
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"), {"p": PROJECT})
        _receipt(c, 8_600_000)
        orange = gate._budget_state_policy_for_job(c, PROJECT, None, "job_x", "docs.update")
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        _receipt(c, 9_200_000)
        red = gate._budget_state_policy_for_job(c, PROJECT, None, "job_x", "docs.update")
    assert orange["defer_seconds"] == 300 and orange["reason"] == "orange_defers_non_critical_work"
    assert red["defer_seconds"] == 900 and red["reason"] == "red_halts_non_p0_work"


def test_token_guides_stay_advisory(client, engine):
    """A per-call token policy shapes alerts and tips; nothing that runs a model reads it."""
    put = client.put("/v1/token-efficiency/policies", json={
        "project_id": PROJECT, "actor": "ceo",
        "policies": [{"stage": "implement", "max_total_tokens_per_call": 1000}]})
    assert put.status_code == 200, put.text
    policies = client.get("/v1/token-efficiency/policies", params={"project_id": PROJECT}).json()
    assert any(p.get("stage") == "implement" and p.get("max_total_tokens_per_call") == 1000
               for p in policies.get("policies", policies.get("items", [])))
    services = Path(inspect.getfile(ps)).parent
    readers = sorted(
        path.name for path in services.glob("*.py")
        if "token_budget_policies" in path.read_text(encoding="utf-8")
    )
    assert readers == ["token_efficiency.py"], readers


def test_allocation_rules_unchanged(engine, ledger):
    governor = BudgetGovernor(api_main.settings)
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=30)
    with engine.begin() as c:
        with pytest.raises(ValueError, match="amount_usd_micros must be greater than zero."):
            governor.create_allocation(c, PROJECT, "department", "eng", 0, start, end, "ceo")
        with pytest.raises(ValueError, match="period_end must be after period_start."):
            governor.create_allocation(c, PROJECT, "department", "eng", 1, end, start, "ceo")
        with pytest.raises(ValueError, match="Root allocation exceeds project budget cap."):
            governor.create_allocation(c, PROJECT, "department", "eng", 11_000_000, start, end,
                                       "ceo")
        root = governor.create_allocation(c, PROJECT, "department", "eng", 8_000_000, start,
                                          end, "ceo")
        with pytest.raises(ValueError, match="Child allocation exceeds parent available budget."):
            governor.create_allocation(c, PROJECT, "initiative", "x", 9_000_000, start, end,
                                       "ceo", parent_allocation_id=root["id"])
        child = governor.create_allocation(c, PROJECT, "initiative", "x", 3_000_000, start,
                                           end, "ceo", parent_allocation_id=root["id"])
        refused = governor.reserve(c, PROJECT, "model", 4_000_000, "char alloc", 60,
                                   allocation_id=child["id"])
        fits = governor.reserve(c, PROJECT, "model", 2_000_000, "char alloc ok", 60,
                                allocation_id=child["id"])
        c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM budget_allocations WHERE project_id = :p"), {"p": PROJECT})
    assert refused["refusal"] == "allocation_exceeded" and refused["decision_id"]
    assert refused["message"] == "Budget allocation exceeded. Decision created."
    assert fits["status"] == "reserved"


def test_initiative_gate_unchanged(client):
    def ask(tier: int, dollars: float) -> dict:
        response = client.post("/v1/finops/initiative-budget", json={
            "project_id": PROJECT, "title": f"char {tier} {dollars}", "tier": tier,
            "amount_usd_micros": int(dollars * 1_000_000), "actor": "ceo"})
        assert response.status_code == 201, response.text
        return response.json()

    gated = ask(2, 50)
    assert gated["status"] == "pending_approval" and gated["decision_id"]
    assert gated["approval_required"] is True
    assert ask(3, 60)["status"] == "pending_approval"
    assert ask(1, 500)["status"] == "auto_approved"
    small = ask(2, 49)
    assert small["status"] == "auto_approved" and small["decision_id"] is None


def test_write_permissions_unchanged(client, engine, knobs, monkeypatch):
    """knobs: providerops_manage; pace, finops, allocations, setup budget: budget_manage."""
    _authenticated(monkeypatch)
    _member(engine, "char_provider", ["providerops_manage"])
    _member(engine, "char_budget_only", ["budget_manage"])
    provider, budget_only = _auth("provider-key"), _auth("budget-key")

    def status(method: str, path: str, body: dict, headers) -> int:
        return client.request(method, path, json=body, headers=headers).status_code

    pace = {"scope_id": PROJECT, "intensity": 40}
    assert status("POST", "/v1/budget/set_intensity",
                  {**pace, "actor": "char_provider"}, provider) == 403
    assert status("POST", "/v1/budget/set_intensity",
                  {**pace, "actor": "char_budget_only"}, budget_only) == 200
    recon = {"project_id": PROJECT}
    assert status("POST", "/v1/finops/reconcile-commitments",
                  {**recon, "actor": "char_provider"}, provider) == 403
    assert status("POST", "/v1/finops/reconcile-commitments",
                  {**recon, "actor": "char_budget_only"}, budget_only) == 201
    initiative = {"project_id": PROJECT, "title": "char perm", "tier": 1,
                  "amount_usd_micros": 1_000_000}
    assert status("POST", "/v1/finops/initiative-budget",
                  {**initiative, "actor": "char_provider"}, provider) == 403
    assert status("POST", "/v1/finops/initiative-budget",
                  {**initiative, "actor": "char_budget_only"}, budget_only) == 201
    setup = {"project_id": PROJECT, "monthly_cap_usd": 120}
    assert status("PUT", "/v1/setup/budget", {**setup, "actor": "char_provider"}, provider) == 403
    assert status("PUT", "/v1/setup/budget",
                  {**setup, "actor": "char_budget_only"}, budget_only) == 200
    now = datetime.now(timezone.utc)
    allocation = {"project_id": PROJECT, "scope_type": "department", "scope_id": "char",
                  "amount_usd_micros": 1_000_000, "period_start": now.isoformat(),
                  "period_end": (now + timedelta(days=1)).isoformat()}
    assert status("POST", "/v1/budget/allocations",
                  {**allocation, "actor": "char_provider"}, provider) == 403
    assert status("POST", "/v1/budget/allocations",
                  {**allocation, "actor": "char_budget_only"}, budget_only) == 201
    knob = {"key": "code_agent_max_turns", "project_id": PROJECT, "value": 44}
    assert status("PUT", "/v1/settings/code_agent_max_turns",
                  {**knob, "actor": "char_budget_only"}, budget_only) == 403
    assert status("PUT", "/v1/settings/code_agent_max_turns",
                  {**knob, "actor": "char_provider"}, provider) == 200
    # A member with budget rights may write token guides (the tightening keeps this true).
    token = {"project_id": PROJECT, "actor": "char_budget_only",
             "policies": [{"stage": "implement", "max_total_tokens_per_call": 64000}]}
    assert status("PUT", "/v1/token-efficiency/policies", token, budget_only) == 200
    with engine.begin() as c:
        c.execute(text("DELETE FROM budget_allocations WHERE project_id = :p"), {"p": PROJECT})


# ── auth helpers ────────────────────────────────────────────────────────────────────────────

def _authenticated(monkeypatch) -> None:
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key,provider-key,budget-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        "ceo-key:ceo,provider-key:char_provider,budget-key:char_budget_only")
    monkeypatch.setattr(api_main.settings, "worker_principal", "ceo")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _member(engine, member_id: str, permissions: list[str]) -> None:
    with engine.begin() as c:
        api_main.members.upsert_member(
            c, project_id=PROJECT, member_id=member_id, display_name=member_id,
            member_type="human", role_keys=[], permissions=permissions, actor="ceo")
