"""A long model call, a lost lease, and a reply in hand: settled once, replayed, never paid twice.

Run wfr_47b4e27e9a3c4a1c9eeb: the Chief Architect's reply took longer than the step's two-minute
claim. The reply was kept on its receipt, unsettled; every retry raised a reconciliation card and
the step dead-lettered; approving the card did nothing. These pin the three repairs:

(a) the claim and the call's leases stay alive for the whole call;
(b) a retry holding the current claim settles the kept reply once and replays it;
(c) approving the reconciliation card settles (or fails) the receipt and retries the step.

Same owned disposable Postgres as test_architecture_governance (BOSS_ARCHITECTURE_TEST_DATABASE_URL).
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from boss_engine.services import architecture_docs as arch
from boss_engine.services.decisions import DecisionService
from boss_engine.services.typed_decision_spend import TypedDecisionReconciliationRequired

from .test_architecture_governance import (  # noqa: F401 — fixtures
    PRD, URL, Scripted, database, generate, metered, panel, scope, spender, workflow,
)

pytestmark = pytest.mark.skipif(not URL, reason="requires an owned disposable Postgres")


def one(c, sql, p):
    return c.execute(text(sql), {"p": p}).scalar_one()


def stop_after_call():
    """The workflow claim is gone when the reply comes back (the incident's third check)."""
    checks = []

    def check():
        checks.append(1)
        if len(checks) == 3:
            raise arch.ArchitectureRefused(
                "Architecture has no current workflow lease or person-requested revision.")
    return check


def kept_reply(scope, model, **kw):
    engine, p, settings = scope
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope, **kw), settings, model, check=stop_after_call())
    with engine.connect() as c:
        receipt = c.execute(text("SELECT status, result FROM receipts WHERE project_id=:p "
                                 "AND effect_kind='model.invoke'"), {"p": p}).mappings().one()
        assert receipt["status"] == "in_progress"
        assert "unsettled_architecture_reply" in receipt["result"]
        assert one(c, "SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending' "
                      "AND recommendation->>'action'='architecture_reconciliation'", p) == 1


# ── (a) the leases last as long as the call ──────────────────────────────────────────────

def test_call_longer_than_its_lease_still_settles(scope):
    engine, p, settings = scope
    metered(scope)
    model = Scripted(lambda: time.sleep(8))
    spend = spender(scope)
    spend.ttl_seconds = 5  # the job and model leases would expire mid-call without heartbeats
    assert generate(spend, settings, model)[1].startswith("## Context")
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "succeeded"
        assert one(c, "SELECT count(*) FROM cost_receipts WHERE project_id=:p", p) == 1


def _adopted_run(scope, *, lease_seconds):
    engine, p, _ = scope
    with engine.begin() as c:
        r = workflow(c, p)
        context, _, _ = panel(c, p)
        c.execute(text("UPDATE workflow_runs SET input=CAST(:i AS JSONB) WHERE id=:r"),
                  {"r": r, "i": json.dumps(context["input"])})
        c.execute(text("UPDATE workflow_step_runs SET lease_expires_at=clock_timestamp()"
                       " + make_interval(secs => :s) WHERE workflow_run_id=:r"),
                  {"r": r, "s": lease_seconds})
    return r


def test_step_claim_is_renewed_when_the_reply_outlives_it(scope, monkeypatch):
    engine, p, settings = scope
    r = _adopted_run(scope, lease_seconds=1)
    model = Scripted(lambda: time.sleep(2))
    monkeypatch.setattr(arch, "architect_adapter", lambda *a, **k: model)
    with engine.connect() as c:
        result = arch.draft(c, settings, p, architect="chief_architect", run_id=r,
                            step_id="arch", prd=PRD, title="Owned architecture")
        assert "Context" in result["markdown"]
        assert c.execute(text("SELECT lease_expires_at > clock_timestamp() FROM "
                              "workflow_step_runs WHERE workflow_run_id=:r"), {"r": r}).scalar_one()
        c.commit()
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "succeeded"
    assert model.calls == 1


def test_a_claim_taken_over_mid_call_is_never_renewed(scope, monkeypatch):
    engine, p, settings = scope
    r = _adopted_run(scope, lease_seconds=120)

    def taken():
        with engine.begin() as other:
            other.execute(text("UPDATE workflow_step_runs SET attempt=attempt+1 "
                               "WHERE workflow_run_id=:r"), {"r": r})
    model = Scripted(taken)
    monkeypatch.setattr(arch, "architect_adapter", lambda *a, **k: model)
    with engine.connect() as c, pytest.raises(arch.ArchitectureDraftError):
        arch.draft(c, settings, p, architect="chief_architect", run_id=r,
                   step_id="arch", prd=PRD, title="Owned architecture")
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "in_progress"


# ── (b) a retry settles the kept reply once and replays it ──────────────────────────────

