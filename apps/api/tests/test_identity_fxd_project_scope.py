"""The role matrix with roles bound at PROJECT scope, and the org-route sweep (FX-D P1-21).

test_identity_authz_matrix.py binds every built-in role at ORG scope, and its R3 sweeps use a
project Admin only. Here Admin, Operator and Viewer are bound to project A only (and one mixed
caller: Operator at A plus Viewer org-wide), and every operation is sent through the real
enforcement point and the real PDP:

  * every project and object operation in project A behaves per the role, minus the org-only
    permissions a project binding never carries (amendment A3);
  * every project operation naming project B is 403 and every object of project B is 404;
  * EVERY `@ org` operation is refused to a caller whose bindings are all project bindings — the
    expectation is derived from each declaration (identity_fxd_support.expect), not hand-listed —
    on the stub mirror, and on the real app for the deny side (no handler runs).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.identity_fxd_support import (
    HTTP_OPS,
    OBJECT_OPS,
    ORG_OPS,
    PROJECT_OPS,
    expect,
    place_objects,
    refusal_body_ok,
    report,
    role_perms,
    sweep,
)
from boss_engine.identity.declarations import Permit

#: (name, org-bound role or None, project-A-bound role)
CALLERS = [("admin@A", None, "admin"), ("operator@A", None, "operator"), ("viewer@A", None, "viewer"),
           ("operator@A+viewer@org", "viewer", "operator")]


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _caller(world, name, org_role, project_role):
    person = world.person(f"ps-{name}")
    world.bind(person, project_role, project=world.project_a)
    if org_role:
        world.bind(person, org_role)
    org_held = role_perms(org_role) if org_role else set()
    return person, org_held, {world.project_a: role_perms(project_role)}


@pytest.mark.parametrize("name, org_role, project_role", CALLERS, ids=[c[0] for c in CALLERS])
def test_in_its_own_project_a_project_bound_role_is_exactly_its_project_permissions(
        mirror, world, monkeypatch, name, org_role, project_role):
    person, org_held, project_held = _caller(world, name, org_role, project_role)
    place_objects(monkeypatch, world.org, world.project_a)
    want = lambda access: expect(access, org_held=org_held, project_held=project_held,  # noqa: E731
                                 project=world.project_a)
    with world.acting_as(world.ctx(person)):
        wrong = sweep(mirror, world, PROJECT_OPS + OBJECT_OPS, want, project=world.project_a)
    allowed = sum(1 for o in PROJECT_OPS + OBJECT_OPS if want(o[2]) == 200)
    assert allowed > 50 and not wrong, report(wrong, f"{name} in project A")


@pytest.mark.parametrize("name, org_role, project_role", CALLERS, ids=[c[0] for c in CALLERS])
def test_in_another_project_a_project_binding_grants_nothing(mirror, world, monkeypatch,
                                                             name, org_role, project_role):
    person, org_held, project_held = _caller(world, name, org_role, project_role)
    place_objects(monkeypatch, world.org, world.project_b)
    want = lambda access: expect(access, org_held=org_held, project_held=project_held,  # noqa: E731
                                 project=world.project_b)
    with world.acting_as(world.ctx(person)):
        wrong = sweep(mirror, world, PROJECT_OPS + OBJECT_OPS, want, project=world.project_b)
    assert not wrong, report(wrong, f"{name} in project B")
    if org_role is None:
        # Nothing at all in B: every project op 403 and every object 404 (no existence oracle).
        assert {want(o[2]) for o in PROJECT_OPS} == {403}
        assert {want(o[2]) for o in OBJECT_OPS} <= {403, 404}


@pytest.mark.parametrize("name, org_role, project_role", CALLERS, ids=[c[0] for c in CALLERS])
def test_every_org_route_is_refused_to_a_caller_with_only_project_bindings(
        mirror, world, name, org_role, project_role):
    person, org_held, project_held = _caller(world, name, org_role, project_role)
    want = lambda access: expect(access, org_held=org_held, project_held=project_held)  # noqa: E731
    with world.acting_as(world.ctx(person)):
        wrong = sweep(mirror, world, ORG_OPS, want, project=world.project_a)
    assert len(ORG_OPS) > 300 and not wrong, report(wrong, f"{name} on org routes")
    if org_role is None:
        assert {want(o[2]) for o in ORG_OPS} == {403}


def test_the_routes_that_need_no_permission_stay_open_to_a_project_only_caller(mirror, world):
    """The only org-level operations a project-only caller may use are the ones whose declaration
    needs no permission (public, or authenticated: the caller's own session, keys, catalogue)."""
    person, _, _ = _caller(world, "open", None, "viewer")
    open_ops = [o for o in HTTP_OPS if not isinstance(o[2], Permit)]
    with world.acting_as(world.ctx(person)):
        got = {(m, p): mirror.request(**identity_matrix.request_for(m, p, a, world.project_a)).status_code
               for m, p, a, _ in open_ops}
    wrong = [f"{m} {p}: {status}" for (m, p), status in got.items() if status != 200]
    assert open_ops and not wrong, wrong


@pytest.mark.parametrize("project_role", ["admin", "operator"])
def test_the_real_app_refuses_every_org_route_to_a_project_admin_or_operator(world, project_role):
    """The deny side on the REAL app: every refusal names the permission the route declares, and
    no handler runs for a project-only caller on an org route."""
    real = TestClient(app, raise_server_exceptions=False)
    person, _, _ = _caller(world, f"real-{project_role}", None, project_role)
    wrong = []
    with world.acting_as(world.ctx(person)):
        for method, path, access, _ in ORG_OPS:
            got = real.request(**identity_matrix.request_for(method, path, access, world.project_a))
            if not refusal_body_ok(got, 403, access.permission):
                wrong.append(f"{method} {path} [{access.permission}]: {got.status_code} {got.text[:100]}")
    assert not wrong, report(wrong, f"real app, {project_role}@A on org routes")


def _org_routes_that_take_a_project():
    """Org-declared operations (not org-only permissions) whose handler takes `project_id`: the
    request names a project, but the guard checks the permission org-wide."""
    import inspect

    from fastapi.routing import APIRoute

    from boss_engine.identity.declarations import access_of
    from boss_engine.identity.permissions_gen import PERMISSION_META
    from boss_engine.identity.scope import ScopeSource

    out = []
    for route in app.router.routes:
        access = access_of(getattr(route, "endpoint", None)) if isinstance(route, APIRoute) else None
        if (isinstance(access, Permit) and access.scope.source == ScopeSource.ORG
                and not PERMISSION_META[access.permission].org_only
                and not PERMISSION_META[access.permission].system_only
                and "project_id" in inspect.signature(route.endpoint).parameters):
            for method in sorted(route.methods or ()):
                out.append((method, route.path, access, "http"))
    return out


#: Routes that take a project_id yet are org-wide BY DESIGN ("label": "why"). Empty today: each
#: entry is a reviewed decision that a project-bound person may not use the route for their project.
ORG_WIDE_BY_DESIGN: dict[str, str] = {}
ORG_ROUTES_NAMING_A_PROJECT = [o for o in _org_routes_that_take_a_project()
                               if f"{o[0]} {o[1]}" not in ORG_WIDE_BY_DESIGN]


@pytest.mark.xfail(strict=True, reason=(
    "unassigned: ~40 routes take a project_id but are declared @ org (GET /v1/code-tasks, /v1/tasks, "
    "/v1/dashboard, /v1/jobs, /v1/activity, /v1/previews, PUT /v1/projects/{project_id}/settings/"
    "builds_at_once ...): an Operator or Admin bound to that project, holding the permission there, "
    "is refused 403 for their own project. Fix: declare them project-scoped, or list a route in "
    "ORG_WIDE_BY_DESIGN with its reason"))
def test_a_project_bound_role_reaches_the_routes_that_name_its_project(mirror, world):
    assert len(ORG_ROUTES_NAMING_A_PROJECT) > 10
    person, _, project_held = _caller(world, "names-project", None, "admin")
    held = project_held[world.project_a]
    want = lambda access: 200 if access.permission in held else 403  # noqa: E731
    with world.acting_as(world.ctx(person)):
        wrong = sweep(mirror, world, ORG_ROUTES_NAMING_A_PROJECT, want, project=world.project_a)
    assert not wrong, report(wrong, "admin@A on org-declared routes naming project A")
