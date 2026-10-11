"""Real-PG provider-history quarantine; controlled adapters only, no live effects."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import subprocess
import sys
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import changeset_apply as apply
from boss_engine.services.budget import BudgetGovernor, budget_period
from boss_engine.services.model_gateway import ModelCapacityDeniedError
from engine.tests.test_changeset_apply_recovery import invoke, seed
from engine.tests.test_legacy_financial_exposure import new_authority, new_project, reserve, start

URL = os.environ.get("BOSS_LEGACY_FINANCIAL_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires owned disposable Postgres")


@pytest.fixture(autouse=True)
def no_uncontrolled_provider_contact(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Only the explicit inert provider transport may be entered")

    monkeypatch.setattr("boss_engine.services.digitalocean_adapter.httpx.request", refuse)


@pytest.fixture(scope="module")
def database():
    engine = create_engine(URL, connect_args={"options": "-c statement_timeout=8000"})
    yield engine
    engine.dispose()


def discarded(scope, status="released"):
    """Historical failed/released shape, not a current valid release transition."""
    error = json.dumps({"error_type": "ReadError", "message": "owned transport interrupted"})
    with scope.engine.begin() as c:
        c.execute(
            text(
                "UPDATE resource_receipts SET status='failed',error=CAST(:e AS JSONB) WHERE changeset_id=:cs"
            ),
            {"e": error, "cs": scope.cs},
        )
        c.execute(
            text(
                "UPDATE receipts SET status='failed',error=CAST(:e AS JSONB) WHERE id IN (SELECT receipt_id FROM resource_receipts WHERE changeset_id=:cs)"
            ),
            {"e": error, "cs": scope.cs},
        )
        c.execute(
            text("UPDATE changeset_operations SET status='failed' WHERE changeset_id=:cs"),
            {"cs": scope.cs},
        )
        c.execute(
            text("UPDATE changesets SET status='deadlettered' WHERE id=:cs"), {"cs": scope.cs}
        )
        c.execute(
            text(
                "UPDATE budget_reservations SET status=:status,expires_at=now()-interval '1 hour' WHERE id=:r"
            ),
            {"r": scope.reservation, "status": status},
        )


def history(engine, project, *, status="released", operations=1):
    scope = seed(engine, project, operations=operations)
    with engine.begin() as c:
        cs = scope.service._get_changeset_any_project(c, scope.cs)
        ops = (
            c.execute(
                text(
                    "SELECT * FROM changeset_operations WHERE changeset_id=:cs ORDER BY operation_index"
                ),
                {"cs": scope.cs},
            )
            .mappings()
            .all()
        )
        for op in ops:
            scope.service.prepare_resource_action(
                c, changeset=cs, operation=dict(op), actor="owned-owner", created_by_job_id=None
            )
    discarded(scope, status)
    return scope


def provider_hold(c, p):
    return BudgetGovernor(Settings(_env_file=None)).status_for_project(c, p)["legacy_provider_hold"]


@pytest.mark.parametrize("accepted", [False, True])
def test_both_transport_histories_are_quarantined(database, monkeypatch, accepted):
    p = new_project(database)
    scope = seed(database, p)
    witness = []

    def contact(request):
        assert (
            request.method == "POST"
            and str(request.url) == "https://api.digitalocean.com/v2/droplets"
        )
        if accepted:
            witness.append("owned accepted effect")
        raise httpx.ReadError("owned transport interrupted", request=request)

    with httpx.Client(transport=httpx.MockTransport(contact), trust_env=False) as client:
        monkeypatch.setattr(
            "boss_engine.services.digitalocean_adapter.httpx.request", client.request
        )
        assert invoke(scope)["status"] == "needs_reconciliation"
    assert len(witness) == int(accepted)
    # The separately retained exact-old-source baseline proves this persisted
    # shape was produced by older code in both cases. No live acceptance inferred.
    discarded(scope)
    with database.begin() as c:
        hold = provider_hold(c, p)
        assert hold["changeset_id"] == scope.cs and hold["original_estimate_usd_micros"] == 80000
        assert reserve(c, p, 1)["refusal"] == "legacy_reconciliation_required"
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE id=:r"), {"r": scope.reservation}
            ).scalar_one()
            == "released"
        )
        assert (
            c.execute(
                text("SELECT status FROM resource_receipts WHERE changeset_id=:cs"),
                {"cs": scope.cs},
            ).scalar_one()
            == "failed"
        )


@pytest.mark.parametrize("status", ["expired", "released"])
@pytest.mark.parametrize("period", ["day", "month"])
def test_discarded_provider_hold_survives_cap_period_and_fresh_process(database, status, period):
    p = new_project(database)
    scope = history(database, p, status=status)
    with database.begin() as c:
        c.execute(
            text(
                "UPDATE budget_policies SET daily_cap_usd_micros=:d,monthly_cap_usd_micros=:m WHERE scope_id=:p"
            ),
            {
                "p": p,
                "d": 1000000 if period == "day" else 0,
                "m": 1000000 if period == "month" else 0,
            },
        )
        c.execute(
            text("UPDATE budget_reservations SET created_at=:old WHERE id=:r"),
            {"r": scope.reservation, "old": budget_period(period)["start"] - timedelta(seconds=1)},
        )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os,sys
from sqlalchemy import create_engine
from boss_engine.services.budget import BudgetGovernor
from boss_engine.config import Settings
engine=create_engine(os.environ['BOSS_LEGACY_FINANCIAL_TEST_DATABASE_URL'])
with engine.begin() as c:
    refused=BudgetGovernor(Settings(_env_file=None)).reserve(c,sys.argv[1],'model',1,'owned restart',15)
    assert refused['refusal']=='legacy_reconciliation_required'
print('provider_hold_refused')
""",
            p,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 0 and child.stdout.strip() == "provider_hold_refused"
    with database.begin() as c:
        snap = BudgetGovernor(Settings(_env_file=None)).status_for_project(
            c, p, now=budget_period(period)["end"] + timedelta(seconds=1)
        )
        assert snap["actual_usd_micros"] == snap["reserved_usd_micros"] == 0
        assert snap["available_usd_micros"] == 1000000 and snap["budget_state"] == "green"
        hold = snap["legacy_provider_hold"]
        assert hold["original_reservation_status"] == status
        for _ in range(3):
            assert reserve(c, p, 1)["decision_id"] == hold["decision_id"]
        c.execute(
            text("UPDATE decisions SET status='resolved' WHERE id=:id"), {"id": hold["decision_id"]}
        )
    with database.begin() as c:
        assert reserve(c, p, 1)["decision_id"] == hold["decision_id"]
        assert (
            c.execute(
                text("SELECT status FROM decisions WHERE id=:id"), {"id": hold["decision_id"]}
            ).scalar_one()
            == "pending"
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 1
        )


