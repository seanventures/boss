"""BOSSAPP-1295: a team budget (an identity group), the $2,000 pilot preset, and a month's forecast
for the org, a team or a project, with the costliest runs.

A team's spend is its projects' spend: the projects the group, or any member, holds a
project-scope binding on. A hard-stop team cap refuses new work and never raises a card.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import scoped_budgets as sb
from boss_engine.services import spend_forecast as sf

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")

DOLLAR = 1_000_000


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


def scope_check_block() -> str:
    from boss_engine.config import get_settings
    from boss_engine.db import split_statements

    sql = get_settings().schema_path.read_text(encoding="utf-8")
    blocks = [s for s in split_statements(sql) if "scoped_budget_policies_scope_type_check" in s]
    assert len(blocks) == 1, "one definition of the scoped budget scope check"
    return blocks[0]


def apply_scope_check(eng) -> None:
    """Run this tree's scope check as ensure_schema does (the raw statement, no parameters).
    The shared test server also serves suites from older trees, whose boots put back a check
    without `team`; each test re-asserts this tree's."""
    raw = eng.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
            cursor.execute(scope_check_block())
        raw.commit()
    finally:
        raw.close()


@pytest.fixture()
def world(eng):
    apply_scope_check(eng)
    sb.reset_for_tests()
    sfx = uuid4().hex[:8]
    w = {"org": f"org_1295_{sfx}", "ws": f"ws_1295_{sfx}", "a": f"pa_1295_{sfx}",
         "b": f"pb_1295_{sfx}", "c": f"pc_1295_{sfx}", "team": f"grp_1295_{sfx}",
         "ann": f"usr_ann_{sfx}", "bo": f"usr_bo_{sfx}", "out": f"usr_out_{sfx}"}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": w["org"]})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
                  {"w": w["ws"], "o": w["org"]})
        for p in (w["a"], w["b"], w["c"]):
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                      {"p": p, "w": w["ws"]})
        for u in (w["ann"], w["bo"], w["out"]):
            c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                           "VALUES (:u, :o, 'user', 'active', :u)"), {"u": u, "o": w["org"]})
        c.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source) VALUES "
                       "(:g, :o, 'preview', 'Preview team', 'manual')"), {"g": w["team"], "o": w["org"]})
        for u in (w["ann"], w["bo"]):
            c.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source) "
                           "VALUES (:g, :u, 'manual')"), {"g": w["team"], "u": u})
        # Ann works in A; the whole group is bound to B; an outsider works in C.
        for subject_type, subject, project in (("principal", w["ann"], w["a"]),
                                               ("group", w["team"], w["b"]),
                                               ("principal", w["out"], w["c"])):
            c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, "
                           "role_id, scope_type, scope_id, granted_by) VALUES (:i, :o, :st, :s, "
                           "'arl_operator', 'project', :p, 'test')"),
                      {"i": "bnd_" + uuid4().hex[:16], "o": w["org"], "st": subject_type,
                       "s": subject, "p": project})
    yield w
    with eng.begin() as c:   # a team row left behind would fail an older schema's check rewrite
        c.execute(text("DELETE FROM scoped_budget_policies WHERE org_id LIKE :o"), {"o": f"{w['org']}%"})
    sb.reset_for_tests()


def _spend(eng, project, usd, *, run=None, included=False, at=None):
    with eng.begin() as c:
        c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, cost_usd_micros, "
                       " included_in_subscription, created_at) VALUES (:i, :p, :r, :c, :inc, "
                       " COALESCE(CAST(:at AS timestamptz), now()))"),
                  {"i": f"llmc_{uuid4().hex[:16]}", "p": project, "r": run, "c": int(usd * DOLLAR),
                   "inc": included, "at": at})


def test_team_spend_rolls_up_its_members_projects(eng, world):
    _spend(eng, world["a"], 3)
    _spend(eng, world["b"], 4)
    _spend(eng, world["c"], 100)                    # an outsider's project: not the team's
    _spend(eng, world["a"], 50, included=True)      # a subscription call: $0
    with eng.begin() as c:
        assert sb.team_projects(c, world["team"]) == sorted([world["a"], world["b"]])
        policy = sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id=world["team"],
                               cap_usd_micros=10 * DOLLAR, actor="ceo")
    assert policy["spent_usd_micros"] == 7 * DOLLAR
    assert policy["scope_name"] == "Preview team"


