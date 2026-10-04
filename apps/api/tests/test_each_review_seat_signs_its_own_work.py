"""Each seat of a review panel signs its own work (factory spec §6.4, §8.1).

The panel's seats shared ONE act, so three reviewers read as one face, their tool calls
interleaved, and a finding could not say which of them wrote it. A seat is now its own act,
keyed on (stage, seat): its model calls and turns carry its act id, and the comments that
stand for the review are signed by the seat whose findings they are.
"""
from __future__ import annotations

import json
import threading
import uuid

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _run(engine, **state):
    run_id = f"crun_{uuid.uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state) "
            "VALUES (:i, :p, 'running', 'review_panel', 'Seat test', '{}'::jsonb, "
            "CAST(:s AS JSONB))"), {"i": run_id, "p": PROJECT, "s": json.dumps(state)})
    return run_id


def test_a_panel_of_three_writes_three_seat_acts(engine):
    from boss_engine.services import agent_acts

    run_id = _run(engine)
    with engine.begin() as c:
        seats = [agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id,
                                      stage="review_code", seat=n) for n in (1, 2, 3)]
        again = agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id,
                                     stage="review_code", seat=2)
        rows = c.execute(text(
            "SELECT act_id, seat, label, role_key FROM agent_acts WHERE code_task_run_id = :r "
            "AND kind = 'stage' ORDER BY seat"), {"r": run_id}).mappings().all()
    assert len({s["act_id"] for s in seats}) == 3
    assert again["act_id"] == seats[1]["act_id"]
    assert [r["seat"] for r in rows] == [1, 2, 3]
    assert rows[1]["label"].startswith("Code review · seat 2 · ")
    assert {r["role_key"] for r in rows} == {"adversarial_reviewer"}


def test_each_seats_calls_carry_its_own_act(engine):
    from boss_engine.services import llm_telemetry

    run_id = _run(engine)

    def seat(n):
        llm_telemetry.set_llm_context(db_engine=engine, project_id=PROJECT, run_id=run_id,
                                      stage="review_code", seat=n)
        llm_telemetry.record_llm_call(engine_name="echo", usage={"model": "m"},
                                      stage="review_code")
        llm_telemetry.clear_llm_context()

    threads = [threading.Thread(target=seat, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT l.act_id, a.seat FROM llm_calls l JOIN agent_acts a ON a.act_id = l.act_id "
            "WHERE l.code_task_run_id = :r ORDER BY a.seat"), {"r": run_id}).mappings().all()
    assert [r["seat"] for r in rows] == [1, 2]
    assert rows[0]["act_id"] != rows[1]["act_id"]


def test_a_seat_never_leaks_into_another_stage(engine):
    """A pooled thread that sat on the panel and then ran the security check signs the
    security check, not a review seat."""
    from boss_engine.services import llm_telemetry

    run_id = _run(engine)
    llm_telemetry.set_llm_context(db_engine=engine, project_id=PROJECT, run_id=run_id,
                                  stage="review_code", seat=2)
    llm_telemetry.set_llm_context(stage="security_check")
    llm_telemetry.record_llm_call(engine_name="echo", usage={"model": "m"},
                                  stage="security_check")
    llm_telemetry.clear_llm_context()
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT a.step_id, a.seat, a.role_key FROM llm_calls l JOIN agent_acts a "
            "ON a.act_id = l.act_id WHERE l.code_task_run_id = :r"), {"r": run_id}).mappings().one()
    assert (row["step_id"], row["seat"], row["role_key"]) == (
        "security_check", None, "security_reviewer")


def test_comments_are_signed_by_the_seat_that_wrote_them(engine):
    from boss_engine.services import agent_acts
    from boss_engine.services.governed_code_task_service import _record_review_comments

    run_id = _run(engine)
    with engine.begin() as c:
        one = agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id,
                                   stage="review_code", seat=1)
        two = agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id,
                                   stage="review_code", seat=2)
        sec = agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id,
                                   stage="security_check")
    evidence = {
        "review_seat": 2,
        "review_summary": "FINDING: BLOCKER src/app.py:12 SQL built from user input.\nVERDICT: REFUTED",
        "security_summary": "FINDING: MAJOR src/auth.py:4 token logged in plain text.",
    }
    assert _record_review_comments(engine, run_id, PROJECT, evidence) == 2
    with engine.begin() as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :r"),
                         {"r": run_id}).scalar()
    by_stage = {x["stage"]: x for x in fctx["review_comments"]}
    assert by_stage["review_code"]["act_id"] == two["act_id"] != one["act_id"]
    assert by_stage["review_code"]["author_role_key"] == "adversarial_reviewer"
    assert by_stage["security_check"]["act_id"] == sec["act_id"]
    assert by_stage["security_check"]["author_role_key"] == "security_reviewer"
