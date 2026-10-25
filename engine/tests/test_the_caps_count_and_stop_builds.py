"""Budget caps count and stop build spend, on Community too (operator decision 2026-10-25).

The code-task pipeline ran on the unmetered budget port (defaults.NoopBudgetPort): a build's
model calls were recorded in llm_calls and never reserved or settled a cost receipt, so no daily
or monthly cap could stop a build — Govern said "Builds $284.01 not capped". Every edition with a
database now meters builds (bootstrap_ports), and every build path asks services/build_budget:

  * the start reserves the code-task estimate at marginal cost under the caps, and a cap with no
    room refuses it in BOSS's cap words;
  * each spending stage boundary renews the hold and stops the build once a cap's money is
    reached (settled money this period + running builds' unsettled money);
  * every exit settles the build's money from its llm_calls under ``code_task:<run>`` — $0 for
    subscription calls, whose list price stays on llm_calls as the included estimate — or
    releases the hold;
  * Govern and get_costs (included_spend.ledger_spend) say one figure, counting nothing twice.
"""
from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from boss_engine.services import build_budget, included_spend

SERVICES = Path(build_budget.__file__).parent
PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
SETTINGS = SimpleNamespace(boss_edition="community", code_task_budget_usd_micros=2_000_000)


# ── the wiring (no database) ─────────────────────────────────────────────────────────────────

def test_every_edition_with_a_database_meters_builds():
    from apps.api.app.adapters_postgres import PostgresBudgetPort
    from apps.api.app.bootstrap_ports import build_ports

    ports = build_ports(SimpleNamespace(boss_edition="community", openai_model="m"), object())
    assert isinstance(ports.budget, PostgresBudgetPort)
    source = (Path(__file__).resolve().parents[2] / "apps/api/app/bootstrap_ports.py").read_text()
    assert "budget=PostgresBudgetPort(settings)," in source  # every edition, not `if paid`


def test_every_spending_stage_boundary_checks_the_caps_and_delivery_does_not():
    source = (SERVICES / "governed_code_task_service.py").read_text()
    # Every phase change is a boundary; only the delivery phases are exempt.
    assert '_abort_if_cancelled(f"phase->{name}", spends=name not in _DELIVERY_PHASES)' in source
    assert 'if spends:\n            _stop_if_cap_reached(where)' in source
    assert "_build_budget.cap_reached(budget, c, project_id)" in source
    assert "_build_budget.renew(budget, c, project_id, reservation_id)" in source
    from boss_engine.services.governed_code_task_service import (_DELIVERY_PHASES,
                                                                 BuildBudgetStopped,
                                                                 CodeTaskCancelled)
    assert _DELIVERY_PHASES == {"opening_pr", "waiting_for_ci"}
    assert issubclass(BuildBudgetStopped, CodeTaskCancelled)  # unwinds like a stop
    honour = source[source.index("    def _honour_cancel(exc):"):]
    assert honour.index("isinstance(exc, BuildBudgetStopped)") < honour.index("_save_workspace(")


def test_no_build_reservation_is_a_magic_number():
    """Each build reserve() is sized by build_budget.reservation_estimate (marginal cost)."""
    for name in ("governed_code_task_service.py", "code_task_service.py", "pr_review_again.py"):
        tree = ast.parse((SERVICES / name).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "reserve"):
                continue
            kws = {k.arg: k.value for k in node.keywords}
            if not (isinstance(kws.get("category"), ast.Constant)
                    and kws["category"].value == "code_task"):
                continue
            amount = ast.unparse(kws["amount_usd_micros"])
            assert "reservation_estimate(" in amount, f"{name}:{node.lineno} reserves {amount}"


# ── the ledger (Postgres) ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def pg(monkeypatch):
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine, text

    from apps.api.app.bootstrap_ports import build_ports
    from boss_engine.db import ensure_schema
    from boss_engine.ports import configure_engine_ports, get_engine_ports

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    pid = "p_bcap_" + uuid4().hex[:8]
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_bcap', 'b') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_bcap', 'org_bcap', 'b') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_bcap', 'b', :p)"), {"p": pid})
    previous = get_engine_ports()
    ports = build_ports(SETTINGS, eng)
    configure_engine_ports(ports)
    # The routed model is metered (an API key) unless a test says it is a subscription.
    monkeypatch.setattr("boss_engine.services.model_routing.call_is_included",
                        lambda *a, **k: False)
    yield SimpleNamespace(engine=eng, project=pid, budget=ports.budget)
    configure_engine_ports(previous)  # each test has its own project; the DB is throwaway


