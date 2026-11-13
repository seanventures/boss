"""BOSSAPP-863: budgets at org, project, agent and workflow scope, checked mid-stage.

reserve() evaluates every matching scoped policy (a hard-stop one refuses past its cap); a paid
call recorded past a hard-stop cap signals its run to stop at the next turn; warn_pct is an
event once per period; with no policy nothing changes.
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import scoped_budgets as sb

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


@pytest.fixture()
def world(eng):
    sb.reset_for_tests()
    sfx = uuid4().hex[:8]
    w = {"org": f"org_863_{sfx}", "ws": f"ws_863_{sfx}", "p": f"p_863_{sfx}", "q": f"q_863_{sfx}",
         "wf": f"wfd_863_{sfx}", "wfr": f"wfr_863_{sfx}", "act": f"act_863_{sfx}"}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": w["org"]})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
                  {"w": w["ws"], "o": w["org"]})
        for p in (w["p"], w["q"]):
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                      {"p": p, "w": w["ws"]})
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) VALUES "
                           "(:i, :p, 'backend_engineer', 'Backend')"), {"i": f"r_{uuid4().hex[:8]}", "p": p})
        c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) VALUES "
                       "(:i, :p, :i, 'Ship', 't')"), {"i": w["wf"], "p": w["p"]})
        c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
                       " spec, content_hash, created_by) VALUES (:v, :p, :d, 1, '{}', 'h', 't')"),
                  {"v": f"wfv_{sfx}", "p": w["p"], "d": w["wf"]})
        c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                       " workflow_version_id, actor) VALUES (:r, :p, :d, :v, 't')"),
                  {"r": w["wfr"], "p": w["p"], "d": w["wf"], "v": f"wfv_{sfx}"})
        c.execute(text("INSERT INTO agent_acts (act_id, project_id, kind, root_act_id, handed_by_kind, how, "
                       " accountable, label, workflow_run_id) VALUES (:a, :p, 'run', :a, 'person', 'named', "
                       " 'ceo', 'Ship', :r)"), {"a": w["act"], "p": w["p"], "r": w["wfr"]})
    yield w
    sb.reset_for_tests()


def _spend(eng, project, usd, *, role=None, act=None, included=False):
    with eng.begin() as c:
        c.execute(text("INSERT INTO llm_calls (id, project_id, role, act_id, cost_usd_micros, "
                       " included_in_subscription) VALUES (:i, :p, :r, :a, :c, :inc)"),
                  {"i": f"llmc_{uuid4().hex[:16]}", "p": project, "r": role, "a": act,
                   "c": int(usd * DOLLAR), "inc": included})


def _policy(eng, w, scope_type, scope_id, usd, **kw):
    with eng.begin() as c:
        return sb.set_policy(c, org_id=w["org"], scope_type=scope_type, scope_id=scope_id,
                             cap_usd_micros=int(usd * DOLLAR), actor="ceo", **kw)


def test_spend_is_measured_per_scope_and_excludes_included_calls(eng, world):
    _spend(eng, world["p"], 2, role="backend_engineer")
    _spend(eng, world["q"], 3, role="backend_engineer")
    _spend(eng, world["p"], 1, act=world["act"])
    _spend(eng, world["p"], 50, role="backend_engineer", included=True)   # a subscription: $0
    org = _policy(eng, world, "org", "", 100)
    role_everywhere = _policy(eng, world, "role", "backend_engineer", 100)
    role_here = _policy(eng, world, "role", "backend_engineer", 100, project_id=world["p"],
                        period="day")
    proj = _policy(eng, world, "project", world["q"], 100)
    wf = _policy(eng, world, "workflow", world["wf"], 100)
    got = {p["id"]: p["spent_usd_micros"] for p in (org, role_everywhere, role_here, proj, wf)}
    assert got == {org["id"]: 6 * DOLLAR, role_everywhere["id"]: 5 * DOLLAR,
                   role_here["id"]: 2 * DOLLAR, proj["id"]: 3 * DOLLAR, wf["id"]: 1 * DOLLAR}


def test_reserve_refuses_past_any_matching_hard_cap(eng, world):
    from boss_engine.config import get_settings
    from boss_engine.services.budget import BudgetGovernor
    from boss_engine.services.llm_telemetry import set_llm_context

    _spend(eng, world["p"], 4, role="backend_engineer")
    _policy(eng, world, "role", "backend_engineer", 5)
    with eng.begin() as c:
        # another agent is not capped by the backend engineer's budget
        assert sb.reserve_refusal(c, world["p"], 2 * DOLLAR, role="reviewer") is None
        refused = sb.reserve_refusal(c, world["p"], 2 * DOLLAR, role="backend_engineer")
    assert refused["refusal"] == "scoped_cap_exceeded" and refused["scope_type"] == "role"
    set_llm_context(role_key="backend_engineer")
    try:
        with eng.begin() as c:
            out = BudgetGovernor(get_settings()).reserve(c, world["p"], "model", 2 * DOLLAR,
                                                         "a build", 30)
    finally:
        set_llm_context(role_key=None)
    assert out["status"] == "denied" and out["refusal"] == "scoped_cap_exceeded"
    assert "agent backend_engineer" in out["message"]


def test_a_soft_policy_never_refuses(eng, world):
    _spend(eng, world["p"], 9)
    _policy(eng, world, "project", world["p"], 5, hard_stop=False)
    with eng.begin() as c:
        assert sb.reserve_refusal(c, world["p"], DOLLAR) is None


def test_a_paid_call_past_the_cap_stops_the_run_at_its_next_turn(eng, world):
    from boss_engine.services import harness_failures

    _policy(eng, world, "workflow", world["wf"], 3)
    _spend(eng, world["p"], 4, act=world["act"])
    with eng.begin() as c:
        stopped = sb.after_call(c, project_id=world["p"], role=None,
                                workflow_run_id=world["wfr"], run_id="ctr_863")
    assert stopped
    # the run's next turn raises; another run is untouched
    token = harness_failures._RUN.set("ctr_863")
    try:
        with pytest.raises(sb.BudgetStop, match="workflow"):
            harness_failures.check_turn({"direction": "assistant", "content": "next"}, engine="x")
    finally:
        harness_failures._RUN.reset(token)
    token = harness_failures._RUN.set("ctr_other")
    try:
        harness_failures.check_turn({"direction": "assistant", "content": "next"}, engine="x")
    finally:
        harness_failures._RUN.reset(token)
    # a new execution of the run starts clear
    with harness_failures.run_scope("ctr_863"):
        assert sb.stop_for("ctr_863") is None


def test_warn_is_an_event_once_per_period(eng, world):
    _policy(eng, world, "project", world["p"], 10)
    _spend(eng, world["p"], 9)
    for _ in range(3):
        with eng.begin() as c:
            assert sb.after_call(c, project_id=world["p"], role=None, workflow_run_id=None,
                                 run_id="ctr_w") == []
    with eng.begin() as c:
        rows = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                              "event_type = 'budget.scoped_warning'"), {"p": world["p"]}).scalars().all()
    assert len(rows) == 1 and rows[0]["spent_usd_micros"] == 9 * DOLLAR
    assert sb.stop_for("ctr_w") is None


def test_with_no_policy_nothing_is_checked(eng, world):
    with eng.begin() as c:
        c.execute(text("UPDATE scoped_budget_policies SET status = 'removed' WHERE status = 'active'"))
    sb.reset_for_tests()
    with eng.begin() as c:
        assert sb.reserve_refusal(c, world["p"], 10**12) is None
        assert sb.after_call(c, project_id=world["p"], role="backend_engineer",
                             workflow_run_id=world["wfr"], run_id="r") == []


def test_one_active_policy_per_scope_and_bad_scopes_refused(eng, world):
    first = _policy(eng, world, "project", world["p"], 10)
    second = _policy(eng, world, "project", world["p"], 20)
    with eng.begin() as c:
        live = [p for p in sb.list_policies(c, world["org"]) if p["scope_type"] == "project"]
        assert [p["id"] for p in live] == [second["id"]]
        with pytest.raises(sb.ScopedBudgetError):
            sb.set_policy(c, org_id=world["org"], scope_type="role", scope_id="nobody",
                          cap_usd_micros=DOLLAR, actor="ceo")
        with pytest.raises(sb.ScopedBudgetError):
            sb.set_policy(c, org_id=world["org"], scope_type="project", scope_id="p_elsewhere",
                          cap_usd_micros=DOLLAR, actor="ceo")
        assert sb.remove_policy(c, world["org"], second["id"])
    assert json.dumps(first)  # serialisable for the API


def test_the_one_recording_seam_checks_mid_stage(eng, world):
    """record_llm_call (every model call) runs after_call on a paid call, not on an included one."""
    from boss_engine.services.llm_telemetry import record_llm_call, set_llm_context

    _policy(eng, world, "role", "backend_engineer", 1, project_id=world["p"])
    set_llm_context(project_id=world["p"], role_key="backend_engineer", run_id="ctr_seam")
    try:
        with eng.begin() as c:
            record_llm_call(engine_name="claude_code", usage={"billing": "subscription"},
                            cost_usd_micros=5 * DOLLAR, stage="implement", connection=c)
        assert sb.stop_for("ctr_seam") is None              # included: not money
        with eng.begin() as c:
            record_llm_call(engine_name="openai", usage={"billing": "metered"},
                            cost_usd_micros=2 * DOLLAR, stage="implement", connection=c)
    finally:
        set_llm_context(project_id=None, role_key=None, run_id=None)
    assert sb.stop_for("ctr_seam") and "backend_engineer" in sb.stop_for("ctr_seam")