def test_a_team_from_another_org_is_refused(eng, world):
    with eng.begin() as c, pytest.raises(sb.ScopedBudgetError, match="team"):
        sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id="grp_nope",
                      cap_usd_micros=DOLLAR, actor="ceo")


def test_the_pilot_preset_is_a_2000_monthly_hard_stop_team_cap():
    body = sb.apply_preset({"preset": "pilot_2000", "scope_id": "grp_x"})
    assert body["scope_type"] == "team" and body["cap_usd_micros"] == 2_000 * DOLLAR
    assert body["period"] == "month" and body["hard_stop"] is True and body["warn_pct"] == 0.8
    # what the caller names wins
    assert sb.apply_preset({"preset": "pilot_2000", "cap_usd_micros": 5 * DOLLAR})["cap_usd_micros"] == 5 * DOLLAR
    # an explicit False / 0.0 is a choice, not a blank (review M3)
    named = sb.apply_preset({"preset": "pilot_2000", "hard_stop": False, "warn_pct": 0.5})
    assert named["hard_stop"] is False and named["warn_pct"] == 0.5
    with pytest.raises(sb.ScopedBudgetError):
        sb.apply_preset({"preset": "nope"})


def test_a_hard_team_cap_refuses_new_work_without_asking_anyone(eng, world):
    _spend(eng, world["a"], 9)
    with eng.begin() as c:
        sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id=world["team"],
                      cap_usd_micros=10 * DOLLAR, actor="ceo")
        # a project outside the team is not capped by it
        assert sb.reserve_refusal(c, world["c"], 5 * DOLLAR) is None
        refused = sb.reserve_refusal(c, world["b"], 2 * DOLLAR)
        cards = c.execute(text("SELECT COUNT(*) FROM decisions WHERE project_id = ANY(:p)"),
                          {"p": [world["a"], world["b"]]}).scalar()
    assert refused["refusal"] == "scoped_cap_exceeded" and refused["scope_type"] == "team"
    assert refused["decision_id"] is None and cards == 0
    assert "team Preview team" in refused["message"]


def test_a_soft_team_cap_only_warns(eng, world):
    _spend(eng, world["a"], 9)
    with eng.begin() as c:
        sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id=world["team"],
                      cap_usd_micros=10 * DOLLAR, hard_stop=False, actor="ceo")
        assert sb.reserve_refusal(c, world["a"], 5 * DOLLAR) is None
        stopped = sb.after_call(c, project_id=world["a"], role=None, workflow_run_id=None,
                                run_id="ctr_soft_1295")
    assert stopped == [] and sb.stop_for("ctr_soft_1295") is None


def test_month_forecast_for_a_team_with_its_costliest_runs(eng, world):
    now = datetime.now(timezone.utc)
    with eng.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:r, :p, 'Onboard API')"),
                  {"r": f"ctr_big_{world['a']}", "p": world["a"]})
    _spend(eng, world["a"], 6, run=f"ctr_big_{world['a']}")
    _spend(eng, world["a"], 2, run=f"ctr_big_{world['a']}")
    _spend(eng, world["b"], 1, run=f"ctr_small_{world['b']}")
    _spend(eng, world["b"], 1)                       # no run: counted in spend, not in runs
    _spend(eng, world["c"], 40, run="ctr_outsider")
    with eng.begin() as c:
        sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id=world["team"],
                      cap_usd_micros=2_000 * DOLLAR, actor="ceo")
        got = sf.month_forecast(c, world["org"], "team", world["team"], now=now)
        org = sf.month_forecast(c, world["org"], "org", "", now=now)
        proj = sf.month_forecast(c, world["org"], "project", world["c"], now=now)
    assert got["label"] == "Team Preview team"
    assert got["spent_usd_micros"] == 10 * DOLLAR
    assert got["cap_usd_micros"] == 2_000 * DOLLAR and got["hard_stop"] is True
    assert got["run_count"] == 2 and got["avg_run_usd_micros"] == int(4.5 * DOLLAR)
    assert [r["money_usd_micros"] for r in got["runs"]] == [8 * DOLLAR, 1 * DOLLAR]
    assert got["runs"][0]["title"] == "Onboard API" and got["runs"][0]["kind"] == "build"
    rate = got["spent_usd_micros"] / max(got["days_elapsed"], 1.0)
    assert abs(got["forecast_usd_micros"] - (got["spent_usd_micros"] + rate * got["days_left"])) <= 0.01 * DOLLAR  # days are rounded
    assert got["state"] == "on_track"
    assert org["spent_usd_micros"] == 50 * DOLLAR and org["cap_usd_micros"] is None and org["state"] == "no_cap"
    assert proj["spent_usd_micros"] == 40 * DOLLAR


