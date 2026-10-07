"""GET /v1/identity/me/projects: where the signed-in principal holds access, with what (R2, console
fixer's ask for Identity › Access requests — one read instead of probing every project)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _projects(world, who) -> dict[str, dict]:
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(who)):
        got = client.get("/v1/identity/me/projects")
    assert got.status_code == 200, got.text
    assert got.headers.get("cache-control") == "no-store"
    return {p["project_id"]: p for p in got.json()["projects"]}


def test_a_project_binding_lists_that_project_only_with_its_permissions(world):
    admin = world.person("mp-admin-a")
    world.bind(admin, "admin", project=world.project_a)
    mine = _projects(world, admin)
    assert set(mine) == {world.project_a}
    assert mine[world.project_a]["via"] == ["project"]
    perms = set(mine[world.project_a]["permissions"])
    assert "role_binding:assign" in perms and "code_task:read" in perms
    assert "user:invite" not in perms             # org-only: inert in a project binding (A3)


def test_a_group_binding_counts_and_an_org_binding_reaches_every_project(world):
    member = world.person("mp-group-member")
    group = world.group("mp-group", members=(member,))
    world.bind(group, "operator", project=world.project_b, subject_type="group")
    assert set(_projects(world, member)) == {world.project_b}
    viewer = world.person("mp-org-viewer")
    world.bind(viewer, "viewer")
    mine = _projects(world, viewer)
    assert {world.project_a, world.project_b} <= set(mine)
    assert all(p["via"] == ["org"] for p in mine.values())
    assert "role_binding:assign" not in set(mine[world.project_a]["permissions"])


def test_no_binding_no_projects(world):
    assert _projects(world, world.person("mp-nobody")) == {}
