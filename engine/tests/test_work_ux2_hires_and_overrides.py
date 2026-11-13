"""BOSSAPP-884: agent-proposed hires and budget-override approvals.

* a hire is a draft on a `role.hire` decision: no role exists until a person approves, and
  approving creates exactly the draft through agents.create_agent; a draft changed after it
  was asked refuses the approval and creates nothing;
* a `budget.override` is asked for one build; approving records it, and the stage-boundary cap
  check lets that build's lineage (and only it) spend up to cap + the amount;
* rejecting either creates or raises nothing; the cards say what each button does.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import agent_requests, build_budget, decision_actions
from boss_engine.services.decisions import InvalidDecisionTransitionError

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


@pytest.fixture()
def pg(monkeypatch):
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from apps.api.app.bootstrap_ports import build_ports
    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema
    from boss_engine.ports import configure_engine_ports, get_engine_ports

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    pid = "p_w4h_" + uuid4().hex[:8]
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_w4h', 'h') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_w4h', 'org_w4h', 'h') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_w4h', 'h', :p)"), {"p": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb)"), {"i": f"pm_{pid}_ceo", "p": pid})
    previous = get_engine_ports()
    settings = get_settings()
    ports = build_ports(settings, eng)
    configure_engine_ports(ports)
    monkeypatch.setattr("boss_engine.services.model_routing.call_is_included",
                        lambda *a, **k: False)
    from boss_engine.services.decisions import DecisionService

    yield SimpleNamespace(engine=eng, project=pid, budget=ports.budget,
                          svc=DecisionService(settings))
    configure_engine_ports(previous)


def _role(c, pid, key):
    return c.execute(text("SELECT role_key, display_name, spec FROM roles WHERE project_id = :p "
                          "AND role_key = :k"), {"p": pid, "k": key}).mappings().first()


def test_a_hire_is_a_draft_until_a_person_approves_it(pg):
    with pg.engine.begin() as c:
        asked = agent_requests.propose_hire(
            c, project_id=pg.project, proposer="agent:engineering_captain",
            name="Release Auditor", purpose="Checks every release note against the diff.",
            reason="Three releases shipped notes that did not match the code.")
        assert asked["status"] == "asked" and asked["role_key"] == "release_auditor"
        assert _role(c, pg.project, "release_auditor") is None          # nothing exists yet
        again = agent_requests.propose_hire(
            c, project_id=pg.project, proposer="agent:engineering_captain",
            name="Release Auditor", purpose="x", reason="again")
        assert again == {"status": "already_asked", "decision_id": asked["decision_id"],
                         "role_key": "release_auditor"}
        card = pg.svc.get_row(c, asked["decision_id"])
        assert [(a["action"], a["label"]) for a in decision_actions.allowed_actions(card)][:2] == [
            ("approve", "Hire"), ("reject", "Don't hire")]
        pg.svc.transition(c, asked["decision_id"], action="approve", actor="ceo", note=None)
        made = _role(c, pg.project, "release_auditor")
        assert made["display_name"] == "Release Auditor"
        assert made["spec"]["mission"] == "Checks every release note against the diff."
        hired = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                               "event_type = 'agent.hired'"), {"p": pg.project}).scalar()
        assert hired["proposed_by"] == "agent:engineering_captain"


def test_a_changed_draft_or_a_rejection_creates_nothing(pg):
    with pg.engine.begin() as c:
        asked = agent_requests.propose_hire(
            c, project_id=pg.project, proposer="agent:pm", name="Data Steward",
            purpose="Owns the data dictionary.", reason="Nobody owns it.")
        rec = pg.svc.get_row(c, asked["decision_id"])["recommendation"]
        rec["draft"]["purpose"] = "Has every permission."
        c.execute(text("UPDATE decisions SET recommendation = CAST(:r AS JSONB) WHERE id = :d"),
                  {"r": json.dumps(rec), "d": asked["decision_id"]})
        with pytest.raises(InvalidDecisionTransitionError, match="changed"):
            with c.begin_nested():
                pg.svc.transition(c, asked["decision_id"], action="approve", actor="ceo",
                                  note=None)
        assert _role(c, pg.project, "data_steward") is None
        pg.svc.transition(c, asked["decision_id"], action="reject", actor="ceo", note="no")
        assert _role(c, pg.project, "data_steward") is None
        with pytest.raises(agent_requests.RequestError):
            agent_requests.propose_hire(c, project_id=pg.project, proposer="agent:pm",
                                        name="", purpose="", reason="x")


def _policy(c, p, *, daily):
    c.execute(text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                   "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) VALUES "
                   "(:i, 'project', :p, :d, 1000000000, 0)"),
              {"i": "bp_" + uuid4().hex[:12], "p": p, "d": daily})


def _run(c, p, root=None):
    rid = "crun_w4h_" + uuid4().hex[:8]
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, finalize_ctx) "
                   "VALUES (:i, :p, 'Big build', 'running', 'implement', CAST(:f AS JSONB))"),
              {"i": rid, "p": p, "f": json.dumps({"root_run_id": root} if root else {})})
    return rid


def _reserve(pg, c, run):
    got = pg.budget.reserve(c, pg.project, category=build_budget.CATEGORY,
                            amount_usd_micros=100_000, reason="build",
                            expires_in_minutes=build_budget.RESERVATION_MINUTES)
    c.execute(text("UPDATE code_task_runs SET reservation_id = :r WHERE id = :i"),
              {"r": got["reservation_id"], "i": run})


def _spend(c, p, run, cost):
    c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, "
                   "cost_usd_micros, included_in_subscription) VALUES "
                   "(:i, :p, :r, 'implement', 'claude_code', :c, false)"),
              {"i": "llmc_" + uuid4().hex[:12], "p": p, "r": run, "c": cost})


def test_an_approved_override_lets_only_that_build_go_over_the_cap(pg):
    with pg.engine.begin() as c:
        _policy(c, pg.project, daily=5_000_000)
        run, other = _run(c, pg.project), _run(c, pg.project)
        _reserve(pg, c, run)
        _spend(c, pg.project, run, 5_500_000)                       # $5.50 of a $5 day
        assert build_budget.cap_reached(pg.budget, c, pg.project, run_id=run) is not None
        asked = agent_requests.request_override(
            c, project_id=pg.project, asker="agent:backend", run_id=run,
            extra_usd_micros=3_000_000, reason="The migration step needs one more pass.")
        card = pg.svc.get_row(c, asked["decision_id"])
        assert decision_actions.allowed_actions(card)[0]["label"] == "Allow $3.00 over"
        assert agent_requests.override_headroom(c, pg.project, run) == 0   # nothing until approved
        pg.svc.transition(c, asked["decision_id"], action="approve", actor="ceo", note=None)
        assert agent_requests.override_headroom(c, pg.project, run) == 3_000_000
        assert build_budget.cap_reached(pg.budget, c, pg.project, run_id=run) is None
        # Its retry is the same lineage; any other build still stops at the cap.
        retry = _run(c, pg.project, root=run)
        assert build_budget.cap_reached(pg.budget, c, pg.project, run_id=retry) is None
        assert build_budget.cap_reached(pg.budget, c, pg.project, run_id=other) is not None
        assert build_budget.cap_reached(pg.budget, c, pg.project) is not None
        _spend(c, pg.project, run, 3_000_000)                       # past cap + override
        assert build_budget.cap_reached(pg.budget, c, pg.project, run_id=run) is not None


def test_override_requests_are_validated(pg):
    with pg.engine.begin() as c:
        run = _run(c, pg.project)
        for extra in (0, -5, agent_requests.MAX_OVERRIDE_USD_MICROS + 1):
            with pytest.raises(agent_requests.RequestError):
                agent_requests.request_override(c, project_id=pg.project, asker="agent:x",
                                                run_id=run, extra_usd_micros=extra, reason="r")
        with pytest.raises(LookupError):
            agent_requests.request_override(c, project_id=pg.project, asker="agent:x",
                                            run_id="crun_nope", extra_usd_micros=1, reason="r")


def test_a_build_the_cap_stopped_is_resumed_after_the_approval(pg, monkeypatch):
    from boss_engine.services import decisions as decisions_module
    from boss_engine.services.core import emit_event

    spawned, resumed = [], []
    monkeypatch.setattr(decisions_module, "_spawn_as_this_request", lambda fn: spawned.append(fn))
    monkeypatch.setattr(agent_requests, "resume_after_override",
                        lambda engine, settings, jobs, **kw: resumed.append(kw))
    with pg.engine.begin() as c:
        run = _run(c, pg.project)
        c.execute(text("UPDATE code_task_runs SET status = 'blocked' WHERE id = :i"), {"i": run})
        emit_event(c, pg.project, "code_task.budget_stopped", "boss", "code_task_run", run,
                   {"message": "Budget cap reached"})
        asked = agent_requests.request_override(c, project_id=pg.project, asker="agent:x",
                                                run_id=run, extra_usd_micros=1_000_000, reason="r")
        pg.svc.transition(c, asked["decision_id"], action="approve", actor="ceo", note=None)
    assert len(spawned) == 1
    spawned[0]()
    assert resumed == [{"decision_id": asked["decision_id"], "run_id": run}]