def not_dispatched(database, p):
    scope = seed(database, p, operations=2)
    checks = []

    def authorize(_c, _p):
        checks.append(1)
        if len(checks) > 1:
            raise ValueError("owned authority revoked")
        return "owned-owner"

    result = apply.apply_changeset(
        database, scope.service, changeset_id=scope.cs, authorize=authorize
    )
    assert result["status"] == "deadlettered"
    assert all(r["error"]["code"] == "provider_not_dispatched" for r in result["resource_receipts"])
    return scope


@pytest.mark.parametrize(
    "damage", [None, "event", "base_code", "sibling_unknown", "sibling_success", "missing_sibling"]
)
def test_only_whole_durable_no_dispatch_protocol_clears(database, damage):
    p = new_project(database)
    scope = not_dispatched(database, p)
    with database.begin() as c:
        ids = (
            c.execute(
                text(
                    "SELECT id,receipt_id FROM resource_receipts WHERE changeset_id=:cs ORDER BY id"
                ),
                {"cs": scope.cs},
            )
            .mappings()
            .all()
        )
        if damage == "event":
            c.execute(
                text(
                    "DELETE FROM events WHERE entity_id=:cs AND event_type='changeset.not_dispatched'"
                ),
                {"cs": scope.cs},
            )
        elif damage == "base_code":
            c.execute(
                text("UPDATE receipts SET error='{}'::jsonb WHERE id=:id"),
                {"id": ids[0]["receipt_id"]},
            )
        elif damage in ("sibling_unknown", "sibling_success"):
            c.execute(
                text("UPDATE resource_receipts SET status=:status,error='{}'::jsonb WHERE id=:id"),
                {
                    "id": ids[1]["id"],
                    "status": "succeeded" if damage == "sibling_success" else "in_progress",
                },
            )
        elif damage == "missing_sibling":
            c.execute(text("DELETE FROM resource_receipts WHERE id=:id"), {"id": ids[1]["id"]})
        assert (provider_hold(c, p) is None) == (damage is None)
        assert reserve(c, p, 1)["status"] == ("reserved" if damage is None else "denied")


