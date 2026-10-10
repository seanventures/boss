"""Real PG legacy accounting fences; no paid provider contact."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import subprocess
import sys
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.budget import BudgetGovernor, budget_period
from boss_engine.services.model_gateway import ModelCapacityDeniedError, ModelGatewayService

URL = os.environ.get("BOSS_LEGACY_FINANCIAL_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires owned disposable Postgres")


@pytest.fixture(scope="module")
def database():
    engine = create_engine(URL, connect_args={"options": "-c statement_timeout=8000"})
    yield engine
    engine.dispose()


def new_project(engine):
    p = "legacyqual_" + uuid4().hex[:12]
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": p})
        c.execute(
            text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"), {"p": p}
        )
        policy(c, p)
    return p


def policy(c, p):
    c.execute(
        text(
            "INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,monthly_cap_usd_micros,burst_cap_usd_micros_per_min) VALUES(:id,'project',:p,100000,100000,0)"
        ),
        {"id": "bp_" + uuid4().hex, "p": p},
    )


def legacy(engine, p, *, status="expired", amount=80000):
    ident = "legacy_" + uuid4().hex[:12]
    with engine.begin() as c:
        scope = (
            c.execute(
                text(
                    "SELECT p.workspace_id,w.org_id FROM projects p JOIN workspaces w ON w.id=p.workspace_id WHERE p.id=:p"
                ),
                {"p": p},
            )
            .mappings()
            .one()
        )
        c.execute(
            text(
                "INSERT INTO budget_reservations(id,project_id,category,amount_usd_micros,status,expires_at) VALUES(:id,:p,'model_gateway',:amount,:status,now()-interval '1 hour')"
            ),
            {"id": ident, "p": p, "amount": amount, "status": status},
        )
        c.execute(
            text("INSERT INTO jobs(id,project_id,kind) VALUES(:id,:p,'owned-fixture')"),
            {"id": ident, "p": p},
        )
        c.execute(
            text(
                "INSERT INTO entitlement_sources(id,project_id,provider,source_id,source_kind,entitlement_kind,owner_type,owner_id) VALUES(:id,:p,'owned-fixture',:id,'api','api_key','project',:p)"
            ),
            {"id": ident, "p": p},
        )
        c.execute(
            text(
                "INSERT INTO routing_logs(id,project_id,task_kind,workload_class,risk_tier,budget_state) VALUES(:id,:p,'owned-fixture','general',0,'green')"
            ),
            {"id": ident, "p": p},
        )
        c.execute(
            text(
                "INSERT INTO receipts(id,org_id,workspace_id,project_id,dedupe_key,effect_kind,payload_hash) VALUES(:id,:o,:w,:p,:id,'model.invoke','owned-fixture')"
            ),
            {"id": ident, "p": p, "o": scope["org_id"], "w": scope["workspace_id"]},
        )
        c.execute(
            text(
                "INSERT INTO model_gateway_leases(id,project_id,job_id,agent_id,worker_id,route_decision_id,entitlement_source_id,entitlement_pool_id,source_id,provider,auth_method,lease_token_hash,fencing_token,estimated_cost_usd_micros,budget_reservation_id,dedupe_key,status,expires_at) VALUES(:id,:p,:id,'owned-agent','owned-worker',:id,:id,'owned-fixture',:id,'owned-fixture','api_key','owned-expired',1,:amount,:id,:id,'expired',now()-interval '1 hour')"
            ),
            {"id": ident, "p": p, "amount": amount},
        )
        c.execute(
            text(
                "INSERT INTO model_gateway_invocations(id,project_id,model_gateway_lease_id,job_id,agent_id,receipt_id,request_hash,dedupe_key) VALUES(:id,:p,:id,:id,'owned-agent',:id,'owned-fixture',:id)"
            ),
            {"id": ident, "p": p},
        )
    return ident


def reserve(c, p, amount=30000):
    return BudgetGovernor(Settings(_env_file=None)).reserve(
        c, p, "model", amount, "owned financial qualification", 15
    )


@pytest.mark.parametrize("status", ["expired", "released"])
@pytest.mark.parametrize("period", ["day", "month"])
def test_legacy_hold_denies_independent_of_cap_period_without_reviving_authority(
    database, status, period
):
    p = new_project(database)
    old = legacy(database, p, status=status)
    g = BudgetGovernor(Settings(_env_file=None))
    with database.begin() as c:
        c.execute(
            text(
                "UPDATE budget_policies SET daily_cap_usd_micros=:d,monthly_cap_usd_micros=:m WHERE scope_id=:p"
            ),
            {
                "p": p,
                "d": 100000 if period == "day" else 0,
                "m": 100000 if period == "month" else 0,
            },
        )
        before = budget_period(period)["start"] - timedelta(seconds=1)
        c.execute(
            text("UPDATE budget_reservations SET created_at=:before WHERE id=:id"),
            {"id": old, "before": before},
        )
    database.dispose()
    with database.begin() as c:
        snap = g.status_for_project(c, p, now=budget_period(period)["end"] + timedelta(seconds=1))
        hold = snap["legacy_financial_hold"]
        assert (
            hold["original_estimate_usd_micros"] == 80000
            and hold["original_reservation_status"] == status
        )
        assert snap["actual_usd_micros"] == snap["reserved_usd_micros"] == 0
        assert snap["budget_state"] == "green" and snap["available_usd_micros"] == 100000
        denied = reserve(c, p, 1)
        assert denied["refusal"] == "legacy_reconciliation_required" and not denied["transient"]
        assert denied["decision_id"] == hold["decision_id"]
        assert g.assert_active_reservation(c, p, old) is None
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE id=:id"), {"id": old}
            ).scalar_one()
            == status
        )
        assert (
            c.execute(
                text("SELECT status FROM model_gateway_leases WHERE id=:id"), {"id": old}
            ).scalar_one()
            == "expired"
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )


def test_rolled_back_refusal_remains_blocked_and_committed_status_records_one_decision(database):
    p = new_project(database)
    legacy(database, p)
    with database.connect() as c:
        transaction = c.begin()
        assert reserve(c, p)["refusal"] == "legacy_reconciliation_required"
        transaction.rollback()
    database.dispose()
    # A new OS process re-derives the refusal from durable evidence; it rolls its
    # refusal transaction back too, so the later committed GET owns the decision.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys
from sqlalchemy import create_engine
from boss_engine.config import Settings
from boss_engine.services.budget import BudgetGovernor
engine = create_engine(os.environ['BOSS_LEGACY_FINANCIAL_TEST_DATABASE_URL'])
with engine.connect() as connection:
    result = BudgetGovernor(Settings(_env_file=None)).reserve(
        connection, sys.argv[1], 'model', 1, 'owned fresh process', 15)
    assert result['refusal'] == 'legacy_reconciliation_required'
    connection.rollback()
engine.dispose()
print('fresh_process_refused')
""",
            p,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 0 and child.stdout.strip() == "fresh_process_refused"
    with database.begin() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )
        hold = BudgetGovernor(Settings(_env_file=None)).status_for_project(c, p)[
            "legacy_financial_hold"
        ]
    with database.begin() as c:
        for _ in range(3):
            assert reserve(c, p)["decision_id"] == hold["decision_id"]
        c.execute(
            text(
                "UPDATE decisions SET status='resolved',resolution_note='owned generic resolve' WHERE id=:id"
            ),
            {"id": hold["decision_id"]},
        )
    with database.begin() as c:
        assert reserve(c, p)["decision_id"] == hold["decision_id"]
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
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:p AND event_type='budget.legacy_reconciliation_required'"
                ),
                {"p": p},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize(
    "control",
    ["zero", "terminal", "settled", "foreign_lease", "foreign_invocation", "foreign_receipt"],
)
def test_nonqualifying_or_foreign_history_does_not_fence_project(database, control):
    p = new_project(database)
    other = new_project(database)
    old = legacy(database, p, amount=0 if control == "zero" else 80000)
    with database.begin() as c:
        if control == "terminal":
            c.execute(
                text("UPDATE model_gateway_invocations SET status='failed' WHERE id=:id"),
                {"id": old},
            )
        if control == "settled":
            c.execute(
                text(
                    "INSERT INTO cost_receipts(id,project_id,reservation_id,receipt_id,category,skill_or_model_id,estimated_usd_micros,actual_usd_micros,status) VALUES(:id,:p,:id,:id,'model','owned-fixture',80000,7000,'actual')"
                ),
                {"id": old, "p": p},
            )
        for kind, table in [
            ("foreign_lease", "model_gateway_leases"),
            ("foreign_invocation", "model_gateway_invocations"),
            ("foreign_receipt", "receipts"),
        ]:
            if control == kind:
                c.execute(
                    text("UPDATE " + table + " SET project_id=:p WHERE id=:id"),
                    {"id": old, "p": other},
                )
        assert reserve(c, p, 1)["status"] == "reserved"
        assert reserve(c, other, 1)["status"] == "reserved"


