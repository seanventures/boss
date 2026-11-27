"""A runbook approval is CLAIMED, and the claim committed, before its remediation runs
(BOSSAPP-1293 review). On a real Postgres:

* two runs racing for the same approval on two connections: exactly one claims it, exactly one
  effect runs, the other run is held for a new approval;
* the claim survives the caller's transaction rolling back after the effect, so a later run never
  replays it (a lost effect is safer than a replay).
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from boss_engine.services.sre_runbooks import SreRunbookService

from ._ops2_support import fresh_engine, make_project

STEP = {"id": "redeploy", "action": "redeploy_deployment", "params": {"run_id": "crun_x"}}


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("srclaim")
    yield engine
    drop()


class _Effect:
    def __init__(self):
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, connection, project_id, params, *, actor, approval=None):
        time.sleep(0.4)                      # widen the window a second claimer would use
        with self.lock:
            self.calls.append((approval or {}).get("run_id") or "")
        return {"ok": True}


def _approved_world(eng, effect):
    project = make_project(eng)
    svc = SreRunbookService(effects={"redeploy_deployment": effect})
    with eng.begin() as c:
        book = svc.create_runbook(c, project, name="Redeploy the gateway", steps=[STEP],
                                  actor="ceo")
        held = svc.execute_run(c, project, book["id"], "inc_1", actor="agent:sre")
    assert held["status"] == "held" and effect.calls == []
    with eng.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ops-lead' "
                       "WHERE id = :d"), {"d": held["decision_id"]})
    return project, book, held["decision_id"]


def test_two_concurrent_runs_claim_one_approval_once(eng):
    effect = _Effect()
    project, book, decision_id = _approved_world(eng, effect)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def run():
        try:
            svc = SreRunbookService(effects={"redeploy_deployment": effect})
            barrier.wait()
            with eng.begin() as c:
                results.append(svc.execute_run(c, project, book["id"], "inc_1",
                                               actor="agent:sre"))
        except BaseException as error:  # noqa: BLE001 — reported below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert sorted(r["status"] for r in results) == ["completed", "held"]
    assert len(effect.calls) == 1
    winner = next(r for r in results if r["status"] == "completed")
    with eng.connect() as c:
        claims = c.execute(text("SELECT run_id FROM sre_runbook_approvals_used "
                                "WHERE decision_id = :d"), {"d": decision_id}).scalars().all()
    assert claims == [winner["id"]] == effect.calls


def test_the_claim_outlives_a_rolled_back_caller_and_never_replays(eng):
    effect = _Effect()
    project, book, decision_id = _approved_world(eng, effect)
    svc = SreRunbookService(effects={"redeploy_deployment": effect})
    with eng.connect() as c:
        tx = c.begin()
        done = svc.execute_run(c, project, book["id"], "inc_1", actor="agent:sre")
        assert done["status"] == "completed" and len(effect.calls) == 1
        tx.rollback()                        # the caller's transaction is lost after the effect
    with eng.connect() as c:
        kept = c.execute(text("SELECT count(*) FROM sre_runbook_approvals_used "
                              "WHERE decision_id = :d"), {"d": decision_id}).scalar()
    assert kept == 1                          # the claim was committed before the effect
    with eng.begin() as c:
        again = SreRunbookService(effects={"redeploy_deployment": effect}).execute_run(
            c, project, book["id"], "inc_1", actor="agent:sre")
    assert again["status"] == "held" and len(effect.calls) == 1
