"""Integration (lead): under enforce, the org picker and project list follow identity, not the
legacy membership tables — an invited person with no legacy membership sees exactly the projects
they may read (a new Operator landed on an empty org picker in the enforce-mode walk)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

client = TestClient(app)


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def test_a_project_viewer_sees_their_org_at_that_project_and_nothing_else(world):
    vic = world.person("vic")
    world.bind(vic, "viewer", project=world.project_b)
    with world.acting_as(world.ctx(vic)):
        orgs = client.get("/v1/orgs")
        projects = client.get("/v1/projects")
    assert orgs.status_code == 200, orgs.text
    assert [(o["org_id"], o["project_id"]) for o in orgs.json()["items"]] == [(world.org, world.project_b)]
    assert [p["id"] for p in projects.json()["items"]] == [world.project_b]


def test_an_org_operator_sees_every_project_of_their_org_only(world):
    oscar = world.person("oscar")
    world.bind(oscar, "operator")
    with world.acting_as(world.ctx(oscar)):
        projects = client.get("/v1/projects").json()["items"]
    assert sorted(p["id"] for p in projects) == sorted([world.project_a, world.project_b])
