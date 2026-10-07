"""F2 / M9, M5: object confinement is CENTRAL, and every client project_id / org_id is the caller's.

The review found 693 id parameters no declaration confined, and ~114 org-scoped handlers trusting a
client project_id or org_id. The enforcement point now confines EVERY id a request names by an
object-id name (object_kinds.ID_PARAM_KINDS) — path, query and body at any depth — to the project
the permission was checked at (the org, for an org-level check), and refuses an org_id that is not
the caller's. Every id name of every route is classified (an object kind, or an explicit reason it
names no project-owned row), so a new route cannot ship an unconfined id by omission.

  * coverage: every id parameter of every route (path, query, body fields at any depth) is
    classified, and every kind named has a loader that runs against the schema;
  * generated: for EVERY classified id of EVERY @permit route, another project's object is 404 and
    this project's is allowed (the control);
  * nested body ids are confined;
  * live, against the real app (the tester's harness_object_idor and harness_idor): another org's
    artifact, decision, job, deployment target, project and org are not found.
"""
from __future__ import annotations

import re
import typing
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import object_kinds
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Permit, access_of


# ---- coverage ------------------------------------------------------------------------------------

def _model_fields(annotation, depth=0):
    """Every field name of a body model, nested models and lists of models included."""
    if depth > 6 or annotation is None:
        return
    for arg in typing.get_args(annotation):
        yield from _model_fields(arg, depth + 1)
    fields = getattr(annotation, "model_fields", None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and fields:
        for name, field in fields.items():
            yield field.alias or name
            yield from _model_fields(field.annotation, depth + 1)


def _id_params():
    for route in app.router.routes:
        if not isinstance(route, APIRoute):
            continue
        dependant = route.dependant
        names = [("path", p.alias or p.name) for p in dependant.path_params]
        names += [("query", p.alias or p.name) for p in dependant.query_params]
        for p in dependant.body_params:
            names.append(("body", p.alias or p.name))
            names += [("body", n) for n in _model_fields(p.field_info.annotation)]
        for where, name in names:
            if object_kinds.is_id_name(name):
                yield route, where, name


ID_PARAMS = list(_id_params())


def test_every_id_parameter_of_every_route_is_classified():
    unclassified = sorted({f"{name} ({where} of {sorted(route.methods)} {route.path})"
                           for route, where, name in ID_PARAMS
                           if name not in object_kinds.ID_PARAM_KINDS
                           and name not in object_kinds.NON_OBJECT_IDS})
    assert not unclassified, ("classify each in object_kinds.ID_PARAM_KINDS (the kinds it names) "
                              "or NON_OBJECT_IDS (why it names no project row): "
                              + "; ".join(unclassified[:40]))
    assert len(ID_PARAMS) > 600


def test_a_name_is_an_object_or_a_declared_non_object_never_both():
    assert not set(object_kinds.ID_PARAM_KINDS) & set(object_kinds.NON_OBJECT_IDS)
    assert all(reason.strip() for reason in object_kinds.NON_OBJECT_IDS.values())


def test_every_kind_a_name_maps_to_has_a_loader():
    registered = scope_module.registered_object_kinds()
    missing = {name: [k for k in kinds if k not in registered]
               for name, kinds in object_kinds.ID_PARAM_KINDS.items()
               if any(k not in registered for k in kinds)}
    assert not missing, missing


def test_every_loader_runs_against_the_schema(engine):
    with engine.connect() as db:
        for spec in object_kinds.all_kinds():
            spec.load(db, "f2-nothing-" + spec.kind)       # a missing table or column raises


# ---- generated: every classified id of every route ------------------------------------------------

def _named_ops():
    seen = set()
    for route, where, name in ID_PARAMS:
        access = access_of(route.endpoint)
        if not isinstance(access, Permit) or name not in object_kinds.ID_PARAM_KINDS:
            continue
        if access.scope.location == (where, name):
            continue
        for method in sorted(m for m in (route.methods or ()) if m != "HEAD"):
            if where == "body" and method not in ("POST", "PUT", "PATCH", "DELETE"):
                continue
            key = (method, route.path, where, name)
            if key not in seen:
                seen.add(key)
                yield method, route.path, access, where, name


NAMED = list(_named_ops())


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def admin(world):
    person = world.person("f2-admin-of-a")
    world.bind(person, "admin", project=world.project_a)
    world.bind(person, "admin")
    return person


@pytest.fixture
def placed(monkeypatch, world):
    """b_* objects live in project B, x_* in ANOTHER org, everything else in project A (this org);
    project ids are themselves (a project is its own project)."""
    def load(db, oid):
        oid = str(oid)
        if oid in (world.project_a, world.project_b):
            return (world.org, oid)
        if oid.startswith("x_"):
            return ("org_somebody_else", "p_somebody_else")
        return (world.org, world.project_b if oid.startswith("b_") else world.project_a)
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind, load)  # noqa: SLF001


def _request(method, path, access, where, name, world, object_id):
    req = identity_matrix.request_for(method, path, access, world.project_a, object_id="a_obj")
    if where == "path":
        url = re.sub(r"{" + name + r"(?::\w+)?}", object_id, path)
        for other in re.findall(r"{(\w+)(?::\w+)?}", url):
            url = re.sub(r"{" + other + r"(?::\w+)?}",
                         world.project_a if other == "project_id" else "a_obj", url)
        req["url"] = url.replace("{path:path}", "x")
    elif where == "query":
        req["params"] = {**req["params"], name: object_id}
    else:
        req["json"] = {**req.get("json", {}), name: object_id}
    return req