def _policy(c, p, *, daily, monthly, burst=0):
    from sqlalchemy import text

    c.execute(text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                   "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) VALUES "
                   "(:i, 'project', :p, :d, :m, :b)"),
              {"i": "bp_" + uuid4().hex[:12], "p": p, "d": daily, "m": monthly, "b": burst})


def _run(c, p, status="running"):
    from sqlalchemy import text

    rid = "crun_bcap_" + uuid4().hex[:8]
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase) "
                   "VALUES (:i, :p, 'Build', :s, 'implement')"), {"i": rid, "p": p, "s": status})
    return rid


def _call(c, p, run, *, cost, included=False):
    from sqlalchemy import text

    c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, "
                   "cost_usd_micros, included_in_subscription) VALUES "
                   "(:i, :p, :r, 'implement', 'claude_code', :c, :inc)"),
              {"i": "llmc_" + uuid4().hex[:12], "p": p, "r": run, "c": cost, "inc": included})


def _receipt(c, p, *, sid, money, category="model"):
    from sqlalchemy import text

    c.execute(text("INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
                   "estimated_usd_micros, actual_usd_micros, status) VALUES "
                   "(:i, :p, :cat, :s, :a, :a, 'actual')"),
              {"i": "cost_" + uuid4().hex[:12], "p": p, "cat": category, "s": sid, "a": money})


def _reserve_build(env, c, run):
    from sqlalchemy import text

    got = env.budget.reserve(c, env.project, category=build_budget.CATEGORY,
                             amount_usd_micros=build_budget.reservation_estimate(
                                 c, SETTINGS, env.project),
                             reason="build", expires_in_minutes=build_budget.RESERVATION_MINUTES)
    assert got["status"] == "reserved", got
    c.execute(text("UPDATE code_task_runs SET reservation_id = :r WHERE id = :i"),
              {"r": got["reservation_id"], "i": run})
    return got["reservation_id"]


def _reservation(c, rid):
    from sqlalchemy import text

    return c.execute(text("SELECT status, amount_usd_micros, expires_at FROM budget_reservations "
                          "WHERE id = :i"), {"i": rid}).mappings().one()


def _day(env, c):
    return env.budget.status_for_project(c, env.project)["periods"]["day"]


def _ledger(c, p):
    return included_spend.ledger_spend(c, p, datetime.now(timezone.utc) - timedelta(days=1))


def test_a_build_reserves_settles_and_the_caps_count_it(pg):
    from boss_engine.services import govern_view
    from boss_engine.tools import build_default_registry

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        res = _reserve_build(pg, c, run)
        # The hold is the code-task estimate (metered route), under the caps.
        assert _reservation(c, res)["amount_usd_micros"] == 2_000_000
        assert _day(pg, c)["reserved_usd_micros"] == 2_000_000
        _call(c, pg.project, run, cost=1_500_000)
        _call(c, pg.project, run, cost=900_000, included=True)   # a subscription call
        # Running: its money is in flight — under the caps, not "not capped".
        mid = _ledger(c, pg.project)
        assert (mid["in_flight_usd_micros"], mid["not_under_cap_usd_micros"]) == (1_500_000, 0)
        assert build_budget.inflight_money(c, pg.project) == 1_500_000

        build_budget.settle_or_release(pg.budget, c, pg.project, res, "done", run_id=run)
        assert _reservation(c, res)["status"] == "consumed"
        day = _day(pg, c)
        assert (day["actual_usd_micros"], day["reserved_usd_micros"]) == (1_500_000, 0)
        ledger = _ledger(c, pg.project)
        meter = govern_view.ledger_for(c, pg.project, {
            "period_start": datetime.now(timezone.utc) - timedelta(days=1),
            "resets_at": datetime.now(timezone.utc) + timedelta(hours=1)})
    assert ledger == meter
    assert ledger["under_cap_usd_micros"] == 1_500_000
    assert ledger["in_flight_usd_micros"] == ledger["not_under_cap_usd_micros"] == 0
    assert ledger["money_usd_micros"] == 1_500_000              # receipt + llm_calls: once
    assert ledger["included_usd_micros"] == 900_000              # the estimate, kept, not money

    ctx = SimpleNamespace(settings=SimpleNamespace(), begin=pg.engine.begin)
    costs = build_default_registry().get("get_costs").handler(
        ctx, project_id=pg.project, actor="ceo", args={"days": 1})
    assert costs["total_cost_usd"] == pytest.approx(1.5)
    assert costs["not_under_a_cap_usd"] == 0 and costs["in_flight_usd"] == 0