def test_wrong_settlement_receipt_does_not_clear_legacy_hold(database):
    p = new_project(database)
    old = legacy(database, p)
    with database.begin() as c:
        c.execute(
            text(
                "INSERT INTO cost_receipts(id,project_id,reservation_id,category,skill_or_model_id,estimated_usd_micros,actual_usd_micros,status) VALUES(:id,:p,:id,'model','unlinked-fixture',80000,7000,'actual')"
            ),
            {"id": old, "p": p},
        )
        assert reserve(c, p)["refusal"] == "legacy_reconciliation_required"


def test_concurrent_detection_is_bounded_retryable_and_deduped(database):
    p = new_project(database)
    legacy(database, p)
    ready, release = Event(), Event()

    def first():
        with database.begin() as c:
            result = reserve(c, p)
            ready.set()
            assert release.wait(5)
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert ready.wait(5)
        try:
            with database.begin() as c:
                assert reserve(c, p)["refusal"] == "admission_busy"
        finally:
            release.set()
        decision = future.result(timeout=5)["decision_id"]
    with database.begin() as c:
        assert reserve(c, p)["decision_id"] == decision
        assert (
            c.execute(
                text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 1
        )


def new_authority(engine, p, *, included=False, estimate=1000, reservation_amount=None):
    ident = legacy(engine, p, amount=estimate)
    gateway = ModelGatewayService(Settings(_env_file=None))
    with engine.begin() as c:
        c.execute(text("DELETE FROM model_gateway_invocations WHERE id=:id"), {"id": ident})
        c.execute(
            text(
                "UPDATE budget_reservations SET status='active',expires_at=now()+interval '1 hour',amount_usd_micros=:amount WHERE id=:id"
            ),
            {"id": ident, "amount": estimate if reservation_amount is None else reservation_amount},
        )
        c.execute(
            text(
                "UPDATE model_gateway_leases SET status='active',expires_at=now()+interval '1 hour',lease_token_hash=:hash,entitlement_pool_id=:id WHERE id=:id"
            ),
            {"id": ident, "hash": gateway._hash_token("owned-model-token")},
        )
        c.execute(
            text(
                "UPDATE jobs SET status='running',lease_owner='owned-worker',lease_token_hash=:hash,lease_expires_at=now()+interval '1 hour' WHERE id=:id"
            ),
            {"id": ident, "hash": gateway.jobs._hash_token("owned-job-token")},
        )
        if included:
            c.execute(
                text(
                    "UPDATE entitlement_sources SET entitlement_kind='local_capacity',cost_mode='local_capacity' WHERE id=:id"
                ),
                {"id": ident},
            )
    return gateway, ident


def start(c, p, gateway, ident):
    return gateway.start_invocation(
        c,
        p,
        ident,
        worker_id="owned-worker",
        lease_token="owned-model-token",
        job_lease_token="owned-job-token",
        request_hash="owned-request",
        dedupe_key=ident + "-new",
        metadata={},
    )


@pytest.mark.parametrize(
    "included,estimate,reservation_amount",
    [(False, 1000, 1000), (False, 0, 0), (True, 1, 0), (True, 1, 1), (True, 0, 1)],
)
def test_preexisting_authority_cannot_start_paid_or_uncertain_work(
    database, included, estimate, reservation_amount
):
    p = new_project(database)
    g, ident = new_authority(
        database, p, included=included, estimate=estimate, reservation_amount=reservation_amount
    )
    legacy(database, p)
    with database.begin() as c:
        with pytest.raises(ModelCapacityDeniedError, match="historical unresolved"):
            start(c, p, g, ident)
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM model_gateway_invocations WHERE model_gateway_lease_id=:id"
                ),
                {"id": ident},
            ).scalar_one()
            == 0
        )
        with pytest.raises(ModelCapacityDeniedError, match="historical unresolved"):
            g.acquire(
                c,
                p,
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
        active = BudgetGovernor(Settings(_env_file=None)).assert_active_reservation(c, p, ident)
        assert (active is None) if reservation_amount > 0 else (active is not None)


@pytest.mark.parametrize("kind", ["local_capacity", "subscription"])
def test_exact_zero_included_authority_can_start_but_paid_fallback_cannot(database, kind):
    p = new_project(database)
    g, ident = new_authority(database, p, included=True, estimate=0)
    with database.begin() as c:
        c.execute(
            text(
                "UPDATE entitlement_sources SET entitlement_kind=:kind,cost_mode=:kind WHERE id=:id"
            ),
            {"id": ident, "kind": kind},
        )
    legacy(database, p)
    with database.begin() as c:
        assert reserve(c, p, 0)["status"] == "reserved"
        assert start(c, p, g, ident)["invocation"]["status"] == "in_progress"
        # Ordinary heartbeat remains usable while a different legacy hold exists.
        assert (
            g.heartbeat(
                c,
                p,
                ident,
                worker_id="owned-worker",
                lease_token="owned-model-token",
                job_lease_token="owned-job-token",
                ttl_seconds=60,
            )["status"]
            == "active"
        )
        c.execute(
            text(
                "UPDATE entitlement_sources SET entitlement_kind='api_key',cost_mode='usage_based_api' WHERE id=:id"
            ),
            {"id": ident},
        )
        with pytest.raises(ModelCapacityDeniedError, match="historical unresolved"):
            start(c, p, g, ident)


def test_new_included_local_adapter_still_runs_without_paid_fallback(database):
    from boss_engine.services.entitlements import EntitlementBrokerService
    from engine.tests.test_architecture_governance import Scripted, generate, spender

    p = new_project(database)
    settings = Settings(_env_file=None).model_copy(
        update={"local_model_enabled": True, "local_model_id": "owned-fixture"}
    )
    with database.begin() as c:
        EntitlementBrokerService(settings).seed_default_sources(c, p)
    legacy(database, p)
    scope = (database, p, settings)
    model = Scripted()
    assert generate(spender(scope), settings, model)
    assert model.calls == 1
    with database.connect() as c:
        actual = (
            c.execute(
                text("SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p"), {"p": p}
            )
            .scalars()
            .all()
        )
        assert actual == [0]


def test_already_inflight_paid_work_can_heartbeat_and_settle_without_clearing_old_hold(database):
    p = new_project(database)
    g, ident = new_authority(database, p, estimate=1000)
    with database.begin() as c:
        invocation = start(c, p, g, ident)["invocation"]
    old = legacy(database, p)
    with database.begin() as c:
        assert (
            g.heartbeat(
                c,
                p,
                ident,
                worker_id="owned-worker",
                lease_token="owned-model-token",
                job_lease_token="owned-job-token",
                ttl_seconds=60,
            )["status"]
            == "active"
        )
        result = g.settle_invocation(
            c,
            p,
            ident,
            invocation["id"],
            worker_id="owned-worker",
            lease_token="owned-model-token",
            job_lease_token="owned-job-token",
            succeeded=True,
            provider_ref="owned-inert-receipt",
            usage_units={},
            actual_cost_usd_micros=500,
            observed_capacity={},
            error={},
        )
        assert result["status"] == "succeeded" and result["actual_cost_usd_micros"] == 500
        snapshot = g.budget.status_for_project(c, p)
        assert snapshot["actual_usd_micros"] == 500
        assert snapshot["legacy_financial_hold"]["reservation_id"] == old
        assert reserve(c, p, 1)["refusal"] == "legacy_reconciliation_required"


def test_zero_included_lease_without_actual_reservation_cannot_start(database):
    p = new_project(database)
    g, ident = new_authority(database, p, included=True, estimate=0)
    legacy(database, p)
    with database.begin() as c:
        c.execute(
            text("UPDATE model_gateway_leases SET budget_reservation_id=NULL WHERE id=:id"),
            {"id": ident},
        )
        with pytest.raises(ModelCapacityDeniedError, match="historical unresolved"):
            start(c, p, g, ident)
