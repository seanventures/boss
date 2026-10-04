"""Signing work never queues one tick behind another (Agent World §5.1, §13).

A tick is one transaction, and it runs agent and model steps that take minutes. Every ledger
lock it takes is held until it commits. If the ledger WAITED for a run or a panel another tick
held, a second tick — the worker's next pass, the console's `advance` — sat blocked for the
whole first tick, and two ticks meeting the same runs in a different order could deadlock.
The ledger now only tries: a run another transaction holds is that holder's to sign, and the
next sync catches up. Measured with a lock timeout: waiting at all is a failure.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf


def test_a_tick_skips_a_run_another_tick_is_signing(client, engine, monkeypatch, caplog):
    from boss_engine.context import EngineContext
    from boss_engine.services import agent_acts, meeting_service, workflow_capability_runner

    wf.install_model(monkeypatch)
    skill = wf.skills(client, 1)[0]
    worker = wf.new_agent(client, wf.key("waiter"), holds=(skill,))
    made = wf.published(client, {"entry_step": "draft", "input_schema": wf.START_FORM,
                                 "steps": [wf.agent_step("draft", captain=worker,
                                                         skills=[skill])]})
    run_id = wf.started(client, made["definition"]["workflow_key"], {"instructions": "Draft."})
    with engine.begin() as c:
        meeting_id = meeting_service.start_meeting_run(
            c, project_id=wf.PROJECT, task_id=None, directive="Ship it?", url="",
            constraints={}, role_keys=["qa"], finalize_ctx={"origin": {"actor": "ceo"}},
            title="Ship it")
        c.execute(text("UPDATE meeting_runs SET status = 'completed' WHERE id = :m"),
                  {"m": meeting_id})

    # The first tick: inside its long transaction, holding this run and this panel.
    with engine.connect() as first:
        holding = first.begin()
        agent_acts.sync_run(first, run_id)
        agent_acts.sync_meeting(first, meeting_id)

        # A second tick, which may not wait at all.
        with engine.connect() as second, second.begin():
            second.execute(text("SET LOCAL lock_timeout = '500ms'"))
            agent_acts.reconcile(second, wf.PROJECT)  # raises LockNotAvailable if it waits
            agent_acts.sync_run(second, run_id, wait=False)
            meeting_service._sign_seats(second, meeting_id)
            with caplog.at_level(logging.WARNING, logger="boss.agent_acts"):
                workflow_capability_runner.tick(second, EngineContext.create(),
                                                project_id=wf.PROJECT)
            assert "LockNotAvailable" not in caplog.text, caplog.text
        holding.rollback()

    # Nothing was lost by skipping: the next tick signs what the first one left.
    wf.tick(client)
    with engine.begin() as c:
        kinds = sorted(c.execute(text(
            "SELECT kind FROM agent_acts WHERE workflow_run_id = :r"), {"r": run_id}).scalars())
        seats_open = c.execute(text(
            "SELECT count(*) FROM agent_acts WHERE meeting_run_id = :m AND ended_at IS NULL"),
            {"m": meeting_id}).scalar()
    assert kinds == ["run", "step"], kinds
    assert seats_open == 0