def test_a_second_settle_of_the_same_build_adds_only_the_new_money(pg):
    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        first = _reserve_build(pg, c, run)
        _call(c, pg.project, run, cost=700_000)
        build_budget.settle_or_release(pg.budget, c, pg.project, first, "plan pause", run_id=run)
        second = _reserve_build(pg, c, run)        # resumed after the plan was approved
        _call(c, pg.project, run, cost=300_000)
        build_budget.settle_or_release(pg.budget, c, pg.project, second, "done", run_id=run)
        assert _day(pg, c)["actual_usd_micros"] == 1_000_000
        assert _ledger(c, pg.project)["money_usd_micros"] == 1_000_000


def test_a_full_cap_refuses_the_build_start_in_words(pg):
    from sqlalchemy import text

    from boss_engine.services.code_task_service import refused_reservation

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=1_000_000, monthly=100_000_000)
        _receipt(c, pg.project, sid="model.agent_decision", money=1_200_000)
        run = _run(c, pg.project)
        refused = pg.budget.reserve(c, pg.project, category="code_task",
                                    amount_usd_micros=build_budget.reservation_estimate(
                                        c, SETTINGS, pg.project),
                                    reason="governed code task: Build", expires_in_minutes=45)
    assert refused["status"] == "denied" and refused["refusal"] == "cap_exceeded"
    assert refused["transient"] is False
    out = refused_reservation(pg.engine, None, run_id=run, project_id=pg.project,
                              reservation=refused, fencing_token=None)
    assert out["status"] == "blocked" and out["reason_code"] == "budget_denied"
    with pg.engine.begin() as c:
        row = c.execute(text("SELECT status, error, finalize_ctx FROM code_task_runs "
                             "WHERE id = :i"), {"i": run}).mappings().one()
    assert row["status"] == "blocked"
    # What cap, how much is spent, how to raise it.
    assert "today's budget is used ($1.20 of $1)" in row["error"], row["error"]
    assert "raise the daily cap in Govern › Limits" in row["error"], row["error"]


def test_a_subscription_build_holds_a_gate_and_settles_no_money(pg, monkeypatch):
    from sqlalchemy import text

    from boss_engine.services.receipts import ReceiptService

    monkeypatch.setattr("boss_engine.services.model_routing.call_is_included",
                        lambda *a, **k: True)
    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        res = _reserve_build(pg, c, run)
        assert _reservation(c, res)["amount_usd_micros"] == 0        # a gate, not a hold
        _call(c, pg.project, run, cost=3_000_000, included=True)
        # The pull request opened: its settle is $0 of money, the list price stays included.
        receipt, _ = ReceiptService().create_or_reuse(
            c, "org_bcap", "ws_bcap", pg.project, f"code:task:{run}",
            "github.pull_request.create", {}, {})
        build_budget.settle_on_pull_request(pg.budget, c, pg.project, res, receipt["id"], 0, run)
        cost = c.execute(text("SELECT category, skill_or_model_id, actual_usd_micros FROM "
                              "cost_receipts WHERE reservation_id = :r"), {"r": res}).mappings().one()
        assert dict(cost) == {"category": "code_task", "skill_or_model_id": f"code_task:{run}",
                              "actual_usd_micros": 0}
        ledger = _ledger(c, pg.project)
    assert ledger["money_usd_micros"] == 0
    assert ledger["included_usd_micros"] == 3_000_000                # not counted twice


