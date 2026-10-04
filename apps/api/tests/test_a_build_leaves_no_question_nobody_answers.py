"""A build leaves no question nobody will answer (factory walk D2).

Two kinds were found on a walked install, both still pending after their build opened its PR:
a direct change's "PRD approval (automatic)" — a gate nobody and no automation answers — and a
retry's second "Merge PR #1?" beside the first attempt's. New builds no longer make them; the
boot repair (run_decisions.withdraw_stale_build_questions) withdraws the ones already there,
with the existing `withdrawn` status, and is idempotent.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.tests import _factory_support as fx
from apps.api.tests import workflow_management_support as wf


def _pr_decision(engine) -> str:
    decision_id = f"dec_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, status) "
                       "VALUES (:i, :p, 'Approve code change (PR #7)', CAST(:r AS JSONB), "
                       "'pending')"),
                  {"i": decision_id, "p": fx.PROJECT, "r": '{"action": "review_pull_request"}'})
    return decision_id


def _status(engine, decision_id: str) -> tuple[str, str]:
    with engine.begin() as c:
        row = c.execute(text("SELECT status, resolution_note FROM decisions WHERE id = :i"),
                        {"i": decision_id}).first()
    return str(row[0]), str(row[1] or "")


def test_a_retry_withdraws_the_earlier_attempts_merge_question(client, engine):
    from boss_engine.services.agent_acts import settle_code_task

    first_dec, second_dec = _pr_decision(engine), _pr_decision(engine)
    first = fx.new_run(engine, status="completed", phase="done", pr=7, decision_id=first_dec,
                       title="Dup app")
    second = fx.new_run(engine, status="completed", phase="done", pr=7, decision_id=second_dec,
                        title="Dup app · retry 1",
                        fctx={"root_run_id": first, "parent_run_id": first, "attempt": 2})
    settle_code_task(engine, second)
    assert _status(engine, first_dec) == ("withdrawn", "Superseded by attempt 2")
    assert _status(engine, second_dec)[0] == "pending"


def test_the_boot_repair_withdraws_what_nothing_will_answer_once(client, engine):
    from boss_engine.services.run_decisions import withdraw_stale_build_questions

    # 1. A direct change's PRD gate, waiting, on a build that already opened its PR.
    made = wf.published(client, {"entry_step": "spec_review", "steps": [
        wf.gate_step("spec_review", question=f"PRD approval (automatic) {uuid4().hex[:6]}")]})
    run_id = wf.started(client, made["definition"]["workflow_key"], {})
    wf.tick(client)
    step = wf.steps_of(wf.run_view(client, run_id))["spec_review"]
    assert step["status"] == "waiting" and step["decision_id"]
    build = fx.new_run(engine, status="completed", phase="done", pr=11, title="Direct app")
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_runs SET input = COALESCE(input, '{}'::jsonb) || "
                       "CAST(:i AS JSONB) WHERE id = :r"),
                  {"i": json.dumps({"adopt_code_task_run_id": build}), "r": run_id})
    # 2. Two attempts' merge questions for one PR, from before the settle-time rule.
    old_dec, new_dec = _pr_decision(engine), _pr_decision(engine)
    first = fx.new_run(engine, status="completed", phase="done", pr=12, decision_id=old_dec,
                       title="Old app", minutes_ago=30)
    fx.new_run(engine, status="completed", phase="done", pr=12, decision_id=new_dec,
               title="Old app · retry 1", minutes_ago=5,
               fctx={"root_run_id": first, "parent_run_id": first, "attempt": 2})

    with engine.begin() as c:
        withdrawn = set(withdraw_stale_build_questions(c, fx.PROJECT))
    assert {step["decision_id"], old_dec} <= withdrawn and new_dec not in withdrawn
    assert _status(engine, step["decision_id"])[0] == "withdrawn"
    assert _status(engine, old_dec) == ("withdrawn", "Superseded by attempt 2")
    after = wf.steps_of(wf.run_view(client, run_id))["spec_review"]
    assert after["status"] == "succeeded"
    with engine.begin() as c:
        assert withdraw_stale_build_questions(c, fx.PROJECT) == []
