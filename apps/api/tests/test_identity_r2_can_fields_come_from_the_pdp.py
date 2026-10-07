"""R2 #5: every `can_*` field a read returns is the PDP's answer for the signed-in principal.

Agent Loadout's `can_edit` (GET /v1/agents/{role_key}/instructions) was the legacy members table
asked about the `actor=` query parameter: under enforce no identity person is a legacy member, so
an Admin saw a read-only Loadout, and the answer followed a claimed actor, not the caller. Infra's
`can_write` read project_members the same way. Both now come from identity.viewer.may: the
permission the control's write route declares, held by the request's verified principal at that
project — whatever `actor=`/`viewer=` says.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture
def install(engine):
    with engine.connect() as db:
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
        project = db.execute(text("SELECT p.id FROM projects p ORDER BY p.created_at, p.id LIMIT 1")).scalar()
        role_key = db.execute(text("SELECT role_key FROM roles WHERE project_id = :p ORDER BY 1 LIMIT 1"),
                              {"p": project}).scalar()
    assert org and project and role_key
    return IdentityWorld(engine, org, "ws-unused", project, project), role_key


def _can_edit(world, who, role_key, actor="ceo") -> bool:
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(who)):
        got = client.get(f"/v1/agents/{role_key}/instructions",
                         params={"project_id": world.project_a, "actor": actor})
    assert got.status_code == 200, got.text[:300]
    return got.json()["can_edit"]


def test_loadout_can_edit_is_the_callers_agent_update_not_the_claimed_actor(install):
    world, role_key = install
    admin = world.person("r2-can-admin")
    world.bind(admin, "admin", project=world.project_a)       # agent:update at the project
    viewer = world.person("r2-can-viewer")
    world.bind(viewer, "viewer")                              # agent:read only, org-wide
    assert _can_edit(world, admin, role_key) is True
    assert _can_edit(world, admin, role_key, actor="nobody") is True
    # claiming the console's legacy seat buys nothing
    assert _can_edit(world, viewer, role_key, actor="ceo") is False


def test_infra_can_write_is_the_callers_host_manage(install):
    world, _ = install
    client = TestClient(main.app, raise_server_exceptions=False)
    admin = world.person("r2-infra-admin")
    world.bind(admin, "admin", project=world.project_a)
    operator = world.person("r2-infra-operator")
    world.bind(operator, "operator")
    seen = {}
    for name, who in (("admin", admin), ("operator", operator)):
        with world.acting_as(world.ctx(who)):
            got = client.get("/v1/infra/fleet", params={"project_id": world.project_a, "viewer": "ceo"})
        assert got.status_code == 200, got.text[:300]
        seen[name] = got.json()["preconditions"]["can_write"]
    assert seen == {"admin": True, "operator": False}
