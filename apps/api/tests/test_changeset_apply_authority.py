"""Canonical API guards around real PG apply claims; only HTTP provider is inert."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authn, authz
from boss_engine.identity.principal import (
    AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus,
)
from engine.tests.test_changeset_apply_recovery import Provider, seed


@pytest.fixture
def owned(client, engine, monkeypatch):
    world = IdentityWorld.create(engine)
    owner = world.person('provider-owner')
    binding = world.bind(owner, 'owner', project=world.project_a)
    scope = seed(engine, world.project_a)
    provider = Provider(scope, monkeypatch)
    monkeypatch.setattr(main, 'resource_plane', scope.service)
    # Select the existing IdentityWorld test session transport. The real guard
    # still resolves catalogue permissions, scope and current canonical actor.
    client.cookies.set(authn.SESSION_COOKIE, 'owned-test-session-selector')
    yield SimpleNamespace(world=world, owner=owner, binding=binding, scope=scope, provider=provider)
    provider.client.close()


def post(client, owned, **kwargs):
    return client.post(f'/v1/changesets/{owned.scope.cs}/apply',
                       json={'actor': 'not-the-authority', **kwargs},
                       headers={'Sec-Fetch-Site': 'same-origin', 'Idempotency-Key': str(uuid4())})


def test_owner_apply_records_canonical_actor_and_replays_once(client, owned, engine):
    with owned.world.acting_as(owned.world.ctx(owned.owner)):
        first = post(client, owned)
        assert first.status_code == 200, first.text
        assert first.json()['status'] == 'applied'
        again = post(client, owned)
        assert again.status_code == 200 and again.json()['status'] == 'applied', again.text
    assert len(owned.provider.calls) == 1
    with engine.connect() as c:
        assert c.execute(text('SELECT applied_by FROM changesets WHERE id=:id'), {'id': owned.scope.cs}).scalar_one() == owned.owner.id
        assert c.execute(text("SELECT actor FROM events WHERE entity_id=:id AND event_type='changeset.apply_started'"), {'id': owned.scope.cs}).scalar_one() == owned.owner.id


@pytest.mark.parametrize('kind', ['viewer', 'foreign_owner', 'worker', 'revoked'])
def test_current_permission_refuses_before_claim_or_contact(client, owned, engine, kind):
    world = owned.world
    if kind == 'worker':
        principal = Principal('sys_worker', PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE, 'Owned worker')
    elif kind == 'revoked':
        principal = owned.owner
        with engine.begin() as c:
            c.execute(text('DELETE FROM identity_role_bindings WHERE id=:id'), {'id': owned.binding})
            authz.bump_epoch(world.org, db=c, reason='owned revoke')
    else:
        principal = world.person(kind)
        world.bind(principal, 'viewer' if kind == 'viewer' else 'owner',
                   project=world.project_a if kind == 'viewer' else world.project_b)
    ctx = AuthContext(principal, AuthMethod.SYSTEM) if kind == 'worker' else world.ctx(principal)
    with world.acting_as(ctx):
        response = post(client, owned)
    assert response.status_code in (403, 404), response.text
    assert not owned.provider.calls
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM resource_receipts WHERE changeset_id=:id'), {'id': owned.scope.cs}).scalar_one() == 0


def test_current_permission_checked_again_before_completed_replay(client, owned, engine):
    with owned.world.acting_as(owned.world.ctx(owned.owner)):
        assert post(client, owned).status_code == 200
        with engine.begin() as c:
            c.execute(text('DELETE FROM identity_role_bindings WHERE id=:id'), {'id': owned.binding})
            authz.bump_epoch(owned.world.org, db=c, reason='owned replay revoke')
        response = post(client, owned)
        assert response.status_code in (403, 404), response.text
    assert len(owned.provider.calls) == 1


def test_body_job_id_cannot_mint_apply_authority(client, owned, engine):
    job_id = 'owned_job_' + uuid4().hex
    with engine.begin() as c:
        c.execute(text("INSERT INTO jobs(id,project_id,kind) VALUES(:id,:p,'provider_apply')"),
                  {'id': job_id, 'p': owned.scope.p})
    with owned.world.acting_as(owned.world.ctx(owned.owner)):
        response = post(client, owned, created_by_job_id=job_id)
        assert response.status_code == 422 and 'leased owner protocol' in response.text, response.text
    assert not owned.provider.calls


def test_actual_session_csrf_is_required_before_apply(client, owned, engine, monkeypatch):
    import base64
    from datetime import datetime, timezone
    from boss_engine.identity import authn_support, sessions
    from boss_engine.config import get_settings
    monkeypatch.setattr(get_settings(), 'identity_server_key', base64.b64encode(b'owned471-csrf-fixture-key-material').decode())
    authn_support.reset_server_key_for_tests()
    try:
        with engine.begin() as c:
            issued = sessions.issue(c, principal_id=owned.owner.id, org_id=owned.world.org,
                methods=['password', 'totp'], mfa_at=datetime.now(timezone.utc),
                ip=None, user_agent=None, device_id_hash=None)
        client.cookies.clear()
        client.cookies.set(authn.SESSION_COOKIE, issued.token)
        path = f'/v1/changesets/{owned.scope.cs}/apply'
        # No IdentityWorld authentication seam here: real session lookup and CSRF HMAC.
        naked = client.post(path, json={'actor': owned.owner.id})
        assert naked.status_code == 403 and 'csrf_failed' in naked.text, naked.text
        assert not owned.provider.calls
        positive = client.post(path, json={'actor': 'not-the-authority'}, headers={
            'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin',
            'X-Boss-CSRF': sessions.csrf_token(issued.session_id)})
        assert positive.status_code == 200 and positive.json()['status'] == 'applied', positive.text
        assert len(owned.provider.calls) == 1
    finally:
        authn_support.reset_server_key_for_tests()


def test_revoked_after_committed_claim_refuses_before_provider(client, owned, engine, monkeypatch):
    original = owned.scope.service._get_changeset_any_project
    calls = []
    def at_read(c, changeset_id):
        calls.append(1)
        if len(calls) == 2:
            with engine.begin() as observer:
                assert observer.execute(text('SELECT count(*) FROM resource_receipts WHERE changeset_id=:id'),
                                        {'id': owned.scope.cs}).scalar_one() == 1
                observer.execute(text('DELETE FROM identity_role_bindings WHERE id=:id'), {'id': owned.binding})
                authz.bump_epoch(owned.world.org, db=observer, reason='owned revoke after claim')
        return original(c, changeset_id)
    monkeypatch.setattr(owned.scope.service, '_get_changeset_any_project', at_read)
    with owned.world.acting_as(owned.world.ctx(owned.owner)):
        response = post(client, owned)
    assert response.status_code == 403, response.text
    assert not owned.provider.calls
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM budget_reservations WHERE id=:r'),
                         {'r': owned.scope.reservation}).scalar_one() == 'released'
        assert c.execute(text("SELECT count(*) FROM events WHERE entity_id=:id AND event_type='changeset.not_dispatched'"),
                         {'id': owned.scope.cs}).scalar_one() == 1
