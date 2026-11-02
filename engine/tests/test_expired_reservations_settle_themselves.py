"""An expired budget hold settles itself from BOSS's own evidence; only a genuinely unknown
outcome reaches a person, and then as one card per cause.

2026-11-01, northpeak-lending: 68 'Reconcile expired budget reservation res_…' tier-2 cards,
one per knowledge.embed hold. The governed embedder wrote its outcome on a closed connection
(fixed in 51cd3c2ba), so every hold stayed active past expiry and each became its own card.
The class fix, enforced here:
  1. the governed adapter links its receipt to its reservation and writes the outcome even when
     the caller's connection is gone; a failure raised before the network is marked as such;
  2. the expiry pass releases a hold whose linked receipt failed, settles one whose receipt
     carries the provider's usage, and resolves any card already open for it (system actor);
  3. holds still unknown are grouped: one card per project+category+reason, and older
     one-per-reservation cards fold into it.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import model_routing
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.core import create_blocking_decision
from boss_engine.services.github_adapter import IntegrationConfigurationError
from boss_engine.services.receipts import ReceiptService

from .test_financial_admission_real_postgres import database, project  # noqa: F401

REASON = "knowledge.embed: one model turn (embed)"
ACTION = "verify_provider_outcome_then_settle_or_release"


def _hold(db, project, *, receipt_status=None, result=None, error=None, link=True):
    reservation = BudgetGovernor(Settings()).reserve(db, project, "knowledge", 2000, REASON, 15)
    rid = reservation["reservation_id"]
    receipt_id = None
    if receipt_status is not None:
        receipt, _ = ReceiptService().create_or_reuse(
            db, project, project, project, f"knowledge.embed:embed:1:{uuid.uuid4().hex[:12]}",
            "knowledge.embed", {"ref": "embed", **({"budget_reservation_id": rid} if link else {})},
            {"turn": 1}, data_boundary="internal")
        receipt_id = receipt["id"]
        if receipt_status == "failed":
            ReceiptService().mark_failed(db, receipt_id, error or {"message": "boom"})
        elif receipt_status == "succeeded":
            ReceiptService().mark_succeeded(db, receipt_id, "text-embedding-3-small", result)
    db.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 second' WHERE id=:id"),
               {"id": rid})
    return rid, receipt_id


def _pending(db, project):
    return db.execute(text(
        "SELECT id, recommendation FROM decisions WHERE project_id=:p AND status='pending' "
        "AND recommendation->>'action'=:a"), {"p": project, "a": ACTION}).mappings().all()


def _status(db, rid):
    return db.execute(text("SELECT status FROM budget_reservations WHERE id=:id"), {"id": rid}).scalar_one()


def test_a_hold_whose_call_failed_before_the_provider_is_released_without_a_card(database, project):
    engine, _ = database
    with engine.begin() as db:
        rid, _ = _hold(db, project, receipt_status="failed",
                       error={"message": "no key", "code": "provider_not_dispatched"})
    with engine.begin() as db:
        BudgetGovernor(Settings()).status_for_project(db, project)
        assert _status(db, rid) == "released"
        assert _pending(db, project) == []
        assert db.execute(text("SELECT count(*) FROM events WHERE entity_id=:r "
                               "AND event_type='budget.reservation_auto_reconciled'"),
                          {"r": rid}).scalar_one() == 1


def test_a_hold_whose_receipt_carries_usage_settles_at_the_actual(database, project):
    engine, _ = database
    with engine.begin() as db:
        rid, receipt_id = _hold(db, project, receipt_status="succeeded",
                                result={"usage": {"total_tokens": 7}, "actual_usd_micros": 3})
    with engine.begin() as db:
        BudgetGovernor(Settings()).status_for_project(db, project)
        assert _status(db, rid) == "consumed"
        assert db.execute(text("SELECT actual_usd_micros FROM cost_receipts WHERE reservation_id=:r "
                               "AND receipt_id=:rc"), {"r": rid, "rc": receipt_id}).scalar_one() == 3
        assert _pending(db, project) == []


def test_an_open_card_for_a_hold_that_now_qualifies_is_resolved_by_the_system(database, project):
    engine, _ = database
    with engine.begin() as db:
        rid, _ = _hold(db, project, receipt_status="failed", error={"message": "401"})
        # The card the old pass raised (the flood on the laptop install).
        old = create_blocking_decision(db, project, None, f"Reconcile expired budget reservation {rid}",
                                       {"action": ACTION, "reservation_id": rid},
                                       {"reserved_usd_micros": 2000}, "budget_outcome_unknown")
        db.execute(text("INSERT INTO events(id,project_id,event_type,actor,entity_type,entity_id,data) "
                        "VALUES(:i,:p,'budget.reservation_reconciliation_required','budget_governor',"
                        "'budget_reservation',:r,'{}')"),
                   {"i": "evt_" + uuid.uuid4().hex[:12], "p": project, "r": rid})
    with engine.begin() as db:
        BudgetGovernor(Settings()).status_for_project(db, project)
        row = db.execute(text("SELECT status, resolution_note, executed_by FROM decisions WHERE id=:d"),
                         {"d": old}).mappings().one()
        assert row["status"] == "resolved" and row["executed_by"] == "budget_governor"
        assert "receipt" in row["resolution_note"]
        assert _status(db, rid) == "released"


def test_unknown_outcomes_of_one_cause_are_one_card_and_old_cards_fold_into_it(database, project):
    engine, _ = database
    with engine.begin() as db:
        # Two holds already flooded as one card each, three more new: a call started (receipt
        # in progress, or no linked evidence at all) and no result was recorded.
        old_holds = [_hold(db, project, receipt_status="in_progress", link=False)[0] for _ in range(2)]
        old_cards = []
        for rid in old_holds:
            old_cards.append(create_blocking_decision(
                db, project, None, f"Reconcile expired budget reservation {rid}",
                {"action": ACTION, "reservation_id": rid}, {"reserved_usd_micros": 2000},
                "budget_outcome_unknown"))
            db.execute(text("INSERT INTO events(id,project_id,event_type,actor,entity_type,entity_id,data) "
                            "VALUES(:i,:p,'budget.reservation_reconciliation_required','budget_governor',"
                            "'budget_reservation',:r,'{}')"),
                       {"i": "evt_" + uuid.uuid4().hex[:12], "p": project, "r": rid})
        new_holds = [_hold(db, project, receipt_status="in_progress")[0] for _ in range(2)]
        new_holds.append(_hold(db, project)[0])
    for _ in range(2):  # a second tick changes nothing
        with engine.begin() as db:
            BudgetGovernor(Settings()).status_for_project(db, project)
    with engine.begin() as db:
        pending = _pending(db, project)
        assert len(pending) == 1, json.dumps([dict(p) for p in pending], default=str)
        rec = pending[0]["recommendation"]
        rec = rec if isinstance(rec, dict) else json.loads(rec)
        assert rec["grouped"] is True and sorted(rec["reservation_ids"]) == sorted(old_holds + new_holds)
        for card in old_cards:
            row = db.execute(text("SELECT status, resolution_note, executed_by FROM decisions WHERE id=:d"),
                             {"d": card}).mappings().one()
            assert row["status"] == "resolved" and pending[0]["id"] in row["resolution_note"]
            assert row["executed_by"] == "budget_governor"
        # Paid work is held on the group card, not on a per-reservation one.
        refused = BudgetGovernor(Settings()).reserve(db, project, "model", 100000, "fixture", 15)
        assert refused.get("decision_id") in (None, pending[0]["id"])
        # The group card closes itself once its last hold is settled or released.
        governor = BudgetGovernor(Settings())
        for rid in old_holds + new_holds:
            governor.release(db, project, rid, "Fixture: reconciled")
        assert _pending(db, project) == []


def test_the_governed_adapter_links_its_receipt_and_writes_its_outcome_on_a_closed_connection(
        database, project):
    """The 51cd3c2ba shape: the adapter was built on a connection that closed before the call.
    The outcome is still written, so the hold never expires into a card at all."""
    engine, _ = database

    class _NoKey:
        def create_embeddings(self, *_a, **_k):
            raise IntegrationConfigurationError("Azure OpenAI needs its key (stored in the vault).")

    class _Ok:
        def create_embeddings(self, *_a, **_k):
            return [[0.1]], {"model": "m", "prompt_tokens": 2, "completion_tokens": 0}

    caller = engine.connect()
    adapters = [model_routing.GovernedChatAdapter(
        inner, caller, Settings(), project, skill_id="knowledge.embed", ref="embed",
        estimate_usd_micros=2000, choice=model_routing.ModelChoice("native", "test"),
        category="knowledge") for inner in (_NoKey(), _Ok())]
    caller.close()
    try:
        adapters[0].create_embeddings(["x"])
    except IntegrationConfigurationError:
        pass
    adapters[1].create_embeddings(["x"])
    with engine.begin() as db:
        rows = db.execute(text(
            "SELECT r.status AS hold, rc.status AS receipt, rc.error->>'code' AS code "
            "FROM budget_reservations r JOIN receipts rc ON rc.effect_scope->>'budget_reservation_id'=r.id "
            "WHERE r.project_id=:p ORDER BY r.created_at"), {"p": project}).mappings().all()
        assert [(r["hold"], r["receipt"], r["code"]) for r in rows] == [
            ("released", "failed", "provider_not_dispatched"), ("consumed", "succeeded", None)]