@pytest.mark.parametrize(
    "damage", [None, "wrong_reservation", "wrong_receipt", "wrong_project", "wrong_kind", "partial"]
)
def test_only_exact_whole_changeset_settlement_clears(database, damage):
    p = new_project(database)
    scope = history(database, p, operations=2)
    other = history(database, new_project(database))
    with database.begin() as c:
        receipts = (
            c.execute(
                text(
                    "SELECT id,receipt_id FROM resource_receipts WHERE changeset_id=:cs ORDER BY id"
                ),
                {"cs": scope.cs},
            )
            .mappings()
            .all()
        )
        c.execute(text("UPDATE changesets SET status='applied' WHERE id=:cs"), {"cs": scope.cs})
        c.execute(
            text("UPDATE changeset_operations SET status='succeeded' WHERE changeset_id=:cs"),
            {"cs": scope.cs},
        )
        c.execute(
            text("UPDATE resource_receipts SET status='succeeded' WHERE changeset_id=:cs"),
            {"cs": scope.cs},
        )
        c.execute(
            text(
                "UPDATE receipts SET status='succeeded' WHERE id IN (SELECT receipt_id FROM resource_receipts WHERE changeset_id=:cs)"
            ),
            {"cs": scope.cs},
        )
        other_receipt = c.execute(
            text("SELECT receipt_id FROM resource_receipts WHERE changeset_id=:cs"),
            {"cs": other.cs},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO cost_receipts(id,project_id,reservation_id,receipt_id,category,skill_or_model_id,estimated_usd_micros,actual_usd_micros,status) VALUES(:id,:p,:r,:receipt,'provider',:kind,80000,7000,'actual')"
            ),
            {
                "id": "cost_" + uuid4().hex,
                "p": other.p if damage == "wrong_project" else p,
                "r": other.reservation if damage == "wrong_reservation" else scope.reservation,
                "receipt": other_receipt
                if damage == "wrong_receipt"
                else receipts[0]["receipt_id"],
                "kind": "unrelated" if damage == "wrong_kind" else "resource_plane.changeset.apply",
            },
        )
        if damage == "partial":
            c.execute(
                text("UPDATE resource_receipts SET status='failed' WHERE id=:id"),
                {"id": receipts[1]["id"]},
            )
        # Exactly one cost receipt for two operations is the real settlement protocol.
        assert (provider_hold(c, p) is None) == (damage is None)


@pytest.mark.parametrize(
    "table", ["changesets", "changeset_operations", "resource_receipts", "receipts"]
)
def test_foreign_linkage_cannot_poison_other_project(database, table):
    p, other = new_project(database), new_project(database)
    scope = history(database, p)
    with database.begin() as c:
        clause = "id=:cs" if table == "changesets" else "changeset_id=:cs"
        if table == "receipts":
            clause = "id IN (SELECT receipt_id FROM resource_receipts WHERE changeset_id=:cs)"
        c.execute(
            text(f"UPDATE {table} SET project_id=:other WHERE {clause}"),
            {"other": other, "cs": scope.cs},
        )
        assert provider_hold(c, p) is None and provider_hold(c, other) is None


@pytest.mark.parametrize("case", ["zero", "read", "unknown_status"])
def test_only_positive_write_history_quarantines(database, case):
    p = new_project(database)
    scope = history(database, p)
    with database.begin() as c:
        if case == "zero":
            c.execute(
                text("UPDATE budget_reservations SET amount_usd_micros=0 WHERE id=:r"),
                {"r": scope.reservation},
            )
        elif case == "read":
            for table in ("changeset_operations", "resource_receipts"):
                c.execute(
                    text(f"UPDATE {table} SET action='read' WHERE changeset_id=:cs"),
                    {"cs": scope.cs},
                )
        else:
            c.execute(
                text(
                    "UPDATE resource_receipts SET status='unknown_old_state' WHERE changeset_id=:cs"
                ),
                {"cs": scope.cs},
            )
        assert (provider_hold(c, p) is None) == (case != "unknown_status")


@pytest.mark.parametrize(
    "included,estimate,amount",
    [(False, 1000, 1000), (False, 0, 0), (True, 1, 0), (True, 0, 1), (True, 0, 0)],
)
def test_model_paid_start_and_reuse_obey_provider_hold(database, included, estimate, amount):
    p = new_project(database)
    gateway, ident = new_authority(
        database, p, included=included, estimate=estimate, reservation_amount=amount
    )
    history(database, p)
    with database.begin() as c:
        args = dict(
            job_id=ident,
            worker_id="owned-worker",
            job_lease_token="owned-job-token",
            agent_id="owned-agent",
            skill_id="owned-fixture",
            preferred_capabilities=[],
            risk_tier=0,
            consumer_owner_id=None,
            estimated_cost_usd_micros=estimate,
            max_usage={},
            dedupe_key=ident,
            ttl_seconds=60,
        )
        if included and estimate == amount == 0:
            assert gateway.acquire(c, p, **args)["duplicate"]
            assert start(c, p, gateway, ident)["invocation"]["status"] == "in_progress"
        else:
            with pytest.raises(ModelCapacityDeniedError, match="historical unresolved provider"):
                gateway.acquire(c, p, **args)
            with pytest.raises(ModelCapacityDeniedError, match="historical unresolved provider"):
                start(c, p, gateway, ident)
        assert (gateway.budget.assert_active_reservation(c, p, ident) is None) == (amount > 0)


def test_concurrent_provider_scans_keep_one_decision_and_refuse_busy(database):
    p = new_project(database)
    history(database, p)
    ready, release = Event(), Event()

    def first():
        with database.begin() as c:
            result = reserve(c, p, 1)
            ready.set()
            assert release.wait(5)
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert ready.wait(5)
        try:
            with database.begin() as c:
                assert reserve(c, p, 1)["refusal"] == "admission_busy"
        finally:
            release.set()
        first_result = future.result(timeout=5)
    with database.begin() as c:
        assert reserve(c, p, 1)["decision_id"] == first_result["decision_id"]
        assert (
            c.execute(
                text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 1
        )
