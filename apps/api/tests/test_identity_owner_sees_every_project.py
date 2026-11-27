"""An org Owner sees every project in the org, whoever created it (2026-11-19 report: "Park bench
map", made through Easy Mode by one Owner, was said to be missing for the other Owner).

Every project list — GET /v1/projects, the Projects cards (GET /v1/projects/summary) and the org
picker (GET /v1/orgs) — answers from visible_project_rows / readable_projects, which ask the PDP
for project:read per project. An org-scoped binding reaches every project in the org, so the
creator is irrelevant: nothing person-scoped (created_by, a human project_members row) decides
what an Owner sees. Easy Mode (services/factory_launch) and POST /v1/projects both make the
project through services.projects.create, so their memberships cannot differ.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import projects as project_service


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _created_by(world, engine, creator) -> str:
    """A project made the way Easy Mode makes one: services.projects.create, as `creator`."""
    with engine.begin() as db:
        made = project_service.create(
            db, get_settings(), auth=world.ctx(creator), name=f"Park bench map {creator.id[-8:]}",
            actor=creator.id, project_type="app")
    return str(made["project_id"])


def _ids(client, path: str) -> set[str]:
    got = client.get(path)
    assert got.status_code == 200, got.text
    return {str(i.get("id") or i.get("project_id")) for i in got.json()["items"]}


def test_the_second_owner_sees_the_project_the_first_owner_created(world, engine):
    first, second = world.person("own-first"), world.person("own-second")
    world.bind(first, "owner")
    world.bind(second, "owner")
    made = _created_by(world, engine, first)
    client = TestClient(app, raise_server_exceptions=False)
    for who in (first, second):
        with world.acting_as(world.ctx(who)):
            listed = _ids(client, "/v1/projects")
            cards = _ids(client, "/v1/projects/summary")
            opened = client.get(f"/v1/projects/{made}/overview")
        assert {made, world.project_a, world.project_b} <= listed, who.id
        assert made in cards, who.id
        assert opened.status_code == 200, (who.id, opened.text)


def test_an_org_viewer_sees_it_and_a_person_with_no_binding_does_not(world, engine):
    owner, viewer, nobody = (world.person("own-o"), world.person("own-viewer"),
                             world.person("own-nobody"))
    world.bind(owner, "owner")
    world.bind(viewer, "viewer")
    made = _created_by(world, engine, owner)
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(viewer)):
        assert made in _ids(client, "/v1/projects")
    with world.acting_as(world.ctx(nobody)):
        assert made not in _ids(client, "/v1/projects")
