"""AN ORG CHART THAT CARRIES AUTHORITY (BOSSAPP-885), on a real Postgres.

The chart's `reports_to` was display-only. Here it means four things, each proven on the database:

* the chain — `chain_of_command` walks it (cycle-safe, retired-aware, ends at a person), with
  `acting_manager_for`, `can_take_over` and `is_under_retired_manager`;
* escalation walks the chain — a stalled build is offered to its agent's manager (an agent)
  before a person is interrupted; a pass or a deadline moves it one rung up; an exhausted chain
  falls through to the person path, unchanged (time is moved by passing `now`);
* takeover — the work item's assignee and the running runs move to the manager, and the
  authority path (who acted, under whose chain) is recorded on the run's lineage record;
* retired managers block delegation — a new build for an agent under a retired manager is refused
  in words naming the gap; reassigning it lets it start; a retry (the running work) is not refused.

And THE DEFAULT IS UNCHANGED: with no reporting lines the person path is byte-identical to the
path with the chain switched off, and nothing is written to org_chain_escalations.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import (code_task_service, org_assignments, org_chain, person_wait,
                                  stuck_work, work_items)

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name

REACH_OUT = "supervisor_reach_out"
QUESTION = "The build keeps failing the same contract test; which API version is right?"


def _later(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _roles(c, project, *keys, retired=()):
    for k in keys:
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, lifecycle) "
                       "VALUES (:id, :p, :k, :n, :l)"),
                  {"id": f"role_{uuid4().hex[:10]}", "p": project, "k": k,
                   "n": k.replace("_", " ").title(), "l": "retired" if k in retired else "active"})


def _org(c, project, lines):
    """lines: [(agent, reports_to)] — set top-down through the real writer."""
    for agent, parent in lines:
        org_assignments.set_one(c, project, agent, reports_to=parent, actor="ceo")


def _build(c, project, *, role="qa", title="Fix the orders contract", item=True, status="running"):
    rid = f"crun_885_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx) "
        "VALUES (:id, :p, :s, 'implement', :t, CAST(:spec AS jsonb), '{}'::jsonb)"),
        {"id": rid, "p": project, "t": title, "s": status,
         "spec": json.dumps({"owner": "acme", "repo": "orders", "role_key": role})})
    wid = None
    if item:
        made = work_items.create_item(c, project_id=project, title=title, assignee=f"agent:{role}",
                                      responsible_person="person:usr_param", status="active",
                                      actor="person:ceo")
        wid = made["id"]
        c.execute(text("UPDATE code_task_runs SET work_item_id = :w WHERE id = :r"),
                  {"w": wid, "r": rid})
    return rid, wid


def _budget():
    released: list = []
    return SimpleNamespace(release=lambda **kw: released.append(kw), released=released)


def _stall(c, project, rid, *, chain=True):
    return person_wait.enter_waiting_on_person(
        c, _budget(), run_id=rid, project_id=project, task_id=None, reason=REACH_OUT,
        question=QUESTION, chain=chain)


def _run(c, rid):
    return c.execute(text("SELECT status, phase, finalize_ctx, decision_id FROM code_task_runs "
                          "WHERE id = :r"), {"r": rid}).mappings().one()


def _offer(c, rid):
    return c.execute(text("SELECT * FROM org_chain_escalations WHERE run_id = :r"),
                     {"r": rid}).mappings().first()


def _cards(c, project):
    return c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p"),
                     {"p": project}).scalar()


# ── 1. the chain ─────────────────────────────────────────────────────────────────────────────

def test_the_chain_walks_up_and_ends_at_a_person(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", "cto")
        _org(c, project, [("cto", "person:usr_param"), ("engineering_captain", "cto"),
                          ("qa", "engineering_captain")])
        chain = org_chain.chain_of_command(c, project, "qa")
        assert [r["party"] for r in chain] == ["agent:qa", "agent:engineering_captain",
                                               "agent:cto", "person:usr_param"]
        assert [r["kind"] for r in chain] == ["agent", "agent", "agent", "person"]
        assert org_chain.acting_manager_for(c, project, "qa")["id"] == "engineering_captain"
        assert org_chain.is_under_retired_manager(c, project, "agent:qa") is None
        # A person at the top is not placed on the chart as a seat.
        assert "person:usr_param" not in org_assignments.get_all(c, project)
        # Nobody declared: the chain is the agent alone.
        assert [r["party"] for r in org_chain.chain_of_command(c, project, "designer")] == [
            "agent:designer"]


def test_a_cycle_ends_the_walk_instead_of_looping(eng, project):
    with eng.begin() as c:
        # Written raw: set_one refuses cycles, but a chart edited elsewhere must not hang BOSS.
        for a, b in (("a", "b"), ("b", "c"), ("c", "a")):
            c.execute(text("INSERT INTO org_assignments (project_id, agent_id, reports_to) "
                           "VALUES (:p, :a, :b)"), {"p": project, "a": a, "b": b})
        chain = org_chain.chain_of_command(c, project, "a")
        assert [r["id"] for r in chain] == ["a", "b", "c"]
        assert chain[-1].get("cycle") is True


def test_a_retired_manager_is_passed_over_and_named_as_the_gap(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", "cto", retired=("engineering_captain",))
        _org(c, project, [("cto", None), ("engineering_captain", "cto"),
                          ("qa", "engineering_captain")])
        chain = org_chain.chain_of_command(c, project, "qa")
        assert [r["retired"] for r in chain] == [False, True, False]
        assert org_chain.acting_manager_for(c, project, "qa")["id"] == "cto"
        assert org_chain.is_under_retired_manager(c, project, "qa")["id"] == "engineering_captain"
        assert org_chain.is_under_retired_manager(c, project, "cto") is None


def test_who_may_take_over_a_reports_work(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", "cto", "designer")
        _org(c, project, [("cto", None), ("engineering_captain", "cto"),
                          ("qa", "engineering_captain"), ("designer", "cto")])
        _rid, wid = _build(c, project)
        assert org_chain.can_take_over(c, project, "agent:engineering_captain", wid)
        assert org_chain.can_take_over(c, project, "agent:cto", wid)
        assert not org_chain.can_take_over(c, project, "agent:designer", wid), "a peer is not a manager"
        assert not org_chain.can_take_over(c, project, "agent:qa", wid), "nobody takes over themselves"
        # P1-5's responsible person always may: the final arm.
        got = org_chain.authority_for(c, project, "person:usr_param", wid)
        assert got["via"] == "responsible_person"
        assert not org_chain.can_take_over(c, project, "person:usr_stranger", wid)
        c.execute(text("UPDATE roles SET lifecycle = 'retired' WHERE project_id = :p AND "
                       "role_key = 'engineering_captain'"), {"p": project})
        assert not org_chain.can_take_over(c, project, "agent:engineering_captain", wid)


# ── 2. escalation walks the chain ────────────────────────────────────────────────────────────

def test_a_stall_goes_to_the_manager_agent_first_then_up_then_to_the_person(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", "cto")
        _org(c, project, [("cto", None), ("engineering_captain", "cto"),
                          ("qa", "engineering_captain")])
        rid, _wid = _build(c, project)
        held = _stall(c, project, rid)
        assert held["phase"] == org_chain.PHASE_AWAITING_MANAGER
        assert held["offered_to"] == "agent:engineering_captain"
        assert held["decision_id"] is None
        assert _cards(c, project) == 0, "no person is interrupted while the chain holds it"
        run = _run(c, rid)
        assert (run["status"], run["phase"]) == ("waiting_on_person", "awaiting_manager")
        # The Shepherd's sweep raises no second card while the chain holds it.
        assert stuck_work._raise_card(c, rid, {"run": {"project_id": project, "id": rid,
                                                         "status": "waiting_on_person"},
                                                "person_wait": {}},
                                      {"cause_code": "quiet"}, "next", "headline") is None
        assert _cards(c, project) == 0

    silent = lambda esc: {}  # noqa: E731 — the manager does not answer
    with eng.begin() as c:
        out = org_chain.poll_due(c, None, now=_later(1), manager_turn=silent)
        assert out["waiting"] == 1 and _offer(c, rid)["offered_to"] == "agent:engineering_captain"
    with eng.begin() as c:
        out = org_chain.poll_due(c, None, now=_later(16), manager_turn=silent)
        assert out["moved"] == 1
        assert _offer(c, rid)["offered_to"] == "agent:cto", "the deadline moves it one rung up"
        assert _cards(c, project) == 0
    with eng.begin() as c:
        out = org_chain.poll_due(c, None, now=_later(40), manager_turn=silent)
        assert out["fell_through"] == 1
        offer = _offer(c, rid)
        assert offer["status"] == "fell_through"
        run = _run(c, rid)
        # Today's person path, unchanged: the card, the thread, the person wait.
        assert (run["status"], run["phase"]) == ("waiting_on_person", "awaiting_grant")
        assert run["decision_id"] and _cards(c, project) == 1
        events = c.execute(text("SELECT event_type FROM events WHERE entity_id = :r ORDER BY "
                                "created_at"), {"r": rid}).scalars().all()
        assert events.index("org.takeover_offered") < events.index("org.takeover_escalated") \
            < events.index("org.takeover_fell_through") < events.index("code_task.waiting_on_person")


def test_a_pass_moves_it_up_and_a_chain_has_one_chance_per_run(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain")
        _org(c, project, [("engineering_captain", None), ("qa", "engineering_captain")])
        rid, _ = _build(c, project)
        _stall(c, project, rid)
    with eng.begin() as c:
        out = org_chain.poll_due(c, None, now=_later(1),
                                 manager_turn=lambda esc: {"action": "pass"})
        # The captain is the last agent: its pass falls straight through to the person.
        assert out["fell_through"] == 1 and _cards(c, project) == 1
        # The same run stalling again goes to the person: the chain had its chance.
        assert org_chain.escalate_first(c, None, project_id=project, origin="person_wait",
                                        run_id=rid, reason=REACH_OUT, question=QUESTION,
                                        fallback={}) is None


def test_an_authority_blocker_still_goes_straight_to_the_person(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain")
        _org(c, project, [("engineering_captain", None), ("qa", "engineering_captain")])
        rid, _ = _build(c, project)
        got = person_wait.enter_waiting_on_person(
            c, _budget(), run_id=rid, project_id=project, task_id=None, reason="no_grant",
            question="No one's authority covers this build.")
        assert got["decision_id"] and _offer(c, rid) is None, "a manager agent cannot give a grant"


# ── 3. takeover ──────────────────────────────────────────────────────────────────────────────

def test_the_manager_takes_over_reassigns_and_records_the_authority_path(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", "cto")
        _org(c, project, [("cto", None), ("engineering_captain", "cto"),
                          ("qa", "engineering_captain")])
        rid, wid = _build(c, project)
        _stall(c, project, rid)
    with eng.begin() as c:
        out = org_chain.poll_due(c, None, now=_later(1), manager_turn=lambda esc: {
            "action": "take_over", "note": "Use API v2; the v1 contract is retired."})
        assert out["taken"] == 1
        run = _run(c, rid)
        assert (run["status"], run["phase"]) == ("running", "queued"), "it resumes under the manager"
        jobs = c.execute(text("SELECT count(*) FROM jobs WHERE kind = 'code.task' AND "
                              "payload->>'run_id' = :r AND status = 'queued'"), {"r": rid}).scalar()
        assert jobs == 1
        ctx = run["finalize_ctx"]
        assert ctx["assignee"] == "agent:engineering_captain"
        # THE LINEAGE RECORD: who acted, under whose chain.
        path = ctx["authority_path"][-1]
        assert path["taken_by"] == "agent:engineering_captain"
        assert path["acted_by"] == "agent:engineering_captain"
        assert path["via"] == "chain" and path["owner"] == "agent:qa"
        assert path["path"] == ["agent:qa", "agent:engineering_captain"]
        assert any("API v2" in n for n in ctx["steer_notes"])
        assert work_items.get_item(c, project, wid)["assignee"] == "agent:engineering_captain"
        offer = _offer(c, rid)
        assert offer["status"] == "taken" and offer["taken_by"] == "agent:engineering_captain"
        assert offer["authority_path"]["path"] == path["path"]
        assert _cards(c, project) == 0, "nobody was interrupted"


def test_a_person_up_the_chain_takes_over_and_an_outsider_is_refused(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain")
        _org(c, project, [("engineering_captain", "person:usr_lead"), ("qa", "engineering_captain")])
        rid, wid = _build(c, project)
        with pytest.raises(org_chain.TakeoverRefused, match="cannot take this work over"):
            org_chain.take_over(c, project_id=project, taker="agent:designer", actor="ceo",
                                work_item_id=wid)
        got = org_chain.take_over(c, project_id=project, taker="person:usr_lead",
                                  actor="person:usr_lead", work_item_id=wid)
        assert got["authority"]["path"] == ["agent:qa", "agent:engineering_captain",
                                            "person:usr_lead"]
        assert got["runs"] == [rid]
        assert work_items.get_item(c, project, wid)["assignee"] == "person:usr_lead"
        assert _run(c, rid)["finalize_ctx"]["authority_path"][-1]["taken_by"] == "person:usr_lead"


def test_a_stuck_live_build_is_offered_to_the_manager_before_a_card(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain")
        _org(c, project, [("engineering_captain", None), ("qa", "engineering_captain")])
        rid, _ = _build(c, project)
        ev = {"run": {"id": rid, "project_id": project, "status": "running",
                      "title": "Fix the orders contract"}, "person_wait": {}}
        tri = {"cause_code": "capped", "cause": "It does not move after 3 fixes", "why_not": "x"}
        assert stuck_work._raise_card(c, rid, ev, tri, "Retry it or cancel it.", "stuck") is None
        assert _offer(c, rid)["origin"] == "stuck_work" and _cards(c, project) == 0
        # A platform cause still goes straight to a person.
        rid2, _ = _build(c, project, title="Another")
        card = stuck_work._raise_card(c, rid2, {**ev, "run": {**ev["run"], "id": rid2}},
                                      {"cause_code": "no_worker", "cause": "No worker"}, "n", "h")
        assert card and _offer(c, rid2) is None
    with eng.begin() as c:
        org_chain.poll_due(c, None, now=_later(20), manager_turn=lambda esc: {})
        assert _offer(c, rid)["status"] == "fell_through"
        cards = c.execute(text("SELECT recommendation FROM decisions WHERE project_id = :p AND "
                               "recommendation->>'cause_code' = 'capped'"),
                          {"p": project}).scalars().all()
        assert len(cards) == 1 and rid in cards[0]["run_ids"]


# ── 4. retired managers block delegation ─────────────────────────────────────────────────────

def _start(c, project, *, finalize_ctx=None):
    return code_task_service.start_code_task(
        c, project_id=project, task_id=None, title=f"Add a report {uuid4().hex[:6]}",
        instructions="Add the weekly report.", acceptance_criteria=[], test_commands=[],
        owner="acme", repo="orders", base_branch=None, finalize_ctx=finalize_ctx,
        jobs=SimpleNamespace(), simulated=True, role_key="qa")


def test_an_agent_under_a_retired_manager_cannot_start_new_work_until_reassigned(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", "cto", retired=("engineering_captain",))
        _org(c, project, [("cto", None), ("engineering_captain", "cto"),
                          ("qa", "engineering_captain")])
        with pytest.raises(org_chain.RetiredChainError) as refused:
            _start(c, project)
        words = str(refused.value)
        assert "'qa' cannot start new work" in words
        assert "'engineering_captain', which is retired" in words
        assert "Reassign 'qa'" in words and "running finishes" in words
        assert isinstance(refused.value, ValueError), "every start door answers it as a 4xx"
        # The running work finishing (a retry of a parent run) is not refused.
        assert _start(c, project, finalize_ctx={"parent_run_id": "crun_parent"})
        # Reassigned to an active manager: it starts.
        org_assignments.set_one(c, project, "qa", reports_to="cto", actor="ceo")
        assert _start(c, project)


# ── the default is unchanged ─────────────────────────────────────────────────────────────────

def _shape(result):
    drop = {"decision_id", "thread_id", "opened_at"}
    return {k: v for k, v in result.items() if k not in drop}


def test_with_no_reporting_lines_the_person_path_is_byte_identical(eng, project):
    with eng.begin() as c:
        _roles(c, project, "qa")
        a, _ = _build(c, project, title="Same build")
        b, _ = _build(c, project, title="Same build")
        with_chain = _stall(c, project, a)
        without = _stall(c, project, b, chain=False)
        assert _shape(with_chain) == _shape(without)
        ra, rb = _run(c, a), _run(c, b)
        assert (ra["status"], ra["phase"]) == (rb["status"], rb["phase"]) == (
            "waiting_on_person", "awaiting_grant")
        pa = {k: v for k, v in ra["finalize_ctx"]["person_wait"].items()
              if k not in ("opened_at",)}
        pb = {k: v for k, v in rb["finalize_ctx"]["person_wait"].items()
              if k not in ("opened_at",)}
        assert pa == pb and set(pa) == set(pb)
        ev_a = c.execute(text("SELECT event_type, data FROM events WHERE entity_id = :r ORDER BY "
                              "created_at"), {"r": a}).all()
        ev_b = c.execute(text("SELECT event_type, data FROM events WHERE entity_id = :r ORDER BY "
                              "created_at"), {"r": b}).all()
        assert [(e[0], sorted(e[1])) for e in ev_a] == [(e[0], sorted(e[1])) for e in ev_b]
        assert not [e for e in ev_a if e[0].startswith("org.")]
        assert c.execute(text("SELECT count(*) FROM org_chain_escalations WHERE project_id = :p"),
                         {"p": project}).scalar() == 0
        # The Shepherd's card: same, nothing offered.
        ev = {"run": {"id": a, "project_id": project, "status": "running", "title": "T"},
              "person_wait": {}}
        assert stuck_work._raise_card(c, "crun_none_" + uuid4().hex[:6], ev,
                                      {"cause_code": "capped", "cause": "c"}, "n", "h")
        assert org_chain.poll_due(c, None, now=_later(60)) == {
            "taken": 0, "moved": 0, "fell_through": 0, "superseded": 0, "waiting": 0}


def test_a_step_named_for_an_agent_under_a_retired_manager_is_refused_in_words(eng, project):
    from boss_engine.services import step_agents

    with eng.begin() as c:
        _roles(c, project, "qa", "engineering_captain", retired=("engineering_captain",))
        _org(c, project, [("engineering_captain", None), ("qa", "engineering_captain")])
        step = {"id": "check", "kind": "capability", "captain": "qa",
                "binding": {"custom_step": "check"}}
        with pytest.raises(step_agents.StepAgentError,
                           match="'engineering_captain', which is retired"):
            step_agents.resolve(c, None, project, step, actor="ceo")
        org_assignments.set_one(c, project, "qa", reports_to=None, actor="ceo")
        assert step_agents.resolve(c, None, project, step, actor="ceo")["agent"] == "qa"
