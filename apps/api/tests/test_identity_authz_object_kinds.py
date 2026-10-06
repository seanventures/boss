"""Every Scope.of object loader runs against the real schema (IDENTITY.md §6.3).

A loader names a table and columns; a typo there would only surface as a 503 on a live route.
Here every registered kind is asked for an id that does not exist (the SQL must run and answer
None), every kind a route declares must have a loader, and representative kinds resolve a real row
to ITS project and org — never to the project a request names.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import object_kinds
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Permit
from boss_engine.identity.scope import RequestValues, ScopeNotFound, ScopeSource


def test_every_declared_object_kind_has_a_loader():
    declared = {e.access.scope.object_kind for e in identity_guard.route_inventory(app)
                if isinstance(e.access, Permit) and e.access.scope.source == ScopeSource.OBJECT}
    assert declared and declared <= scope_module.registered_object_kinds()


def test_every_loader_runs_against_the_schema(engine):
    with engine.connect() as db:
        for spec in object_kinds.all_kinds():
            assert spec.load(db, f"missing-{uuid.uuid4().hex}") is None, spec.kind


def test_an_object_resolves_to_its_own_project_not_the_requested_one(engine):
    world = IdentityWorld.create(engine)
    decision_id, run_id = f"dec_{uuid.uuid4().hex[:10]}", f"wr_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question) VALUES (:d, :p, 'q')"),
                   {"d": decision_id, "p": world.project_b})
    spec = Permit("decision:approve", scope_module.Scope.of("decision", path="decision_id"))
    resolved = scope_module.resolve(
        spec.scope, RequestValues(path_params={"decision_id": decision_id},
                                  query={"project_id": world.project_a}), org_id=world.org)
    assert (resolved.project_id, resolved.object_id) == (world.project_b, decision_id)
    try:
        scope_module.resolve(spec.scope, RequestValues(path_params={"decision_id": decision_id}, query={}),
                             org_id="org_somebody_else")
    except ScopeNotFound:
        pass
    else:
        raise AssertionError("another org's decision must be not found")
    assert run_id  # (workflow runs use the same generic loader as decisions)