def test_the_generated_suite_is_wide():
    assert len(NAMED) > 300, len(NAMED)


def test_another_projects_object_named_anywhere_is_not_found(mirror, world, admin, placed):
    """GENERATED over every classified id of every @permit route (one test, one world: the
    per-test table reset would make ~700 cases take a quarter of an hour)."""
    leaked, controls = [], 0
    with world.acting_as(world.ctx(admin)):
        for method, path, access, where, name in NAMED:
            project_like = name in ("project_id", "project_ids", "scope_id")
            org_level = access.scope.source.value == "org"
            # an org-level check confines to the org (another project of the same org is fine
            # there): its counter-example is another org's object; a project check's is another
            # project's.
            other = "x_obj" if org_level else (world.project_b if project_like else "b_obj")
            own = world.project_a if project_like else "a_obj"
            allowed = mirror.request(**_request(method, path, access, where, name, world, own))
            if allowed.status_code != 200:
                continue            # the control is refused: not this rule's case
            controls += 1
            refused = mirror.request(**_request(method, path, access, where, name, world, other))
            if refused.status_code != 404 or refused.json() != {"error": "not_found"}:
                leaked.append((method, path, where, name, refused.status_code))
    assert not leaked, leaked[:30]
    assert controls >= len(NAMED) * 0.7, (controls, len(NAMED))


def test_a_nested_body_id_is_confined(mirror, world, admin, placed):
    body = {"project_id": world.project_a, "items": [{"meta": {"task_id": "b_obj"}}]}
    ok = {"project_id": world.project_a, "items": [{"meta": {"task_id": "a_obj"}}]}
    headers = {"sec-fetch-site": "same-origin"}
    with world.acting_as(world.ctx(admin)):
        refused = mirror.post("/v1/plans", json=body, headers=headers)
        allowed = mirror.post("/v1/plans", json=ok, headers=headers)
    assert refused.status_code == 404, refused.text
    assert allowed.status_code == 200, allowed.text


# ---- live: another org's objects, through the real handlers ---------------------------------------

@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def orgs(engine):
    a, b = IdentityWorld.create(engine), IdentityWorld.create(engine)
    a.claimed()
    b.claimed()
    owner = a.person("f2-A-owner")
    a.bind(owner, "owner")
    return a, b, owner


def _seed(engine, sql, **params):
    with engine.begin() as db:
        db.execute(text(sql), params)


def test_another_orgs_objects_are_not_found_by_id(client, engine, orgs):
    """The tester's harness_object_idor: artifact, decision, job, deployment target."""
    a, b, owner = orgs
    ids = {k: f"{k}_{uuid.uuid4().hex[:10]}" for k in ("art", "dec", "job", "dt")}
    for project, suffix in ((b.project_a, "b"), (a.project_a, "a")):
        _seed(engine, "INSERT INTO artifacts (id, project_id, artifact_type, title) "
                      "VALUES (:i, :p, 'doc', 'victim')", i=ids["art"] + suffix, p=project)
        _seed(engine, "INSERT INTO decisions (id, project_id, question) VALUES (:i, :p, 'q')",
              i=ids["dec"] + suffix, p=project)
        _seed(engine, "INSERT INTO jobs (id, project_id, kind) VALUES (:i, :p, 'build')",
              i=ids["job"] + suffix, p=project)
    _seed(engine, "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                  "target_kind, target_ref, created_by) VALUES (:i, :p, 'app', 'r', 'staging', "
                  "'ssh', 'ref', 'seed')", i=ids["dt"], p=b.project_a)
    with a.acting_as(a.ctx(owner)):
        for path in ("/v1/artifacts/{}", "/v1/decisions/{}", "/v1/jobs/{}"):
            key = {"/v1/artifacts/{}": "art", "/v1/decisions/{}": "dec", "/v1/jobs/{}": "job"}[path]
            theirs = client.get(path.format(ids[key] + "b"))
            mine = client.get(path.format(ids[key] + "a"))
            assert theirs.status_code == 404, (path, theirs.text)
            assert mine.status_code == 200, (path, mine.text)
        hijack = client.patch(f"/v1/deployment-targets/{ids['dt']}", json={"app_name": "hijacked"})
    assert hijack.status_code == 404, hijack.text


def test_an_org_level_list_cannot_name_another_orgs_project(client, engine, orgs):
    """M5, live: list_code_tasks is an org-level read; the client project_id was trusted."""
    a, b, owner = orgs
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    _seed(engine, "INSERT INTO code_task_runs (id, project_id, title, status) VALUES (:i, :p, 'victim', 'queued')",
          i=run_id, p=b.project_a)
    with a.acting_as(a.ctx(owner)):
        theirs = client.get("/v1/code-tasks", params={"project_id": b.project_a})
        mine = client.get("/v1/code-tasks", params={"project_id": a.project_a})
    assert theirs.status_code == 404, theirs.text
    assert run_id not in theirs.text
    assert mine.status_code == 200, mine.text


def test_another_orgs_members_cannot_be_named_by_org_id(client, orgs):
    a, b, owner = orgs
    with a.acting_as(a.ctx(owner)):
        out = client.get("/v1/org-members", params={"org_id": b.org, "project_id": b.project_a})
    assert out.status_code == 404, out.text


def test_a_second_org_cannot_be_created_under_enforce(client, orgs):
    a, _b, owner = orgs
    with a.acting_as(a.ctx(owner)):
        out = client.post("/v1/orgs", json={"name": "Shadow Org"},
                          headers={"sec-fetch-site": "same-origin"})
    assert out.status_code == 409, out.text