def test_a_cancelled_or_failed_build_without_money_releases_its_hold(pg):
    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        res = _reserve_build(pg, c, run)
        build_budget.settle_or_release(pg.budget, c, pg.project, res, "cancelled by operator",
                                       run_id=run)
        assert _reservation(c, res)["status"] == "released"
        assert _day(pg, c)["reserved_usd_micros"] == 0
        failed = _reserve_build(pg, c, _run(c, pg.project))
        build_budget.settle_or_release(pg.budget, c, pg.project, failed, "code task error",
                                       run_id=None)
        assert _reservation(c, failed)["status"] == "released"


def test_a_build_crossing_a_cap_mid_run_is_stopped_at_the_boundary(pg):
    from sqlalchemy import text

    from boss_engine.services.factory_view import build_state
    from boss_engine.services.governed_code_task_service import stop_build_for_budget

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=5_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        res = _reserve_build(pg, c, run)
        # Other work's hold is a moment, not spending: it never stops a build.
        pg.budget.reserve(c, pg.project, category="model", amount_usd_micros=2_500_000,
                          reason="a meeting", expires_in_minutes=30)
        _call(c, pg.project, run, cost=4_000_000)
        assert build_budget.cap_reached(pg.budget, c, pg.project) is None
        _call(c, pg.project, run, cost=1_500_000)                    # $5.50 of a $5 day
        reached = build_budget.cap_reached(pg.budget, c, pg.project)
    assert reached is not None and reached["period"] == "day"
    assert reached["message"].startswith("Budget cap reached: today's budget is used ($5.50 of $5).")
    assert "raise the daily cap in Govern › Limits" in reached["message"]

    out = stop_build_for_budget(pg.engine, pg.budget, run_id=run, project_id=pg.project,
                                reservation_id=res, message=reached["message"])
    assert out["status"] == "blocked" and out["reason_code"] == "budget_denied"
    with pg.engine.begin() as c:
        assert _reservation(c, res)["status"] == "consumed"            # money so far settled
        assert _day(pg, c)["actual_usd_micros"] == 5_500_000
        row = c.execute(text("SELECT * FROM code_task_runs WHERE id = :i"),
                        {"i": run}).mappings().one()
    assert "The budget stopped this code task: Budget cap reached" in row["error"]
    assert build_state(dict(row))["state"] == "budget_refused"              # the console's state


def test_a_second_running_build_sees_the_first_ones_unsettled_money(pg):
    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=5_000_000, monthly=100_000_000)
        a, b = _run(c, pg.project), _run(c, pg.project)
        _reserve_build(pg, c, a)
        _reserve_build(pg, c, b)
        _call(c, pg.project, a, cost=3_000_000)
        _call(c, pg.project, b, cost=2_000_000)
        assert build_budget.inflight_money(c, pg.project) == 5_000_000
        assert build_budget.cap_reached(pg.budget, c, pg.project)["period"] == "day"


def test_a_running_build_keeps_its_hold_and_a_lapsed_gate_still_settles(pg):
    from sqlalchemy import text

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        res = _reserve_build(pg, c, run)
        c.execute(text("UPDATE budget_reservations SET expires_at = now() + interval '1 minute' "
                       "WHERE id = :i"), {"i": res})
        build_budget.renew(pg.budget, c, pg.project, res)
        left = _reservation(c, res)["expires_at"] - datetime.now(timezone.utc)
        assert left > timedelta(minutes=build_budget.RESERVATION_MINUTES - 2)
        # A $0 gate that lapsed while its build ran is still settled with the build's money.
        c.execute(text("UPDATE budget_reservations SET amount_usd_micros = 0, status = 'expired', "
                       "expires_at = now() - interval '1 minute' WHERE id = :i"), {"i": res})
        _call(c, pg.project, run, cost=400_000)
        assert build_budget.settle_or_release(pg.budget, c, pg.project, res, "done",
                                              run_id=run) is not None
        assert _day(pg, c)["actual_usd_micros"] == 400_000


