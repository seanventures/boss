"""Every id a request names is confined to the request's project (IDENTITY.md §6.3; int-b).

`Scope.project(body="project_id")` proves the caller may act in project A. An id riding in the
same request — a task, a thread, a host target, a reservation, a plan — must be A's too, or a
permission held in A reaches B's row. Routes declare those ids (`@permit(..., confine=...)`) and
the enforcement point answers another project's object 404 before the handler runs.

The suite is generated from the declarations: for EVERY confined id of EVERY route, a principal of
project A naming project B's object is refused 404 ({"error": "not_found"}), and the same request
naming A's object is allowed (the control). Object loaders are pointed by id prefix (b_* is in
project B, a_* in A); the loaders themselves run against the real schema in
test_identity_authz_object_kinds.py. REQUIRED pins the ids the review found unconfined, so
dropping one of them from a declaration fails here.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Permit

#: (method, path) -> the ids that must be confined (identity int-b item 2).
REQUIRED = {
    ("POST", "/v1/plans"): {"task_id", "artifact_id"},
    ("POST", "/v1/plans/{plan_id}/dispatch"): {"plan_id"},
    ("POST", "/v1/code-tasks"): {"task_id"},
    ("POST", "/v1/jobs"): {"task_id", "work_item_id", "reservation_id"},
    ("POST", "/v1/work-orchestration/bot-identities"): {"agent_definition_id", "credential_secret_ref_id"},
    ("GET", "/v1/observability/stack"): {"host_target_id"},
    ("GET", "/v1/observability-suite/status"): {"host_target_id"},
    ("POST", "/v1/observability/stack/deploy"): {"project_id"},
    ("POST", "/v1/engine/agent/action"): {"thread_id"},
    ("POST", "/v1/delivery/deploy"): {"code_task_run_id"},
    ("POST", "/v1/factory/quality-gate"): {"code_task_run_id"},
    ("POST", "/v1/deployment-runs"): {"deployment_target_id"},
    ("POST", "/v1/deployments/stack/plan"): {"target_id"},
    ("POST", "/v1/delivery/first-deploy"): {"run_id", "target_id"},
    ("POST", "/v1/infra/targets/{target_id}/deploy"): {"target_id"},
}


def _confined_ops():
    for entry in identity_guard.route_inventory(app):
        access = entry.access
        if isinstance(access, Permit) and access.confine:
            for label in entry.labels():
                method, _, path = label.partition(" ")
                for rule in access.confine:
                    yield method, path, access, rule


CONFINED = list(_confined_ops())


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def admin(world):
    person = world.person("admin-of-a")
    world.bind(person, "admin", project=world.project_a)
    return person


@pytest.fixture
def placed(monkeypatch, world):
    """b_* objects live in project B, everything else in project A (this org)."""
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid: (world.org, world.project_b if str(oid).startswith("b_")
                                             else world.project_a))


def _request(method, path, access, rule, world, object_id):
    req = identity_matrix.request_for(method, path, access, world.project_a)
    if rule.where == "path":
        # the matrix filled every path parameter with obj_1; put this rule's id in its own slot
        req["url"] = path.replace("{" + rule.name + "}", object_id)
        for other in [p for p in re.findall(r"{(\w+)}", path) if p != rule.name]:
            req["url"] = req["url"].replace("{" + other + "}",
                                            world.project_a if other == "project_id" else "a_obj")
    elif rule.where == "query":
        req["params"] = {**req["params"], rule.name: object_id}
    else:
        req.setdefault("json", {})
        req["json"] = {**req["json"], rule.name: object_id}
    return req


def test_the_review_list_is_declared():
    declared = {}
    for method, path, _access, rule in CONFINED:
        declared.setdefault((method, path), set()).add(rule.name)
    missing = {op: sorted(ids - declared.get(op, set())) for op, ids in REQUIRED.items()
               if ids - declared.get(op, set())}
    assert not missing, missing


@pytest.mark.parametrize("method,path,access,rule", CONFINED,
                         ids=[f"{m} {p} {r.where}.{r.name}" for m, p, _a, r in CONFINED])
def test_another_projects_object_in_a_request_is_not_found(method, path, access, rule, mirror,
                                                          world, admin, placed):
    with world.acting_as(world.ctx(admin)):
        refused = mirror.request(**_request(method, path, access, rule, world, "b_obj"))
        allowed = mirror.request(**_request(method, path, access, rule, world, "a_obj"))
    assert refused.status_code == 404 and refused.json() == {"error": "not_found"}, refused.text
    assert allowed.status_code == 200, allowed.text


def test_an_id_that_names_nothing_passes_to_the_handler(mirror, world, monkeypatch):
    """No object, no project to leak: the handler answers its own not-found."""
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid: None if str(oid).startswith("none_")
                            else (world.org, world.project_a))
    admin = world.person("admin-of-a-none")
    world.bind(admin, "admin", project=world.project_a)
    with world.acting_as(world.ctx(admin)):
        out = mirror.post("/v1/plans", json={"project_id": world.project_a, "task_id": "none_t"},
                          headers={"sec-fetch-site": "same-origin"})
    assert out.status_code == 200, out.text
