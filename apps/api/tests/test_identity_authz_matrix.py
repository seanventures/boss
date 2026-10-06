"""The role x operation matrix (§15.5) and the cross-project / cross-org 404 suite (§15.4 R3).

Both run every operation of the real app through the real enforcement point (enforce mode) and
the real PDP against real bindings, on a mirror whose handlers are stubs (identity_matrix.py).
Object loaders are pointed at a fixture project so every Scope.of route resolves; the loaders
themselves are tested against the real schema in test_identity_authz_object_kinds.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import scope as scope_module
from boss_engine.identity.catalogue import HUMAN_ROLES
from boss_engine.identity.declarations import Permit
from boss_engine.identity.scope import ScopeSource

OPS = identity_matrix.operations(app)
HTTP_OPS = [o for o in OPS if o[3] in ("http", "framework") and o[0] != "WS"]


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def objects_in(monkeypatch, world):
    """Point every object kind at one fixture project (or another org)."""
    def place(org: str, project: str) -> None:
        for kind in scope_module.registered_object_kinds():
            monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                                lambda db, oid, o=org, p=project: (o, p))
    return place


def test_the_committed_matrix_is_current():
    current = identity_matrix.render(identity_matrix.build(app))
    committed = identity_matrix.MATRIX_PATH.read_text(encoding="utf-8")
    assert committed == current, ("tests/identity/matrix.yaml is stale: run "
                                  "`PYTHONPATH=.:engine python -m apps.api.tests.identity_matrix` "
                                  "and review the diff")
    assert "UNDECLARED" not in committed


@pytest.mark.parametrize("role", HUMAN_ROLES)
def test_every_operation_for_every_built_in_role(role, mirror, world, objects_in):
    person = world.person(f"matrix-{role}")
    world.bind(person, role)
    objects_in(world.org, world.project_a)
    wrong: list[str] = []
    with world.acting_as(world.ctx(person)):
        for method, path, access, _kind in HTTP_OPS:
            if _kind == "framework":
                continue   # the mirror serves no docs; framework routes are checked below
            want = identity_matrix.expected(access, role)
            got = mirror.request(**identity_matrix.request_for(method, path, access, world.project_a))
            ok = got.status_code == 200 if want == 200 else got.status_code == want
            if not ok:
                wrong.append(f"{method} {path}: want {want} got {got.status_code} {got.text[:120]}")
    assert not wrong, f"{len(wrong)} wrong for {role}:\n" + "\n".join(wrong[:40])


def test_framework_routes_are_public_at_request_time(world):
    client = TestClient(app)
    with world.acting_as(None):
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200


OBJECT_OPS = [o for o in HTTP_OPS if isinstance(o[2], Permit) and o[2].scope.source == ScopeSource.OBJECT]
PROJECT_OPS = [o for o in HTTP_OPS if isinstance(o[2], Permit) and o[2].scope.source == ScopeSource.PROJECT]


def test_r3_another_projects_object_is_404_even_with_full_permissions_here(mirror, world, objects_in):
    """An Admin of project A asks for an object of project B: 404, never 403 or 200."""
    admin = world.person("admin-of-a")
    world.bind(admin, "admin", project=world.project_a)
    objects_in(world.org, world.project_b)
    wrong = []
    with world.acting_as(world.ctx(admin)):
        for method, path, access, _ in OBJECT_OPS:
            got = mirror.request(**identity_matrix.request_for(method, path, access, world.project_a))
            if got.status_code != 404 or got.json() != {"error": "not_found"}:
                wrong.append(f"{method} {path}: {got.status_code}")
    assert len(OBJECT_OPS) > 50 and not wrong, wrong


def test_r3_another_orgs_object_is_404(mirror, world, objects_in):
    owner = world.person("owner-here")
    world.bind(owner, "owner")
    objects_in("org_somebody_else", world.project_a)
    with world.acting_as(world.ctx(owner)):
        wrong = [f"{m} {p}" for m, p, a, _ in OBJECT_OPS
                 if mirror.request(**identity_matrix.request_for(m, p, a, world.project_a)).status_code != 404]
    assert not wrong, wrong


def test_r3_the_same_object_in_my_project_is_reachable(mirror, world, objects_in):
    """The control for the two above: the only difference is where the object lives."""
    admin = world.person("admin-of-a-2")
    world.bind(admin, "admin", project=world.project_a)
    objects_in(world.org, world.project_a)
    with world.acting_as(world.ctx(admin)):
        statuses = {mirror.request(**identity_matrix.request_for(m, p, a, world.project_a)).status_code
                    for m, p, a, _ in OBJECT_OPS if a.permission not in
                    {"owner:assign"} and not identity_matrix.PERMISSION_META[a.permission].org_only}
    assert 200 in statuses and 404 not in statuses


def test_r3_a_named_project_the_caller_cannot_read_is_refused(mirror, world):
    """Project-scoped routes take the project the request names; naming project B from an
    Admin-of-A session is refused (and a project from another org is not found)."""
    admin = world.person("admin-of-a-3")
    world.bind(admin, "admin", project=world.project_a)
    with world.acting_as(world.ctx(admin)):
        wrong = []
        for method, path, access, _ in PROJECT_OPS:
            other = mirror.request(**identity_matrix.request_for(method, path, access, world.project_b))
            foreign = mirror.request(**identity_matrix.request_for(method, path, access, "pb-not-ours"))
            if other.status_code != 403 or foreign.status_code != 404:
                wrong.append(f"{method} {path}: {other.status_code}/{foreign.status_code}")
    assert len(PROJECT_OPS) > 500 and not wrong, wrong[:40]
