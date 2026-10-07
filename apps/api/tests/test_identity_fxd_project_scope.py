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
from boss_engine.identity.scope import ScopeSource

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


def _routes_that_take_a_project(source):
    """`source`-declared operations (not org-only/system-only permissions) whose handler takes
    `project_id`: the request names a project."""
    import inspect

    from fastapi.routing import APIRoute

    from boss_engine.identity.declarations import access_of
    from boss_engine.identity.permissions_gen import PERMISSION_META

    out = []
    for route in app.router.routes:
        access = access_of(getattr(route, "endpoint", None)) if isinstance(route, APIRoute) else None
        if (isinstance(access, Permit) and access.scope.source == source
                and not PERMISSION_META[access.permission].org_only
                and not PERMISSION_META[access.permission].system_only
                and "project_id" in inspect.signature(route.endpoint).parameters):
            for method in sorted(route.methods or ()):
                out.append((method, route.path, access, "http"))
    return out


#: Routes that take a project_id yet are org-wide BY DESIGN ("label": "why"). Each entry is a
#: reviewed decision that a project-bound person may not use the route for their project: what it
#: reads or changes belongs to the whole install, and `project_id` only names a reader check.
_TELEMETRY = ("install-wide: what this install shares with the BOSS maintainers (or collects, in "
              "collector mode) is one stream for every project; project_id only names the reader")
ORG_WIDE_BY_DESIGN: dict[str, str] = {
    **{f"{m} /v1/telemetry/{p}": _TELEMETRY for m, p in (
        ("GET", "sharing"), ("GET", "preview"), ("POST", "preview/load"), ("GET", "sends/{send_id}"),
        ("GET", "export"), ("POST", "export/load"), ("GET", "collector"), ("GET", "collector.csv"))},
    "PUT /v1/projects/{project_id}/settings/builds_at_once": (
        "install-wide: builds share one machine, so how many run at once is one number for all "
        "projects (system_health.put_builds_at_once)"),
    "POST /v1/delivery/health/sweep": (
        "also fails hung deployments in EVERY project (fail_hung_deployments), whatever project "
        "it names"),
}
ORG_ROUTES_NAMING_A_PROJECT = _routes_that_take_a_project(ScopeSource.ORG)
#: The routes whose project_id is optional: checked at the project named, at the org otherwise.
PROJECT_OR_ORG_OPS = [o for o in _routes_that_take_a_project(ScopeSource.PROJECT)
                      if o[2].scope.org_when_absent]


def test_no_route_that_names_a_project_is_checked_org_wide_unless_justified():
    """THE CLASS (R2 #6): a route that takes project_id is declared at that project
    (Scope.project / Scope.project_or_org); an org-wide one is a reviewed ORG_WIDE_BY_DESIGN entry."""
    labels = {f"{m} {p}" for m, p, _a, _k in ORG_ROUTES_NAMING_A_PROJECT}
    unjustified = sorted(labels - set(ORG_WIDE_BY_DESIGN))
    assert not unjustified, ("declared @ org but they take project_id: declare them "
                             "Scope.project(...) or Scope.project_or_org(query=...), or justify "
                             f"them in ORG_WIDE_BY_DESIGN: {unjustified}")
    stale = sorted(set(ORG_WIDE_BY_DESIGN) - labels)
    assert not stale, f"ORG_WIDE_BY_DESIGN names routes that are no longer org-wide: {stale}"


def test_a_project_bound_role_reaches_the_routes_that_name_its_project(mirror, world):
    assert len(PROJECT_OR_ORG_OPS) > 25
    person, _, project_held = _caller(world, "names-project", None, "admin")
    held = project_held[world.project_a]
    want = lambda access: 200 if access.permission in held else 403  # noqa: E731
    with world.acting_as(world.ctx(person)):
        wrong = sweep(mirror, world, PROJECT_OR_ORG_OPS, want, project=world.project_a)
    assert not wrong, report(wrong, "admin@A on routes naming project A")


def test_naming_no_project_is_an_org_question_a_project_binding_cannot_answer(mirror, world):
    """Without a project_id the handler reads the default project (or every project): decided at
    the org, so a caller bound only to project A is refused, and an org Viewer is not."""
    person, _, _ = _caller(world, "names-none", None, "admin")
    org_viewer = world.person("ps-org-viewer-none")
    world.bind(org_viewer, "viewer")
    viewer_perms = role_perms("viewer")
    wrong = []
    for method, path, access, _ in PROJECT_OR_ORG_OPS:
        request = identity_matrix.request_for(method, path, access, world.project_a)
        request["params"].pop("project_id", None)
        with world.acting_as(world.ctx(person)):
            got = mirror.request(**request).status_code
        if got != 403:
            wrong.append(f"admin@A {method} {path}: {got}")
        with world.acting_as(world.ctx(org_viewer)):
            got = mirror.request(**request).status_code
        if got != (200 if access.permission in viewer_perms else 403):
            wrong.append(f"viewer@org {method} {path}: {got}")
    assert not wrong, report(wrong, "no project named")
