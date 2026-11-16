"""BOSSAPP-1247: one member's monthly build cap does not stop the rest of the org.

The project monthly cap (budget_policies.monthly_cap_usd_micros) stays. A member cap is
separate. Unset — never written, or cleared — means that member is not capped. A build
reservation (category code_task) that would pass a set cap is refused for that member only.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.services.budget import (
    BUILD_CATEGORY,
    BudgetGovernor,
    member_monthly_cap,
    set_member_monthly_cap,
)

URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL.startswith("postgresql") or "test" not in URL.lower(),
    reason="needs DATABASE_URL naming a disposable test database",
)

DOLLAR = 1_000_000


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


def _world(eng, *, monthly: int, daily: int | None = None):
    """One org, one workspace, one project, with the project caps named here."""
    sfx = uuid4().hex[:8]
    world = {
        "org": f"org_1247_{sfx}",
        "ws": f"ws_1247_{sfx}",
        "project": f"p_1247_{sfx}",
        "a": f"mem_a_{sfx}",
        "b": f"mem_b_{sfx}",
    }
    daily_cap = monthly if daily is None else daily
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": world["org"]})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
                  {"w": world["ws"], "o": world["org"]})
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
            {"p": world["project"], "w": world["ws"]})
        c.execute(text(
            "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
            "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) VALUES "
            "(:i, 'project', :p, :d, :m, 0)"),
            {"i": f"bp_{sfx}", "p": world["project"], "d": daily_cap, "m": monthly})
    return world


def _second_project(eng, world, *, monthly: int):
    sfx = uuid4().hex[:8]
    project = f"p2_1247_{sfx}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
            {"p": project, "w": world["ws"]})
        c.execute(text(
            "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
            "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) VALUES "
            "(:i, 'project', :p, :m, :m, 0)"),
            {"i": f"bp_{sfx}", "p": project, "m": monthly})
    return project


def _reserve(eng, project, member, amount):
    governor = BudgetGovernor(get_settings())
    with eng.begin() as c:
        return governor.reserve(
            c, project, BUILD_CATEGORY, amount, "build", 30, member_id=member)


def test_member_a_is_refused_at_the_cap_while_member_b_still_reserves(eng):
    """A at the cap is refused; B in the same org, and A's non-build spend, still reserve.

    Breaks if the member cap is ignored, applied to every member, or applied to every category.
    """
    world = _world(eng, monthly=100 * DOLLAR)
    other = _second_project(eng, world, monthly=100 * DOLLAR)
    with eng.begin() as c:
        set_member_monthly_cap(c, world["org"], world["a"], DOLLAR)
        assert member_monthly_cap(c, world["org"], world["a"]) == DOLLAR
        assert member_monthly_cap(c, world["org"], world["b"]) is None
    first = _reserve(eng, world["project"], world["a"], 600_000)
    assert first["status"] == "reserved", first
    over = _reserve(eng, other, world["a"], 500_000)
    assert over["status"] == "denied", over
    assert over["refusal"] == "member_monthly_cap"
    assert over["member_id"] == world["a"]
    assert over["transient"] is False
    still = _reserve(eng, other, world["b"], 500_000)
    assert still["status"] == "reserved", still
    governor = BudgetGovernor(get_settings())
    with eng.begin() as c:
        chat = governor.reserve(
            c, world["project"], "model", 500_000, "not a build", 30, member_id=world["a"])
    assert chat["status"] == "reserved", chat


def test_an_unset_cap_does_not_refuse_and_clearing_one_lifts_it(eng):
    """No row, and a cap set back to None, both mean that member is not capped."""
    world = _world(eng, monthly=100 * DOLLAR)
    huge = 50 * DOLLAR
    never = _reserve(eng, world["project"], world["b"], huge)
    assert never["status"] == "reserved", never
    with eng.begin() as c:
        set_member_monthly_cap(c, world["org"], world["a"], DOLLAR)
        set_member_monthly_cap(c, world["org"], world["a"], None)
        assert member_monthly_cap(c, world["org"], world["a"]) is None
    cleared = _reserve(eng, world["project"], world["a"], huge)
    assert cleared["status"] == "reserved", cleared


def test_the_project_monthly_cap_still_applies(eng):
    """monthly_cap_usd_micros still refuses a build the member cap would allow.

    Breaks if the member cap replaces the project monthly cap. The month is already
    spent (a receipt), so the refusal is the project's cap, not another member's.
    """
    world = _world(eng, monthly=DOLLAR, daily=100 * DOLLAR)
    with eng.begin() as c:
        set_member_monthly_cap(c, world["org"], world["a"], 50 * DOLLAR)
        c.execute(text(
            "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
            "estimated_usd_micros, actual_usd_micros, status) "
            "VALUES (:i, :p, :cat, 'spent', :a, :a, 'actual')"),
            {"i": "cost_" + uuid4().hex[:12], "p": world["project"],
             "cat": BUILD_CATEGORY, "a": DOLLAR})
    refused = _reserve(eng, world["project"], world["a"], 1)
    assert refused["status"] == "denied", refused
    assert refused["refusal"] == "cap_exceeded"
    assert refused["period"] == "month"