def test_a_builds_settlement_is_not_a_burst_but_a_calls_money_is(pg):
    from boss_engine.services.budget import BudgetGovernor

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=100_000_000, monthly=100_000_000, burst=1_000_000)
        _receipt(c, pg.project, sid="code_task:crun_x", money=5_000_000, category="code_task")
        governor = BudgetGovernor(SETTINGS)
        assert governor.burst_window(c, pg.project, 1_000_000)["spent_usd_micros"] == 0
        _receipt(c, pg.project, sid="model.agent_decision", money=300_000)
        assert governor.burst_window(c, pg.project, 1_000_000)["spent_usd_micros"] == 300_000
        # ...and the day still counts the build in full.
        assert _day(pg, c)["actual_usd_micros"] == 5_300_000


def test_earlier_unmetered_build_money_is_kept_and_self_fix_is_its_own(pg):
    from sqlalchemy import text

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        old = _run(c, pg.project, status="completed")                # ran before metering
        _call(c, pg.project, old, cost=2_840_000)
        sf = pg.budget.reserve(c, pg.project, category="self_fix", amount_usd_micros=1_000_000,
                               reason="self-fix sfx_1", expires_in_minutes=60)
        ledger = _ledger(c, pg.project)
        assert ledger["not_under_cap_usd_micros"] == 2_840_000       # history does not vanish
        assert ledger["in_flight_usd_micros"] == 0
        assert build_budget.inflight_money(c, pg.project) == 0       # not a running build
        assert build_budget.cap_reached(pg.budget, c, pg.project) is None  # no cap counts it
        cat = c.execute(text("SELECT category FROM budget_reservations WHERE id = :i"),
                        {"i": sf["reservation_id"]}).scalar()
    assert cat == "self_fix"


def test_a_build_from_before_metering_settles_only_what_it_spends_from_now_on(pg):
    """Resumed or redelivered after the upgrade, an old build's earlier money stays outside the
    caps (it was spent while no cap counted builds) instead of landing on today's cap at once."""
    from sqlalchemy import text

    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, "
                       "cost_usd_micros, included_in_subscription, created_at) VALUES "
                       "(:i, :p, :r, 'implement', 'claude_code', 6000000, false, "
                       " now() - interval '3 hours')"),
                  {"i": "llmc_" + uuid4().hex[:12], "p": pg.project, "r": run})
        res = _reserve_build(pg, c, run)
        build_budget.bind(c, run, res)
    with pg.engine.begin() as c:   # a later transaction: the build's calls from now on
        _call(c, pg.project, run, cost=250_000)
        assert build_budget.unsettled_money(c, pg.project, run) == 250_000
        mid = _ledger(c, pg.project)
        assert (mid["in_flight_usd_micros"], mid["not_under_cap_usd_micros"]) == (250_000, 6_000_000)
        build_budget.settle_or_release(pg.budget, c, pg.project, res, "done", run_id=run)
        assert _day(pg, c)["actual_usd_micros"] == 250_000
        ledger = _ledger(c, pg.project)
    assert ledger["under_cap_usd_micros"] == 250_000
    assert ledger["not_under_cap_usd_micros"] == 6_000_000
    assert ledger["money_usd_micros"] == 6_250_000


def test_settling_a_hold_twice_is_a_no_op_not_a_swallowed_error(pg):
    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=10_000_000, monthly=100_000_000)
        run = _run(c, pg.project)
        res = _reserve_build(pg, c, run)
        _call(c, pg.project, run, cost=100_000)
        assert build_budget.settle_or_release(pg.budget, c, pg.project, res, "done", run_id=run)
        # A re-entry settles the earlier attempt's hold first: already settled, nothing owed.
        assert build_budget.settle_or_release(pg.budget, c, pg.project, res, "re-entry",
                                              run_id=run) is None
        assert _day(pg, c)["actual_usd_micros"] == 100_000


def test_every_build_start_settles_an_earlier_attempts_hold_before_it_reserves():
    for name in ("governed_code_task_service.py", "code_task_service.py"):
        source = (SERVICES / name).read_text()
        start = source.index("re-entry: the earlier attempt's hold settled")
        assert source.index("_build_budget.bind(c, run_id, reservation_id)", start) > start, name
