"""A Seed Owner may cancel a run; the real PDP gives an Owner every permission a person can hold.

The reported 403 on POST /v1/workflow-runs/{id}/cancel was a request without `project_id`: the
guard could not resolve the route's scope and said "forbidden: workflow_run:control". It is now a
422 naming what is missing (engine/tests/test_the_owner_holds_every_permission.py pins the rule).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.scope import ResolvedScope


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def test_the_real_pdp_gives_an_owner_every_non_system_permission(world):
    owner = world.person("Seed Owner")
    world.bind(owner, "owner")
    ctx = world.ctx(owner)
    refused = []
    for key, meta in PERMISSION_META.items():
        if meta.system_only:
            continue
        scope = (ResolvedScope.org_level(world.org) if meta.org_only
                 else ResolvedScope.in_project(world.org, world.project_a))
        decision = authz.check(ctx, key, scope, db=None)
        if not decision.allowed:
            refused.append((key, decision.reason))
    assert not refused, refused


def test_an_owner_cancels_a_run_and_a_request_without_project_id_is_a_422(world, mirror):
    owner = world.person("Cancelling Owner")
    world.bind(owner, "owner")
    with world.acting_as(world.ctx(owner)):
        ok = mirror.post("/v1/workflow-runs/wfr_x/cancel",
                         json={"project_id": world.project_a, "reason": "stop"})
        assert ok.status_code == 200, ok.text
        bad = mirror.post("/v1/workflow-runs/wfr_x/cancel", json={"reason": "stop"})
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"] == "scope_unresolved"
    assert "permission" not in bad.json() and "project_id" in bad.json()["detail"]