@pytest.mark.parametrize("source", ["included", "metered"])
def test_reply_after_lease_expired_is_settled_once_and_replayed_never_paid_twice(scope, source):
    engine, p, settings = scope
    if source == "metered":
        metered(scope)
    model = Scripted()
    kept_reply(scope, model)
    if source == "included":
        # A $0 hold on a subscription expires on its own (expire_stale_reservations); the call
        # it admitted still happened and is still recorded.
        with engine.begin() as c:
            c.execute(text("UPDATE budget_reservations SET status='expired' WHERE project_id=:p"),
                      {"p": p})
    first = generate(spender(scope), settings, model)
    again = generate(spender(scope), settings, model)
    assert first == again and first[1].startswith("## Context")
    assert model.calls == 1
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "succeeded"
        assert one(c, "SELECT count(*) FROM cost_receipts WHERE project_id=:p", p) == 1
        assert one(c, "SELECT count(*) FROM llm_calls WHERE project_id=:p", p) == 1
        assert one(c, "SELECT status FROM budget_reservations WHERE project_id=:p", p) == "consumed"
        assert one(c, "SELECT status FROM model_gateway_invocations WHERE project_id=:p", p) \
            == "succeeded"
        assert one(c, "SELECT status FROM jobs WHERE project_id=:p", p) == "succeeded"
        assert one(c, "SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending'",
                   p) == 0
        if source == "metered":
            assert one(c, "SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p",
                       p) == 3000


def test_kept_reply_of_a_call_another_worker_took_is_not_settled(scope):
    engine, p, settings = scope
    model = Scripted()
    kept_reply(scope, model)
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET lease_owner='other-worker' WHERE project_id=:p"), {"p": p})
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, model)
    assert model.calls == 1
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "in_progress"
        assert one(c, "SELECT count(*) FROM cost_receipts WHERE project_id=:p", p) == 0


# ── (c) approving the card settles the receipt and retries the step ──────────────────────

def _deadlettered_run(scope):
    engine, p, _ = scope
    with engine.begin() as c:
        r = workflow(c, p)
    return r


def _stop(engine, p, r):
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_step_runs SET status='deadlettered', lease_owner=NULL, "
                       "lease_expires_at=NULL WHERE workflow_run_id=:r"), {"r": r})
        c.execute(text("UPDATE workflow_runs SET status='deadlettered' WHERE id=:r"), {"r": r})


def _approve(engine, p, settings):
    with engine.begin() as c:
        c.execute(text("INSERT INTO project_members (id, project_id, member_id, display_name, "
                       "member_type, role_keys, permissions) VALUES (:i, :p, 'usr_owned_owner', "
                       "'owner', 'human', '[\"ceo\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) "
                       "ON CONFLICT DO NOTHING"), {"i": f"pm_{p}_owner", "p": p})
        row = c.execute(text("SELECT * FROM decisions WHERE project_id=:p AND status='pending' "
                             "AND recommendation->>'action'='architecture_reconciliation'"),
                        {"p": p}).mappings().one()
        DecisionService(settings).transition(c, row["id"], action="approve",
                                             actor="usr_owned_owner", note=None)
        return row["id"]


def test_approving_reconciliation_settles_the_kept_reply_and_retries_the_step(scope):
    engine, p, settings = scope
    r = _deadlettered_run(scope)
    model = Scripted()
    kept_reply(scope, model, run=r)
    _stop(engine, p, r)
    decision_id = _approve(engine, p, settings)
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "succeeded"
        assert one(c, "SELECT count(*) FROM cost_receipts WHERE project_id=:p", p) == 1
        assert c.execute(text("SELECT status FROM decisions WHERE id=:d"),
                         {"d": decision_id}).scalar_one() == "approved"
        assert c.execute(text("SELECT status FROM workflow_step_runs WHERE workflow_run_id=:r "
                              "ORDER BY iteration DESC LIMIT 1"), {"r": r}).scalar_one() \
            not in ("deadlettered", "failed")
    # The step's retry replays the settled reply; nothing is called or paid again.
    assert generate(spender(scope, run=r), settings, model)[1].startswith("## Context")
    assert model.calls == 1


def test_approving_reconciliation_without_a_reply_fails_the_receipt_and_allows_a_fresh_try(scope):
    engine, p, settings = scope
    r = _deadlettered_run(scope)

    def unknown():
        raise TimeoutError("owned unknown acceptance")
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope, run=r), settings, Scripted(unknown))
    _stop(engine, p, r)
    _approve(engine, p, settings)
    with engine.connect() as c:
        assert one(c, "SELECT status FROM receipts WHERE project_id=:p "
                      "AND effect_kind='model.invoke'", p) == "failed"
        assert one(c, "SELECT status FROM budget_reservations WHERE project_id=:p", p) \
            == "released"
    model = Scripted()
    assert generate(spender(scope, run=r), settings, model)[1].startswith("## Context")
    assert model.calls == 1
