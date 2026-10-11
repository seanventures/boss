"""A run's chain names who really did each step, and what each step spent over every attempt.

Operator report (wfr_47b4e27e9a3c, 2026-10-11): a panel, a PRD gate and a nine-hour build all
read "A person" on the run card, the build's seven attempts stacked as seven identical rows, and
the build cost "$0.00" while each attempt's code task recorded about $30. Every built-in step is
signed `run_actor` with no role, its model calls land on its stages and seats, and the calls were
answered by a subscription. `resolve_actors` and `step_spend` read what BOSS recorded instead.
"""
from __future__ import annotations

from boss_engine.services import agent_acts

PERSON = "usr_operator"


def act(act_id, kind, *, parent=None, role=None, step=None, **extra):
    row = {"act_id": act_id, "kind": kind, "parent_act_id": parent, "role_key": role,
           "step_id": step, "handed_by_kind": "boss", "handed_by_id": None,
           "accountable": PERSON, "started_at": f"2026-10-11T04:{len(act_id):02d}:00Z",
           "code_task_run_id": None, "meeting_run_id": None, "decision_id": None}
    row.update(extra)
    return row


def operator_run():
    rows = [
        act("root", "run", handed_by_kind="person", handed_by_id=PERSON),
        act("meeting", "step", parent="root", step="meeting", meeting_run_id="mrun_1",
            handed_by_kind="person", handed_by_id=PERSON, how="run_actor"),
        act("seat_pm", "seat", parent="meeting", role="pm", meeting_run_id="mrun_1"),
        act("seat_eng", "seat", parent="meeting", role="software_engineer", meeting_run_id="mrun_1"),
        act("prd", "step", parent="meeting", step="spec_review", decision_id="dec_1"),
        act("prd_ok", "approval", parent="prd", step="spec_review", decision_id="dec_1",
            handed_by_kind="person", handed_by_id=PERSON),
    ]
    for n in range(3):
        rows.append(act(f"build_{n}", "step", parent="prd", step="build", code_task_run_id=f"crun_{n}",
                        started_at=f"2026-10-11T0{5 + n}:00:00Z"))
        if n == 1:
            continue  # this attempt failed before any agent touched it
        rows += [
            act(f"build_{n}_plan", "stage", parent=f"build_{n}", role="chief_architect", step="plan",
                started_at=f"2026-10-11T0{5 + n}:01:00Z"),
            act(f"build_{n}_impl", "stage", parent=f"build_{n}", role="software_engineer", step="implement",
                started_at=f"2026-10-11T0{5 + n}:02:00Z"),
            act(f"build_{n}_review", "stage", parent=f"build_{n}", role="adversarial_reviewer",
                step="review_code", started_at=f"2026-10-11T0{5 + n}:03:00Z"),
        ]
    # The next step hangs under the build's act: never counted as the build's work.
    rows.append(act("review", "step", parent="build_2", step="review", role="adversarial_reviewer"))
    return rows


FACTS = {
    "approvers": {"dec_1": PERSON},
    "panels": {"mrun_1": ["pm", "software_engineer"]},
    "people": {PERSON: {"kind": "user", "name": "Param G"}},
}


def test_each_built_in_step_is_signed_by_who_did_it():
    actors = agent_acts.resolve_actors(operator_run(), **FACTS)
    # The build is its builder (the latest engineering stage), never the starter.
    assert actors["build_0"] == {"kind": "agent", "role_key": "software_engineer", "person_id": None, "name": None}
    assert actors["build_2"]["role_key"] == "software_engineer"
    # An attempt no agent reached is BOSS's own work.
    assert actors["build_1"]["kind"] == "boss"
    # The panel is its chair, with the whole panel.
    assert actors["meeting"]["role_key"] == "pm"
    assert actors["meeting"]["panel"] == ["pm", "software_engineer"]
    # The gate and its approval are the approver, by display name.
    assert actors["prd"] == {"kind": "person", "role_key": None, "person_id": PERSON, "name": "Param G"}
    assert actors["prd_ok"]["name"] == "Param G"
    # The run is the person who started it; an agent's own act is that agent.
    assert actors["root"]["name"] == "Param G"
    assert actors["build_0_review"]["role_key"] == "adversarial_reviewer"


def test_bosss_machinery_is_boss_and_an_unnamed_human_is_only_a_person():
    rows = [act("g1", "step", step="gate", decision_id="dec_auto"),
            act("g2", "step", step="gate2", decision_id="dec_h"),
            act("g3", "step", step="gate3", decision_id="dec_unknown")]
    actors = agent_acts.resolve_actors(
        rows, approvers={"dec_auto": "auto_approval", "dec_h": "human", "dec_unknown": "usr_gone"},
        panels={}, people={})
    assert actors["g1"]["kind"] == "boss"
    assert actors["g2"] == {"kind": "person", "role_key": None, "person_id": None, "name": None}
    # A person the identity store does not know keeps their id, with no invented name.
    assert actors["g3"] == {"kind": "person", "role_key": None, "person_id": "usr_gone", "name": None}
    # A system principal is BOSS.
    sys_actor = agent_acts.resolve_actors(
        [act("r", "run", handed_by_kind="person", handed_by_id="sys_1")], approvers={}, panels={},
        people={"sys_1": {"kind": "system", "name": "Scheduler"}})
    assert sys_actor["r"]["kind"] == "boss"


def test_a_step_spends_what_every_attempt_spent():
    rows = operator_run()
    wire = {r["act_id"]: {"cost_usd": 0.0, "included_usd": 0.0} for r in rows}
    for n in (0, 2):
        wire[f"build_{n}_plan"]["included_usd"] = 4.0
        wire[f"build_{n}_impl"]["included_usd"] = 21.0
        wire[f"build_{n}_review"]["cost_usd"] = 0.5
    wire["review"]["cost_usd"] = 99.0  # the next step: not the build's
    # The panel's calls never reach llm_calls: its own usage record is the spend.
    recorded = {"meeting": (4.68, 0.0), "build_1": (0.0, 7.0)}
    spend = agent_acts.step_spend(rows, wire, recorded)
    build = spend["build"]
    assert build["attempts"] == 3
    assert build["act_ids"] == ["build_0", "build_1", "build_2"]
    assert build["cost_usd"] == 1.0
    assert build["included_usd"] == 57.0  # 25 + 7 (the record of the attempt with no acts) + 25
    assert build["recorded"] == [0.0, 7.0]
    assert spend["meeting"]["cost_usd"] == 4.68
    assert spend["review"]["cost_usd"] == 99.0


def test_a_record_never_counts_twice():
    rows = [act("s", "step", step="build", code_task_run_id="crun_1"),
            act("s_impl", "stage", parent="s", role="software_engineer", step="implement")]
    wire = {"s": {"cost_usd": 0, "included_usd": 0}, "s_impl": {"cost_usd": 0, "included_usd": 30.0}}
    spend = agent_acts.step_spend(rows, wire, {"s": (0.0, 30.0)})
    assert spend["build"]["included_usd"] == 30.0
    assert spend["build"]["recorded"] == [0.0, 0.0]