def test_forecast_state_words():
    assert sf.state_of(10, 20, None, 0.8) == "no_cap"
    assert sf.state_of(100, 120, 100, 0.8) == "at_cap"
    assert sf.state_of(50, 120, 100, 0.8) == "over_forecast"
    assert sf.state_of(85, 95, 100, 0.8) == "near"
    assert sf.state_of(10, 40, 100, 0.8) == "on_track"


def test_forecast_refuses_a_project_or_team_outside_the_org(eng, world):
    with eng.begin() as c:
        with pytest.raises(sb.ScopedBudgetError):
            sf.month_forecast(c, world["org"], "project", "p_elsewhere")
        with pytest.raises(sb.ScopedBudgetError):
            sf.month_forecast(c, world["org"], "team", "grp_elsewhere")
        teams = sb.list_teams(c, world["org"])
    assert {t["id"]: t["members"] for t in teams} == {world["team"]: 2}
    assert teams[0]["projects"] == sorted([world["a"], world["b"]])


def test_team_spend_counts_only_the_orgs_projects(eng, world):
    """A member bound to a project of ANOTHER org: that project's spend is not the team's, so
    the cap (spent) and the forecast agree (review M4)."""
    other_org, other_ws, other_p = (f"{world['org']}_x", f"{world['ws']}_x", f"{world['a']}_x")
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'x')"), {"o": other_org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'x')"),
                  {"w": other_ws, "o": other_org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                  {"p": other_p, "w": other_ws})
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                       "scope_type, scope_id, granted_by) VALUES (:i, :o, 'principal', :u, 'arl_operator', "
                       "'project', :p, 'test')"),
                  {"i": "bnd_" + uuid4().hex[:16], "o": other_org, "u": world["ann"], "p": other_p})
    _spend(eng, world["a"], 3)
    _spend(eng, other_p, 500)
    with eng.begin() as c:
        policy = sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id=world["team"],
                               cap_usd_micros=10 * DOLLAR, actor="ceo")
        got = sf.month_forecast(c, world["org"], "team", world["team"])
    assert policy["spent_usd_micros"] == got["spent_usd_micros"] == 3 * DOLLAR


def test_a_project_forecast_reads_the_projects_own_monthly_cap(eng, world):
    with eng.begin() as c:
        c.execute(text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                       "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) VALUES (:i, 'project', :p, "
                       "50000000, 300000000, 1000000)"), {"i": f"bp_{uuid4().hex[:8]}", "p": world["a"]})
        got = sf.month_forecast(c, world["org"], "project", world["a"])
        assert got["cap_usd_micros"] == 300 * DOLLAR and got["cap_source"] == "project_budget"
        sb.set_policy(c, org_id=world["org"], scope_type="project", scope_id=world["a"],
                      cap_usd_micros=100 * DOLLAR, actor="ceo")
        scoped = sf.month_forecast(c, world["org"], "project", world["a"])
    assert scoped["cap_usd_micros"] == 100 * DOLLAR and scoped["cap_source"] == "scoped_budget"


def test_the_schema_applies_twice_with_a_team_budget_present(eng, world):
    """ONE definition of the scope check in schema.sql, and it holds a `team` row: run twice
    with one present it neither fails nor rewrites the constraint (review S1). (Read from the
    file and run here, not through a whole ensure_schema: the shared test server's other
    boots record their own failures in schema_apply_runs.)"""
    scope_check_block()   # exactly one definition
    with eng.begin() as c:
        sb.set_policy(c, org_id=world["org"], scope_type="team", scope_id=world["team"],
                      cap_usd_micros=DOLLAR, actor="ceo")
    apply_scope_check(eng)
    apply_scope_check(eng)
    with eng.begin() as c:
        check = c.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'scoped_budget_policies_scope_type_check' "
            "AND conrelid = 'scoped_budget_policies'::regclass")).scalars().all()
    assert len(check) == 1 and "'team'" in check[0]
