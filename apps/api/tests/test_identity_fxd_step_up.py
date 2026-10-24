"""Step-up over HTTP, for EVERY step_up permission a route declares (IDENTITY.md §4.6; FX-D P1-21).

The role matrix always runs with an MFA-fresh session, so it never proves that a step-up route
refuses a stale one. Here an Owner (who holds every human permission) calls every operation whose
declared permission is `step_up`:

  * with a second factor older than the window (11 min) and with a password-only session -> 401
    {"error": "step_up_required"}, on the stub mirror AND on the real app (the guard refuses before
    any handler runs, so the real app's handlers are never reached);
  * with a second factor inside the window (9 min)                                -> allowed;
  * with an API key holding every scope (a bearer credential can never step up)   -> 401;
  * and the window follows the org's policy (step_up_minutes).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.identity_fxd_support import (
    PERMIT_OPS,
    STEP_UP_MINUTES,
    STEP_UP_OPS,
    place_objects,
    refusal_body_ok,
    report,
    role_perms,
    session_ctx,
)
from boss_engine.identity import authz
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.principal import PrincipalKind
from boss_engine.identity.scope import ScopeSource

STALE = timedelta(minutes=STEP_UP_MINUTES + 1)
FRESH = timedelta(minutes=STEP_UP_MINUTES - 1)


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def real():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("step-up-owner")
    world.bind(person, "owner")
    return person


@pytest.fixture
def placed(monkeypatch, world):
    place_objects(monkeypatch, world.org, world.project_a)


def _call(client, op, world):
    method, path, access, _ = op
    return client.request(**identity_matrix.request_for(method, path, access, world.project_a))


def test_the_sweep_covers_every_step_up_permission_a_route_declares():
    declared = {o[2].permission for o in PERMIT_OPS if PERMISSION_META[o[2].permission].step_up}
    swept = {o[2].permission for o in STEP_UP_OPS}
    catalogue = {k for k, m in PERMISSION_META.items() if m.step_up and not m.system_only}
    assert swept == declared and len(STEP_UP_OPS) > 300
    # The step-up permissions no route declares are the reserved ones (no enforcement point yet),
    # and the ones a HANDLER checks as a second permission through the same PDP, which applies
    # step-up itself (identity_access.require_permission -> authz.check -> STEP_UP_REQUIRED):
    #   app_secret:live: PUT /v1/apps/{app_key}/secrets with mode=live (routes_templates.py).
    handler_enforced = {"app_secret:live"}
    reserved = {"org:delete", "project:delete"}
    assert catalogue - swept <= reserved | handler_enforced, catalogue - swept
    assert set(swept) <= role_perms("owner")


@pytest.mark.parametrize("age", [STALE, None], ids=["mfa-11-min-old", "password-only"])
def test_a_stale_session_is_refused_step_up_on_every_step_up_route(mirror, world, owner, placed, age):
    wrong = []
    with world.acting_as(session_ctx(owner, mfa_age=age)):
        for op in STEP_UP_OPS:
            got = _call(mirror, op, world)
            if not refusal_body_ok(got, 401, "step_up"):
                wrong.append(f"{op[0]} {op[1]} [{op[2].permission}]: {got.status_code} {got.text[:100]}")
    assert not wrong, report(wrong, "stale session on step-up routes")


def test_a_second_factor_inside_the_window_is_not_refused(mirror, world, owner, placed):
    wrong = []
    with world.acting_as(session_ctx(owner, mfa_age=FRESH)):
        for op in STEP_UP_OPS:
            got = _call(mirror, op, world)
            if got.status_code != 200:
                wrong.append(f"{op[0]} {op[1]}: {got.status_code} {got.text[:100]}")
    assert not wrong, report(wrong, "fresh session on step-up routes")


def test_a_stale_session_never_reaches_a_real_handler_on_a_step_up_route(real, world, owner, placed):
    """The same sweep against the REAL app: the refusal comes from the global enforcement point,
    so no route (whatever its handler does) can serve a step-up permission to a stale session."""
    wrong = []
    with world.acting_as(session_ctx(owner, mfa_age=STALE)):
        for op in STEP_UP_OPS:
            got = _call(real, op, world)
            if not refusal_body_ok(got, 401, "step_up"):
                wrong.append(f"{op[0]} {op[1]} [{op[2].permission}]: {got.status_code} {got.text[:100]}")
    assert not wrong, report(wrong, "real app, stale session")


def test_non_step_up_routes_do_not_ask_a_stale_session_to_step_up(mirror, world, owner, placed):
    """The control: the refusal above is the step-up rule, not something every route does."""
    others = [o for o in PERMIT_OPS if not PERMISSION_META[o[2].permission].step_up
              and not PERMISSION_META[o[2].permission].system_only
              and not (PERMISSION_META[o[2].permission].org_only and o[2].scope.source != ScopeSource.ORG)]
    wrong = []
    with world.acting_as(session_ctx(owner, mfa_age=STALE)):
        for op in others:
            got = _call(mirror, op, world)
            if got.status_code != 200:
                wrong.append(f"{op[0]} {op[1]}: {got.status_code}")
    assert len(others) > 500 and not wrong, report(wrong, "non-step-up routes, stale session")


def test_an_api_key_with_every_scope_never_satisfies_step_up(mirror, world, owner, placed):
    """§4.6: bearer credentials cannot step up (A26), on every step-up route — while the same key
    is good for the non-step-up routes its scopes cover."""
    key = world.key_ctx(owner, tuple(sorted(role_perms("owner"))))
    refused, control = [], []
    with world.acting_as(key):
        for op in STEP_UP_OPS:
            got = _call(mirror, op, world)
            if not refusal_body_ok(got, 401, "step_up"):
                refused.append(f"{op[0]} {op[1]}: {got.status_code} {got.text[:80]}")
        for op in PERMIT_OPS:
            meta = PERMISSION_META[op[2].permission]
            if meta.step_up or meta.system_only or (meta.org_only and op[2].scope.source != ScopeSource.ORG):
                continue
            got = _call(mirror, op, world)
            if got.status_code != 200:
                control.append(f"{op[0]} {op[1]}: {got.status_code}")
    assert not refused, report(refused, "API key on step-up routes")
    assert not control, report(control, "API key on ordinary routes")


@pytest.mark.xfail(strict=True, reason=(
    "unassigned: IDENTITY.md §4.6 says a SERVICE ACCOUNT's key may use a step-up permission in its "
    "scopes; authz.step_up_fresh_until refuses every key (authz.py:144 says 'a PAT or "
    "service-account key never satisfies it') — the spec and the PDP disagree"))
def test_a_service_account_key_may_use_the_step_up_permissions_in_its_scopes(mirror, world, placed):
    robot = world.principal("deploy-bot", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(robot, "admin")
    admin_step_up = [o for o in STEP_UP_OPS if o[2].permission in role_perms("admin")
                     and not (PERMISSION_META[o[2].permission].org_only and o[2].scope.source != ScopeSource.ORG)]
    key = world.key_ctx(robot, tuple(sorted({o[2].permission for o in admin_step_up})))
    with world.acting_as(key):
        got = {o: _call(mirror, o, world).status_code for o in admin_step_up}
    wrong = [f"{o[0]} {o[1]}: {status}" for o, status in got.items() if status != 200]
    assert not wrong, report(wrong, "service-account key on its scoped step-up routes")


def test_the_window_is_the_orgs_step_up_minutes(mirror, world, owner, placed):
    """§4.6 / policy.step_up_minutes: shortening the window to 5 minutes refuses a 7-minute-old
    factor that the default window accepts."""
    sample = [o for o in STEP_UP_OPS if o[2].scope.source == ScopeSource.ORG][:25]
    seven = timedelta(minutes=7)
    with world.acting_as(session_ctx(owner, mfa_age=seven)):
        before = [_call(mirror, o, world).status_code for o in sample]
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_settings (org_id, policy, epoch) VALUES "
                        "(:o, '{\"step_up_minutes\": 5}', 1) ON CONFLICT (org_id) DO UPDATE SET "
                        "policy = identity_settings.policy || '{\"step_up_minutes\": 5}'"), {"o": world.org})
        authz.bump_epoch(world.org, db=db, reason="test.step_up_minutes")
    try:
        with world.acting_as(session_ctx(owner, mfa_age=seven)):
            after = [_call(mirror, o, world) for o in sample]
        with world.acting_as(session_ctx(owner, mfa_age=timedelta(minutes=4))):
            inside = [_call(mirror, o, world).status_code for o in sample]
    finally:
        with world.engine.begin() as db:
            db.execute(text("UPDATE identity_settings SET policy = policy - 'step_up_minutes' "
                            "WHERE org_id = :o"), {"o": world.org})
            authz.bump_epoch(world.org, db=db, reason="test.step_up_minutes")
    assert set(before) == {200}
    assert all(refusal_body_ok(r, 401, "step_up") for r in after), [r.status_code for r in after]
    assert set(inside) == {200}
