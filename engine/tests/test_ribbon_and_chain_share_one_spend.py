"""The build ribbon and the act chain name one spend (BOSSAPP-1020).

Three aggregations read the same llm_calls rows: the build list (load_facts), the cockpit
cost line (_cost), and the act chain. Two shapes of that ledger — every call included, and
a mix of money and included — must come back as the same (money, included) pair from each.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import llm_call, pg_engine, project
from boss_engine.services import agent_acts, factory_view

SHAPES = {
    "all_included": [(12_500_000, True), (22_500_000, True)],
    "mixed": [(2_000_000, False), (30_000_000, True)],
}


def _pair(money: float, included: float) -> tuple[float, float]:
    return (round(float(money), 6), round(float(included), 6))


@pytest.fixture(scope="module")
def pg():
    return pg_engine()


def _seed(c, shape: str) -> tuple[str, str]:
    pid = project(c)
    run_id = f"crun_{uuid4().hex[:12]}"
    act_id = f"act_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status) "
        "VALUES (:id, :p, 'Ledger shapes', 'completed')"),
        {"id": run_id, "p": pid})
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
        "handed_by_kind, how, accountable, label, code_task_run_id) "
        "VALUES (:a, :p, 'software_engineer', 'run', 'active', :a, 'boss', 'started', "
        "'owner', 'Build', :r)"),
        {"a": act_id, "p": pid, "r": run_id})
    now = c.execute(text("SELECT now()")).scalar()
    for micros, included in SHAPES[shape]:
        llm_call(c, pid, now, cost=micros, included=included, run_id=run_id, stage="implement")
    c.execute(text("UPDATE llm_calls SET act_id = :a WHERE code_task_run_id = :r"),
              {"a": act_id, "r": run_id})
    return pid, run_id


def _three(c, pid: str, run_id: str) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    run = c.execute(text("SELECT * FROM code_task_runs WHERE id = :id"), {"id": run_id}).mappings().one()
    facts = factory_view.load_facts(c, pid, [dict(run)], acts=False, deploys=False)
    cost = facts.cost[run_id]
    ribbon = _pair(cost.get("money_usd", cost.get("metered_usd", 0)), cost.get("included_usd", -1))
    line = factory_view._cost(c, run_id, {"state": "done"})
    cockpit = _pair(line.get("metered_usd", 0), line.get("included_usd", -1))
    chain = agent_acts.chain_for_code_task(c, pid, run_id)
    assert chain is not None
    totals = chain["totals"]
    credits = _pair(totals["cost"], totals["included"])
    return ribbon, cockpit, credits


@pytest.mark.parametrize("shape", list(SHAPES))
def test_ribbon_cockpit_and_chain_report_the_same_pair(pg, shape):
    with pg.begin() as c:
        pid, run_id = _seed(c, shape)
        ribbon, cockpit, credits = _three(c, pid, run_id)
        money = sum(micros for micros, included in SHAPES[shape] if not included) / 1e6
        included = sum(micros for micros, included in SHAPES[shape] if included) / 1e6
        want = _pair(money, included)
    assert ribbon == want
    assert cockpit == want
    assert credits == want
    assert ribbon == cockpit == credits
