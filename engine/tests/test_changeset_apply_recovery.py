"""Actual PG claims and inert HTTP effects, never live provider qualification.

BOSS_CHANGESET_TEST_DATABASE_URL must name an owned disposable schema-loaded DB.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import changeset_apply as apply
from boss_engine.services.resource_plane import ResourcePlaneService

URL = os.environ.get('BOSS_CHANGESET_TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not URL, reason='requires owned disposable Postgres')


@pytest.fixture(scope='module')
def database():
    engine = create_engine(URL, connect_args={'options': '-c statement_timeout=8000'})
    yield engine
    engine.dispose()


def seed(engine, project=None, *, operations=1):
    p = project or 'providerqual_' + uuid4().hex[:12]
    token = uuid4().hex[:12]
    settings = Settings(_env_file=None).model_copy(update={
        'infra_real_provision_allowed': True, 'digitalocean_token': 'owned-inert-fixture'})
    service = ResourcePlaneService(settings)
    cs, reservation, pa = 'cs_' + token, 'res_' + token, 'pa_' + token
    with engine.begin() as c:
        if project is None:
            c.execute(text('INSERT INTO orgs(id,name) VALUES(:p,:p)'), {'p': p})
            c.execute(text('INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)'), {'p': p})
            c.execute(text('INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)'), {'p': p})
        if not c.execute(text("SELECT 1 FROM secret_refs WHERE project_id=:p AND ref_kind='env' "
                              "AND ref_key='BOSS_DIGITALOCEAN_TOKEN'"), {'p': p}).first():
            service.secret_refs.create_secret_ref(c, project_id=p, ref_kind='env',
                ref_key='BOSS_DIGITALOCEAN_TOKEN', provider='digitalocean', status='active',
                metadata={}, actor='owned-owner')
        c.execute(text("INSERT INTO provider_accounts(id,project_id,provider,account_label,credential_ref,"
            "read_only,created_by) VALUES(:pa,:p,'digitalocean',:pa,'env:BOSS_DIGITALOCEAN_TOKEN',false,'owned-owner')"), {'p': p, 'pa': pa})
        c.execute(text("INSERT INTO budget_reservations(id,project_id,category,amount_usd_micros,expires_at) "
            "VALUES(:r,:p,'infra',80000,now()+interval '1 hour')"), {'p': p, 'r': reservation})
        c.execute(text("INSERT INTO changesets(id,project_id,provider_account_id,title,summary,status,"
            "expected_cost_usd_micros,reservation_id,created_by,approved_by,approved_at) "
            "VALUES(:id,:p,:pa,'Owned fixture','Controlled provider only','approved',80000,:r,'owned-owner','owned-owner',now())"),
            {'id': cs, 'p': p, 'pa': pa, 'r': reservation})
        for index in range(operations):
            c.execute(text("INSERT INTO changeset_operations(id,project_id,changeset_id,provider_account_id,operation_index,"
                "skill_id,action,resource_type,resource_ref,desired_state) VALUES(:id,:p,:cs,:pa,:i,"
                "'digitalocean.droplet.create','create','droplet',:id,CAST(:d AS JSONB))"),
                {'id': f'op_{token}_{index}', 'p': p, 'cs': cs, 'pa': pa, 'i': index,
                 'd': json.dumps({'name': f'owned-{token}-{index}', 'region': 'nyc3', 'size': 's-1vcpu-1gb'})})
    return SimpleNamespace(engine=engine, p=p, cs=cs, reservation=reservation, pa=pa, service=service)


@pytest.fixture
def scope(database):
    return seed(database)


class Provider:
    def __init__(self, scope, monkeypatch):
        self.scope = scope
        self.calls = []
        self.on_contact = lambda: None
        self.fail_at = None
        self.crash = False
        self.observations = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.request), trust_env=False)
        monkeypatch.setattr('boss_engine.services.digitalocean_adapter.httpx.request', self.client.request)

    def request(self, request):
        assert str(request.url) == 'https://api.digitalocean.com/v2/droplets'
        assert request.method == 'POST'
        self.calls.append(json.loads(request.content))
        with self.scope.engine.connect() as c:
            rows = c.execute(text("SELECT rr.status, r.status AS receipt, b.status AS budget, cs.status AS changeset "
                "FROM resource_receipts rr JOIN receipts r ON r.id=rr.receipt_id "
                "JOIN changesets cs ON cs.id=rr.changeset_id JOIN budget_reservations b ON b.id=cs.reservation_id "
                "WHERE rr.changeset_id=:cs"), {'cs': self.scope.cs}).mappings().all()
            assert rows and all(row['budget'] == 'active' and row['changeset'] == 'applying' for row in rows)
            assert any(row['status'] == row['receipt'] == 'in_progress' for row in rows)
            assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending' "
                "AND recommendation->>'action'='reconcile_provider_changeset'"), {'p': self.scope.p}).scalar_one() == 1
            self.observations.append([dict(row) for row in rows])
        self.on_contact()
        if self.crash:
            raise SystemExit('owned process loss after acceptance')
        if self.fail_at == len(self.calls):
            raise httpx.ReadError('owned accepted but response lost; do-not-expose-secret', request=request)
        payload = self.calls[-1]
        return httpx.Response(202, json={'droplet': {'id': 471000 + len(self.calls), 'name': payload['name'],
            'region': {'slug': payload['region']}, 'size_slug': payload['size'], 'status': 'new'}})


def invoke(scope, **kwargs):
    return apply.apply_changeset(scope.engine, scope.service, changeset_id=scope.cs,
        authorize=lambda _c, p: 'owned-owner' if p == scope.p else (_ for _ in ()).throw(ValueError('wrong tenant')),
        **kwargs)


def held(scope):
    with scope.engine.begin() as c:
        c.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 hour' WHERE id=:r"), {'r': scope.reservation})
        scope.service.budget.expire_stale_reservations(c, scope.p)
        assert c.execute(text('SELECT status FROM budget_reservations WHERE id=:r'), {'r': scope.reservation}).scalar_one() == 'active'
        assert c.execute(text('SELECT count(*) FROM cost_receipts WHERE reservation_id=:r'), {'r': scope.reservation}).scalar_one() == 0


def test_committed_claim_and_success_survive_outer_rollback(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    with scope.engine.connect() as outer:
        transaction = outer.begin()
        first = invoke(scope)
        transaction.rollback()
    assert first['status'] == 'applied'
    assert invoke(scope)['resource_receipts'] == first['resource_receipts']
    assert len(provider.calls) == 1
    with scope.engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM cost_receipts WHERE reservation_id=:r'), {'r': scope.reservation}).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending'"), {'p': scope.p}).scalar_one() == 0


def test_concurrent_same_changeset_contacts_once(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    entered, release = Event(), Event()
    provider.on_contact = lambda: (entered.set(), release.wait(5) or (_ for _ in ()).throw(AssertionError('barrier timeout')))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, scope)
        assert entered.wait(5)
        try:
            second = pool.submit(invoke, scope).result(timeout=3)
            assert second['status'] == 'applying' and second['decision_id']
            assert len(provider.calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5)['status'] == 'applied'


def test_accepted_lost_response_holds_across_ttl_and_replay(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    provider.fail_at = 1
    result = invoke(scope)
    assert result['status'] == 'needs_reconciliation' and result['decision_id']
    assert 'do-not-expose-secret' not in json.dumps(result)
    assert result['resource_receipts'][0]['status'] == 'in_progress'
    held(scope)
    assert invoke(scope)['status'] == 'needs_reconciliation'
    assert len(provider.calls) == 1


def test_process_loss_after_acceptance_survives_fresh_service(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    provider.crash = True
    with pytest.raises(SystemExit):
        invoke(scope)
    scope.service = ResourcePlaneService(scope.service.settings)
    result = invoke(scope)
    assert result['status'] == 'applying' and result['decision_id']
    held(scope)
    assert len(provider.calls) == 1


def test_failure_recording_success_never_repeats_provider(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    monkeypatch.setattr(scope.service, 'finish_resource_action', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('owned DB finalization failure')))
    assert invoke(scope)['status'] == 'needs_reconciliation'
    held(scope)
    assert invoke(scope)['status'] == 'needs_reconciliation'
    assert len(provider.calls) == 1


def test_partial_changeset_retains_success_and_unknown_hold(database, monkeypatch):
    scope = seed(database, operations=2)
    provider = Provider(scope, monkeypatch)
    provider.fail_at = 2
    result = invoke(scope)
    assert result['status'] == 'needs_reconciliation'
    assert sorted(x['status'] for x in result['resource_receipts']) == ['in_progress', 'succeeded']
    held(scope)
    invoke(scope)
    assert len(provider.calls) == 2


@pytest.mark.parametrize('kind', ['unapproved', 'expired', 'insufficient', 'read_only', 'foreign_account', 'job_id', 'phased_host'])
def test_no_contact_on_invalid_authority(scope, monkeypatch, kind):
    provider = Provider(scope, monkeypatch)
    with scope.engine.begin() as c:
        if kind == 'unapproved':
            c.execute(text("UPDATE changesets SET status='pending_approval' WHERE id=:cs"), {'cs': scope.cs})
        if kind == 'expired':
            c.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 hour' WHERE id=:r"), {'r': scope.reservation})
        if kind == 'insufficient':
            c.execute(text("UPDATE budget_reservations SET amount_usd_micros=1 WHERE id=:r"), {'r': scope.reservation})
        if kind == 'read_only':
            c.execute(text("UPDATE provider_accounts SET read_only=true WHERE id=:pa"), {'pa': scope.pa})
        if kind == 'foreign_account':
            other = seed(scope.engine)
            c.execute(text('UPDATE changeset_operations SET provider_account_id=:pa WHERE changeset_id=:cs'), {'pa': other.pa, 'cs': scope.cs})
        if kind == 'phased_host':
            c.execute(text("UPDATE changesets SET operations='[{\"skill_id\":\"ssh_host.app.deploy\"}]'::jsonb WHERE id=:cs"), {'cs': scope.cs})
    with pytest.raises(ValueError):
        invoke(scope, **({'created_by_job_id': 'unproven-job'} if kind == 'job_id' else {}))
    assert not provider.calls
    with scope.engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM resource_receipts WHERE changeset_id=:cs'), {'cs': scope.cs}).scalar_one() == 0


def test_connection_only_write_entry_refuses(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    with scope.engine.begin() as c, pytest.raises(ValueError, match='committed'):
        scope.service.apply_changeset(c, changeset_id=scope.cs, actor='owned-owner')
    assert not provider.calls


def test_caught_predispatch_authority_loss_releases_proven_unused_hold(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    checks = []
    def authorize(_c, _p):
        checks.append(1)
        if len(checks) > 1:
            raise ValueError('owned permission revoked before contact')
        return 'owned-owner'
    result = apply.apply_changeset(scope.engine, scope.service, changeset_id=scope.cs, authorize=authorize)
    assert result['status'] == 'deadlettered'
    assert result['resource_receipts'][0]['error']['code'] == 'provider_not_dispatched'
    assert not provider.calls
    with scope.engine.connect() as c:
        assert c.execute(text('SELECT status FROM budget_reservations WHERE id=:r'), {'r': scope.reservation}).scalar_one() == 'released'
        assert c.execute(text("SELECT count(*) FROM events WHERE entity_id=:id AND event_type='changeset.not_dispatched'"), {'id': scope.cs}).scalar_one() == 1


def test_operation_edit_after_claim_cannot_change_dispatched_payload(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    checks = []
    def authorize(c, _p):
        checks.append(1)
        if len(checks) > 1:
            c.execute(text("UPDATE changeset_operations SET desired_state='{}'::jsonb WHERE changeset_id=:id"), {'id': scope.cs})
        return 'owned-owner'
    result = apply.apply_changeset(scope.engine, scope.service, changeset_id=scope.cs, authorize=authorize)
    assert result['status'] == 'deadlettered'
    assert not provider.calls


def test_final_settlement_failure_keeps_success_evidence_and_hold(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    monkeypatch.setattr(scope.service.budget, 'settle', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('owned settlement failure')))
    result = invoke(scope)
    assert result['status'] == 'needs_reconciliation'
    assert result['resource_receipts'][0]['status'] == 'succeeded'
    held(scope)
    assert invoke(scope)['status'] == 'needs_reconciliation'
    assert len(provider.calls) == 1


def test_same_reservation_cannot_fund_two_distinct_admissions(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    other = seed(scope.engine, scope.p)
    with scope.engine.begin() as c:
        c.execute(text('UPDATE changesets SET reservation_id=:r WHERE id=:id'), {'r': scope.reservation, 'id': other.cs})
    entered, release = Event(), Event()
    provider.on_contact = lambda: (entered.set(), release.wait(5) or (_ for _ in ()).throw(AssertionError('barrier timeout')))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, scope)
        assert entered.wait(5)
        second = pool.submit(invoke, other)
        release.set()
        assert first.result(timeout=5)['status'] == 'applied'
        with pytest.raises(ValueError, match='active|already belongs'):
            second.result(timeout=5)
    assert len(provider.calls) == 1


def test_fresh_process_refuses_claim_left_by_process_loss(scope, monkeypatch, tmp_path):
    import subprocess
    import sys
    provider = Provider(scope, monkeypatch)
    provider.crash = True
    with pytest.raises(SystemExit):
        invoke(scope)
    marker = tmp_path / 'unexpected-contact'
    program = '''import json,os,pathlib
from sqlalchemy import create_engine
from boss_engine.config import Settings
from boss_engine.services.changeset_apply import apply_changeset
from boss_engine.services.resource_plane import ResourcePlaneService
from boss_engine.services.digitalocean_adapter import DigitalOceanAdapter
def unexpected(*args,**kwargs):
    pathlib.Path(os.environ['OWNED_CONTACT_MARKER']).write_text('contact')
    raise AssertionError('provider contact forbidden after restart')
DigitalOceanAdapter.create_droplet=unexpected
engine=create_engine(os.environ['OWNED_REPLAY_DATABASE_URL'])
try:
    r=apply_changeset(engine,ResourcePlaneService(Settings(_env_file=None)),
        changeset_id=os.environ['OWNED_CHANGESET_ID'],authorize=lambda c,p:'owned-owner')
    print(json.dumps({'status':r['status'],'decision':bool(r['decision_id'])}))
finally:engine.dispose()
'''
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TZ') if k in os.environ}
    env.update(PYTHONPATH='.:engine', PYTHONDONTWRITEBYTECODE='1', OWNED_REPLAY_DATABASE_URL=URL,
               OWNED_CHANGESET_ID=scope.cs, OWNED_CONTACT_MARKER=str(marker))
    restarted = subprocess.run([sys.executable, '-c', program], env=env, capture_output=True, text=True, timeout=15)
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout) == {'status': 'applying', 'decision': True}
    assert not marker.exists() and len(provider.calls) == 1


def test_process_loss_before_contact_does_not_invent_no_call_proof(scope, monkeypatch):
    provider = Provider(scope, monkeypatch)
    checks = []
    def authorize(_c, _p):
        checks.append(1)
        if len(checks) > 1:
            raise SystemExit('owned abrupt stop before dispatch')
        return 'owned-owner'
    with pytest.raises(SystemExit):
        apply.apply_changeset(scope.engine, scope.service, changeset_id=scope.cs, authorize=authorize)
    assert invoke(scope)['status'] == 'applying'
    held(scope)
    assert not provider.calls


def guarded_surface(scope):
    ownership_id = 'surface_' + uuid4().hex
    with scope.engine.begin() as c:
        c.execute(text("INSERT INTO surface_ownerships(id,project_id,surface_key,environment,owner_ref,"
                       "coordination_route,acquired_by,expires_at) "
                       "VALUES(:id,:p,'owned-service','staging','owned-owner','owned-route','owned-owner',"
                       "now()+interval '1 hour')"), {'id': ownership_id, 'p': scope.p})
        c.execute(text("UPDATE changeset_operations SET desired_state=desired_state || CAST(:guard AS JSONB) "
                       "WHERE changeset_id=:id"), {'id': scope.cs, 'guard': json.dumps({'surface_ownership': {
                           'surface_key': 'owned-service', 'environment': 'staging', 'version': 1}})})
    return ownership_id


@pytest.mark.parametrize('mutation', ['owner', 'version', 'status', 'expiry', 'surface', 'environment', 'project'])
def test_surface_revocation_after_committed_claim_has_no_contact(scope, monkeypatch, mutation):
    ownership_id = guarded_surface(scope)
    other = seed(scope.engine) if mutation == 'project' else None
    provider = Provider(scope, monkeypatch)
    checks = []
    def authorize(_c, _p):
        checks.append(1)
        if len(checks) == 2:
            # Another committed transaction changes authority after admission.
            with scope.engine.begin() as observer:
                assert observer.execute(text('SELECT count(*) FROM resource_receipts WHERE changeset_id=:id'),
                                        {'id': scope.cs}).scalar_one() == 1
                assert observer.execute(text('SELECT count(*) FROM ownership_transitions WHERE surface_ownership_id=:id'),
                                        {'id': ownership_id}).scalar_one() == 1
                assignments = {'owner': "owner_ref='another-owner'", 'version': 'version=2',
                    'status': "status='relinquished'", 'expiry': "expires_at=clock_timestamp()-interval '1 second'",
                    'surface': "surface_key='another-service'", 'environment': "environment='production'",
                    'project': 'project_id=:p'}
                observer.execute(text('UPDATE surface_ownerships SET ' + assignments[mutation] + ' WHERE id=:id'),
                                 {'id': ownership_id, 'p': other.p if other else scope.p})
        return 'owned-owner'
    result = apply.apply_changeset(scope.engine, scope.service, changeset_id=scope.cs, authorize=authorize)
    assert result['status'] == 'deadlettered' and not provider.calls
    assert result['resource_receipts'][0]['error']['code'] == 'provider_not_dispatched'
    with scope.engine.connect() as c:
        assert c.execute(text('SELECT status FROM budget_reservations WHERE id=:r'),
                         {'r': scope.reservation}).scalar_one() == 'released'
        assert c.execute(text("SELECT count(*) FROM events WHERE entity_id=:id AND event_type='changeset.not_dispatched'"),
                         {'id': scope.cs}).scalar_one() == 1


def test_surface_authority_lock_is_retained_through_contact(scope, monkeypatch):
    from sqlalchemy.exc import OperationalError
    ownership_id = guarded_surface(scope)
    provider = Provider(scope, monkeypatch)
    def try_concurrent_change():
        with scope.engine.begin() as observer:
            observer.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(OperationalError) as error:
                observer.execute(text("UPDATE surface_ownerships SET owner_ref='another-owner' WHERE id=:id"),
                                 {'id': ownership_id})
            assert error.value.orig.sqlstate == '55P03'
    provider.on_contact = try_concurrent_change
    assert invoke(scope)['status'] == 'applied'
    assert len(provider.calls) == 1
    with scope.engine.begin() as c:
        # The same mutation succeeds after the result transaction finishes.
        assert c.execute(text('UPDATE surface_ownerships SET version=version+1 WHERE id=:id RETURNING version'),
                         {'id': ownership_id}).scalar_one() == 2


def test_later_surface_revocation_preserves_partial_effect_hold(database, monkeypatch):
    scope = seed(database, operations=2)
    ownership_id = guarded_surface(scope)
    provider = Provider(scope, monkeypatch)
    checks = []
    def authorize(_c, _p):
        checks.append(1)
        if len(checks) == 3:
            with scope.engine.begin() as observer:
                assert observer.execute(text("SELECT count(*) FROM resource_receipts WHERE changeset_id=:id AND status='succeeded'"),
                                        {'id': scope.cs}).scalar_one() == 1
                observer.execute(text('UPDATE surface_ownerships SET version=version+1 WHERE id=:id'),
                                 {'id': ownership_id})
        return 'owned-owner'
    result = apply.apply_changeset(scope.engine, scope.service, changeset_id=scope.cs, authorize=authorize)
    assert result['status'] == 'needs_reconciliation'
    assert sorted(x['status'] for x in result['resource_receipts']) == ['in_progress', 'succeeded']
    held(scope)
    assert invoke(scope)['status'] == 'needs_reconciliation'
    assert len(provider.calls) == 1
