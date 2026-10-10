"""BOSS-470: owned real Postgres; no model/provider calls or real credentials."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import subprocess
from threading import Barrier, Event, current_thread
import time
import uuid

import pytest
from sqlalchemy import create_engine, event, text

from boss_engine.config import Settings
from boss_engine.services.budget import BudgetGovernor, budget_period
from boss_engine.services.model_gateway import ModelGatewayService

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get('BOSS_RUN_FINANCIAL_TEST') != '1',
                                reason='requires an owned disposable Docker Postgres')


def docker(*args, input=None):
    return subprocess.run(['docker', *args], input=input, text=True, capture_output=True,
                          check=True, timeout=90).stdout.strip()


@pytest.fixture(scope='module')
def database():
    docker('image', 'inspect', 'postgres:16-alpine')  # no network pull
    name = 'bossfinance' + uuid.uuid4().hex[:10]
    volume = name + '-data'
    docker('volume', 'create', '--label', 'boss.qualification=' + name, volume)
    engine = None
    resource = None
    try:
        docker('run', '-d', '--name', name, '--label', 'boss.qualification=' + name,
               '--pull', 'never', '-e', 'POSTGRES_PASSWORD=fixture', '-e', 'POSTGRES_USER=boss',
               '-e', 'POSTGRES_DB=boss', '-p', '127.0.0.1::5432',
               '-v', volume + ':/var/lib/postgresql/data', 'postgres:16-alpine')
        for _ in range(100):
            check = subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1',
                                    '-U', 'boss', '-d', 'boss'], capture_output=True)
            if check.returncode == 0:
                break
            Event().wait(0.1)  # service startup only; admission schedules use barriers
        else:
            pytest.fail('owned Postgres did not become ready')
        docker('exec', '-i', name, 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'boss', '-d', 'boss',
               input=(REPO / 'db/schema.sql').read_text())
        port = docker('port', name, '5432').rsplit(':', 1)[1]
        engine = create_engine(f'postgresql+psycopg://boss:fixture@127.0.0.1:{port}/boss',
                               connect_args={'options': '-c statement_timeout=10000'})
        resource = [engine, name]
        yield resource
    finally:
        if resource is not None:
            resource[0].dispose()
        elif engine is not None:
            engine.dispose()
        docker('rm', '-f', name)
        docker('volume', 'rm', volume)


@pytest.fixture()
def project(database):
    engine, _ = database
    project = 'finance_' + uuid.uuid4().hex[:12]
    with engine.begin() as db:
        db.execute(text('INSERT INTO orgs(id,name) VALUES(:p,:p)'), {'p': project})
        db.execute(text('INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)'), {'p': project})
        db.execute(text('INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)'), {'p': project})
        db.execute(text('INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,'
                        'monthly_cap_usd_micros,burst_cap_usd_micros_per_min) '
                        "VALUES(:p,'project',:p,100000,100000,0)"), {'p': project})
    return project


def reserve(db, project, *, amount=40000, job_id=None):
    return BudgetGovernor(Settings()).reserve(db, project, 'model_gateway' if job_id else 'model',
                                              amount, 'local qualification', 15, job_id=job_id)


@pytest.mark.parametrize('callers', [('proof', 'proof'), ('job', 'job'), ('proof', 'job')])
@pytest.mark.parametrize('allocations', ['none', 'same', 'different'])
def test_concurrent_admission_never_exceeds_project_cap(database, project, callers, allocations):
    """First transaction pauses after totals; second must refuse or await serialization.

    The controller releases the first only once the second either returns or is visibly
    blocked by its backend. On the old code, the second commits against the same snapshot,
    then the first commits too. No timing sleep chooses the winner.
    """
    engine, _ = database
    with engine.begin() as db:
        db.execute(text("INSERT INTO cost_receipts(id,project_id,category,skill_or_model_id,"
                        "estimated_usd_micros,actual_usd_micros,status) VALUES(:p,:p,'fixture','fixture',40000,40000,'actual')"), {'p': project})
        for index, kind in enumerate(callers):
            if kind == 'job':
                db.execute(text("INSERT INTO jobs(id,project_id,kind) VALUES(:j,:p,'fixture')"),
                           {'j': project + str(index), 'p': project})
        if allocations != 'none':
            for index in range(2 if allocations == 'different' else 1):
                db.execute(text("INSERT INTO budget_allocations(id,project_id,scope_type,scope_id,amount_usd_micros,period_start,period_end) "
                                "VALUES(:a,:p,'fixture',:a,100000,now(),now()+interval '1 day')"),
                           {'a': project + 'alloc' + str(index), 'p': project})
    snapshot_read, finish_first, second_entered = Event(), Event(), Event()
    backend = {}

    class PausedGovernor(BudgetGovernor):
        def status_for_project(self, connection, project_id, **kwargs):
            snapshot = super().status_for_project(connection, project_id, **kwargs)
            if not snapshot_read.is_set():
                snapshot_read.set()
                assert finish_first.wait(10), 'controller did not release first admission'
            return snapshot

    def admit(index):
        with engine.begin() as db:
            backend[index] = db.execute(text('SELECT pg_backend_pid()')).scalar_one()
            if index:
                second_entered.set()
            allocation = None if allocations == 'none' else project + 'alloc' + str(index if allocations == 'different' else 0)
            if allocation:
                # Some callers arrive already owning their allocation row. Different rows
                # must still share one project envelope without a reversed-lock deadlock.
                db.execute(text('SELECT id FROM budget_allocations WHERE id=:id FOR UPDATE'), {'id': allocation})
            governor = PausedGovernor(Settings()) if index == 0 else BudgetGovernor(Settings())
            return governor.reserve(db, project, callers[index], 40000, 'concurrent fixture', 15,
                                    job_id=project + str(index) if callers[index] == 'job' else None,
                                    allocation_id=allocation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(admit, 0)
        try:
            assert snapshot_read.wait(10)
            second = executor.submit(admit, 1)
            assert second_entered.wait(10)
            deadline = time.monotonic() + 8
            with engine.connect() as control:
                while not second.done():
                    blockers = control.execute(text('SELECT pg_blocking_pids(:pid)'), {'pid': backend[1]}).scalar_one()
                    if backend[0] in blockers:
                        break
                    assert time.monotonic() < deadline, 'second admission neither returned nor serialized'
        finally:
            finish_first.set()
        results = [first.result(timeout=10), second.result(timeout=10)]
    with engine.begin() as db:
        snapshot = BudgetGovernor(Settings()).status_for_project(db, project)
        admitted = sum(row['status'] == 'reserved' for row in results)
        assert snapshot['committed_usd_micros'] <= 100000, (callers, admitted, snapshot['committed_usd_micros'])
        assert admitted == 1, results
        refusal = next(row for row in results if row['status'] != 'reserved')
        expected = 'headroom' if allocations == 'same' else 'admission_busy'
        assert refusal['refusal'] == expected and refusal['transient'] and refusal['decision_id'] is None
        assert db.execute(text("SELECT count(*) FROM events WHERE project_id=:p AND event_type='budget.refused' AND data->>'kind'=:kind"), {'p': project, 'kind': expected}).scalar_one() == 1
        if allocations == 'none':
            assert db.execute(text("SELECT count(*) FROM budget_reservations WHERE project_id=:p AND allocation_id IS NOT NULL"), {'p': project}).scalar_one() == 0
        assert reserve(db, project, amount=20000)['status'] == 'reserved'
        assert reserve(db, project, amount=1)['refusal'] == 'headroom'
        assert BudgetGovernor(Settings()).status_for_project(db, project)['committed_usd_micros'] == 100000


@pytest.mark.parametrize('boundary', ['ttl', 'day', 'month'])
@pytest.mark.parametrize('envelope', ['day', 'month'])
def test_unresolved_positive_hold_survives_expiry_and_rollover(database, project, boundary, envelope):
    engine, _ = database
    with engine.begin() as db:
        db.execute(text('UPDATE budget_policies SET daily_cap_usd_micros=:daily, monthly_cap_usd_micros=:monthly WHERE scope_id=:p'),
                   {'p': project, 'daily': 100000 if envelope == 'day' else 0,
                    'monthly': 100000 if envelope == 'month' else 0})
        held = reserve(db, project, amount=80000)['reservation_id']
        if boundary == 'ttl':
            db.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 second' WHERE id=:id"), {'id': held})
        else:
            before = budget_period(boundary)['start'] - timedelta(seconds=1)
            db.execute(text('UPDATE budget_reservations SET created_at=:before WHERE id=:id'), {'before': before, 'id': held})
    # New engine/governor represents process restart: accounting must come from durable rows.
    engine.dispose()
    with engine.begin() as db:
        snapshot = BudgetGovernor(Settings()).status_for_project(db, project)
        assert snapshot['reserved_usd_micros'] == 80000, (boundary, snapshot)
        assert snapshot['periods']['day']['reserved_usd_micros'] == 80000
        assert snapshot['periods']['month']['reserved_usd_micros'] == 80000
        assert reserve(db, project, amount=30000)['status'] == 'denied'


def _started_invocation(db, project, reservation, *, accepted=True):
    """Durable accepted/uncertain shape, deliberately no provider call."""
    db.execute(text("INSERT INTO jobs(id,project_id,kind) VALUES(:p,:p,'fixture')"), {'p': project})
    db.execute(text("INSERT INTO entitlement_sources(id,project_id,provider,source_id,source_kind,entitlement_kind,owner_type,owner_id) "
                    "VALUES(:p,:p,'fixture',:p,'api','api','project',:p)"), {'p': project})
    db.execute(text("INSERT INTO routing_logs(id,project_id,task_kind,workload_class,risk_tier,budget_state) "
                    "VALUES(:p,:p,'fixture','general',0,'green')"), {'p': project})
    db.execute(text("INSERT INTO receipts(id,org_id,workspace_id,project_id,dedupe_key,effect_kind,payload_hash) "
                    "VALUES(:p,:p,:p,:p,:p,'model.invoke','fixture')"), {'p': project})
    db.execute(text("INSERT INTO model_gateway_leases(id,project_id,job_id,agent_id,worker_id,route_decision_id,"
                    "entitlement_source_id,entitlement_pool_id,source_id,provider,auth_method,lease_token_hash,"
                    "fencing_token,estimated_cost_usd_micros,budget_reservation_id,dedupe_key,expires_at) "
                    "VALUES(:p,:p,:p,'fixture','fixture',:p,:p,'fixture',:p,'fixture','api_key','fixture',1,80000,:r,:p,now()-interval '1 second')"),
               {'p': project, 'r': reservation})
    db.execute(text("INSERT INTO model_gateway_invocations(id,project_id,model_gateway_lease_id,job_id,agent_id,receipt_id,request_hash,dedupe_key,provider_ref) "
                    "VALUES(:p,:p,:p,:p,'fixture',:p,'fixture',:p,:ref)"),
               {'p': project, 'ref': 'fixture-accepted-reference' if accepted else None})


@pytest.mark.parametrize('accepted', [True, False])
def test_gateway_reaper_does_not_release_an_unresolved_invocation(database, project, accepted):
    engine, _ = database
    with engine.begin() as db:
        reservation = reserve(db, project, amount=80000)['reservation_id']
        _started_invocation(db, project, reservation, accepted=accepted)
    with engine.begin() as db:
        ModelGatewayService(Settings()).reap_expired(db, project, actor='fixture')
    engine.dispose()
    with engine.begin() as db:
        assert BudgetGovernor(Settings()).status_for_project(db, project)['reserved_usd_micros'] == 80000
        refused = reserve(db, project, amount=30000)
        assert refused['refusal'] == 'reconciliation_required' and not refused['transient']
        assert refused['decision_id']
        assert db.execute(text('SELECT status FROM model_gateway_invocations WHERE id=:p'), {'p': project}).scalar_one() == 'in_progress'
        assert db.execute(text("SELECT count(*) FROM events WHERE project_id=:p AND event_type='model_gateway.reconciliation_required'"), {'p': project}).scalar_one() == 1


def test_expired_authority_cannot_restart_but_explicit_no_call_release_frees_hold(database, project):
    engine, _ = database
    governor = BudgetGovernor(Settings())
    with engine.begin() as db:
        reservation = reserve(db, project, amount=80000)['reservation_id']
        db.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 second' WHERE id=:id"), {'id': reservation})
    for _ in range(2):
        with engine.begin() as db:
            assert governor.assert_active_reservation(db, project, reservation) is None
            assert governor.status_for_project(db, project)['reserved_usd_micros'] == 80000
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM events WHERE entity_id=:id AND event_type='budget.reservation_reconciliation_required'"), {'id': reservation}).scalar_one() == 1
        assert governor.release(db, project, reservation, 'Fixture dispatcher cancelled before any call')['status'] == 'released'
        assert governor.release(db, project, reservation, 'Repeated known cancellation')['status'] == 'released'
        assert governor.status_for_project(db, project)['reserved_usd_micros'] == 0
        assert db.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending'"), {'p': project}).scalar_one() == 0
        assert reserve(db, project, amount=100000)['status'] == 'reserved'


@pytest.mark.parametrize('reaped_first', [False, True])
def test_proven_predispatch_abort_releases_even_after_gateway_reap(database, project, reaped_first):
    engine, _ = database
    with engine.begin() as db:
        reservation = reserve(db, project, amount=80000)['reservation_id']
        _started_invocation(db, project, reservation, accepted=False)
    gateway = ModelGatewayService(Settings())
    if reaped_first:
        with engine.begin() as db:
            gateway.reap_expired(db, project, actor='fixture')
    with engine.begin() as db:
        gateway.abandon_expired_attempt(db, project, project, worker_id='fixture',
                                       invocation_id=project, reason='Fixture stopped before dispatch')
        assert BudgetGovernor(Settings()).status_for_project(db, project)['reserved_usd_micros'] == 0
        assert db.execute(text('SELECT status FROM model_gateway_invocations WHERE id=:p'), {'p': project}).scalar_one() == 'failed'
        assert db.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending'"), {'p': project}).scalar_one() == 0


def test_gateway_without_any_invocation_releases_expired_capacity(database, project):
    engine, _ = database
    with engine.begin() as db:
        reservation = reserve(db, project, amount=80000)['reservation_id']
        _started_invocation(db, project, reservation)
        db.execute(text('DELETE FROM model_gateway_invocations WHERE id=:p'), {'p': project})
    with engine.begin() as db:
        ModelGatewayService(Settings()).reap_expired(db, project, actor='fixture')
        assert BudgetGovernor(Settings()).status_for_project(db, project)['reserved_usd_micros'] == 0


def test_late_settlement_is_idempotent_and_replaces_hold_with_actual(database, project):
    engine, _ = database
    governor = BudgetGovernor(Settings())
    with engine.begin() as db:
        reservation = reserve(db, project, amount=80000)['reservation_id']
        _started_invocation(db, project, reservation)
        db.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 second' WHERE id=:id"), {'id': reservation})
        assert governor.assert_active_reservation(db, project, reservation) is None
    start = Barrier(2)

    def settle():
        with engine.begin() as db:
            start.wait(timeout=10)
            return governor.settle(db, project, reservation, project, 'fixture', 'fixture', 80000, 30000)

    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [executor.submit(settle), executor.submit(settle)]
        ids = [call.result(timeout=10) for call in calls]
    assert ids[0] == ids[1]
    with engine.begin() as db:
        snapshot = governor.status_for_project(db, project)
        assert (snapshot['reserved_usd_micros'], snapshot['actual_usd_micros']) == (0, 30000)
        assert db.execute(text('SELECT count(*) FROM cost_receipts WHERE reservation_id=:r'), {'r': reservation}).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending'"), {'p': project}).scalar_one() == 0
        same = governor.consume(db, project, reservation, project, 'fixture', 'fixture', 80000, 30000, 'fixture')
        assert same['cost_receipt']['id'] == ids[0]
        with pytest.raises(ValueError, match='different cost evidence'):
            governor.settle(db, project, reservation, project, 'fixture', 'fixture', 80000, 40000)
        assert reserve(db, project, amount=70000)['status'] == 'reserved'


@pytest.mark.parametrize('initial_actual', [30000, None])
def test_included_settlement_replay_rejects_different_equivalent_cost(database, project, initial_actual):
    engine, _ = database
    governor = BudgetGovernor(Settings())
    equivalent = initial_actual if initial_actual is not None else 80000
    with engine.begin() as db:
        reservation = reserve(db, project, amount=0)['reservation_id']
        db.execute(text("INSERT INTO receipts(id,org_id,workspace_id,project_id,dedupe_key,effect_kind,payload_hash) "
                        "VALUES(:p,:p,:p,:p,:p,'model.invoke','fixture')"), {'p': project})
        first = governor.consume(db, project, reservation, project, 'fixture', 'fixture',
                                 80000, initial_actual, 'fixture', included_in_subscription=True)
        assert first['cost_receipt']['actual_usd_micros'] == 0
        assert first['cost_receipt']['equivalent_usd_micros'] == equivalent
    with engine.begin() as db:
        same = governor.consume(db, project, reservation, project, 'fixture', 'fixture',
                                80000, equivalent, 'fixture', included_in_subscription=True)
        assert same['cost_receipt']['id'] == first['cost_receipt']['id']
        with pytest.raises(ValueError, match='different cost evidence'):
            governor.consume(db, project, reservation, project, 'fixture', 'fixture',
                             80000, equivalent + 10000, 'fixture', included_in_subscription=True)
        if initial_actual is not None:
            with pytest.raises(ValueError, match='different cost evidence'):
                governor.consume(db, project, reservation, project, 'fixture', 'fixture',
                                 80000, None, 'fixture', included_in_subscription=True)
        rows = db.execute(text('SELECT actual_usd_micros, equivalent_usd_micros FROM cost_receipts WHERE reservation_id=:r'),
                          {'r': reservation}).all()
        assert rows == [(0, equivalent)]
        assert governor.status_for_project(db, project)['actual_usd_micros'] == 0


def test_database_restart_preserves_unknown_exposure(database, project):
    engine, name = database
    with engine.begin() as db:
        reservation = reserve(db, project, amount=80000)['reservation_id']
        _started_invocation(db, project, reservation, accepted=False)
        db.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 second', created_at=now()-interval '2 months' WHERE id=:id"), {'id': reservation})
    engine.dispose()
    docker('restart', name)
    for _ in range(100):
        ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1',
                                '-U', 'boss', '-d', 'boss'], capture_output=True)
        if ready.returncode == 0:
            break
        Event().wait(0.1)
    else:
        pytest.fail('owned Postgres did not recover from restart')
    # Docker can assign a new ephemeral host port on restart.
    port = docker('port', name, '5432').rsplit(':', 1)[1]
    restarted = create_engine(f'postgresql+psycopg://boss:fixture@127.0.0.1:{port}/boss')
    database[0] = restarted
    with restarted.begin() as db:
        ModelGatewayService(Settings()).reap_expired(db, project, actor='fixture')
        assert BudgetGovernor(Settings()).status_for_project(db, project)['reserved_usd_micros'] == 80000
        assert reserve(db, project, amount=30000)['status'] == 'denied'


def test_admission_busy_is_retryable_and_distinct_from_budget_overrun():
    from boss_engine.services.budget import ADMISSION_BUSY_MESSAGE
    from boss_engine.services.model_failures import explain
    result = explain(ADMISSION_BUSY_MESSAGE)
    assert result['kind'] == 'budget_wait' and result['retry']
    assert 'admission' in result['headline'] and 'increase' in result['action']


def test_settlement_cannot_open_a_gap_between_actual_and_held_totals(database, project):
    engine, _ = database
    with engine.begin() as db:
        db.execute(text('UPDATE budget_policies SET monthly_cap_usd_micros=0 WHERE scope_id=:p'), {'p': project})
        reservation = reserve(db, project, amount=80000)['reservation_id']
        _started_invocation(db, project, reservation)
    actual_read, continue_admission = Event(), Event()

    def pause_actual_read(conn, cursor, statement, parameters, context, executemany):
        if (current_thread().name.startswith('admission-snapshot')
                and 'FROM cost_receipts ' in statement and not actual_read.is_set()):
            actual_read.set()
            assert continue_admission.wait(10)

    def admit():
        with engine.begin() as db:
            return reserve(db, project, amount=30000)

    event.listen(engine, 'after_cursor_execute', pause_actual_read)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='admission-snapshot') as executor:
            future = executor.submit(admit)
            try:
                assert actual_read.wait(10)
                with engine.begin() as db:
                    BudgetGovernor(Settings()).settle(db, project, reservation, project,
                                                      'fixture', 'fixture', 80000, 80000)
            finally:
                continue_admission.set()
            assert future.result(timeout=10)['status'] == 'denied'
    finally:
        event.remove(engine, 'after_cursor_execute', pause_actual_read)
    with engine.begin() as db:
        assert BudgetGovernor(Settings()).status_for_project(db, project)['committed_usd_micros'] == 80000
