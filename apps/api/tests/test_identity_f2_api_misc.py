"""F2 security fixes at the API layer: H5 (system members), M1 (legacy SCIM), the SCIM route
collision, the production label, the read-only MCP server list and the socket peer.

Each test fails without its fix:
  * H5: POST /v1/members or /v1/org-members with member_type "system" minted an Owner-equivalent
    sys_legacy_* principal at the next migration seed; the API refuses the type (422).
  * M1: the legacy project-path SCIM shared bearer set arbitrary roleKeys/permissions; it can no
    longer carry BOSS access, and under enforce it is retired (410).
  * collision: /scim/v2/{project_id}/Users matched /scim/v2/Groups/Users (project "Groups").
  * production: changing a target's environment label to production needed only
    deployment_target:manage, so a non-production principal could relabel what the production gate
    reads; it needs deployment:production now.
  * GET /v1/mcp/servers demanded CSRF even when it only reads.
  * the identity peer is the socket that connected, never a forwarded value.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.routing import Match

from apps.api.app import identity_guard
from apps.api.app import main as api_main
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


@pytest.fixture
def owner(world):
    person = world.person("f2-misc-owner")
    world.bind(person, "owner")
    return person


SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


# ---- H5 -------------------------------------------------------------------------------------------

def test_the_api_refuses_a_system_member(client, world, owner):
    member = {"project_id": world.project_a, "member_id": f"m_{uuid.uuid4().hex[:8]}",
              "display_name": "Sneaky", "role_keys": ["ceo"], "permissions": ["*"]}
    with world.acting_as(world.ctx(owner)):
        system = client.post("/v1/members", json={**member, "member_type": "system"},
                             headers=SAME_ORIGIN)
        system_caps = client.post("/v1/members", json={**member, "member_type": " SYSTEM "},
                                  headers=SAME_ORIGIN)
        org = client.post("/v1/org-members", json={**member, "org_id": world.org,
                                                   "member_type": "system"}, headers=SAME_ORIGIN)
    assert system.status_code == 422, system.text
    assert system_caps.status_code == 422, system_caps.text
    assert org.status_code == 422, org.text
    with world.engine.connect() as db:
        rows = db.execute(text("SELECT count(*) FROM project_members WHERE member_id = :m"),
                          {"m": member["member_id"]}).scalar()
    assert rows == 0


def test_the_member_service_refuses_the_type_too(engine):
    from boss_engine.config import get_settings
    from boss_engine.services.members import MemberDirectoryService

    with engine.begin() as db, pytest.raises(ValueError, match="configuration only"):
        MemberDirectoryService(get_settings()).upsert_member(
            db, project_id="p", member_id="m", display_name="m", member_type="system",
            role_keys=[], permissions=[], actor="t")


# ---- M1 -------------------------------------------------------------------------------------------

def test_the_legacy_scim_payload_cannot_carry_roles_or_permissions():
    for converter, ext in ((api_main.scim_group_payload_to_service, api_main.BOSS_SCIM_GROUP_EXTENSION),
                           (api_main.scim_user_payload_to_service, api_main.BOSS_SCIM_USER_EXTENSION)):
        for field in ("roleKeys", "role_keys", "permissions"):
            with pytest.raises(api_main.ScimRolesRefused):
                converter({"displayName": "x", "userName": "x@example.com",
                           ext: {field: ["owner"]}})
        converter({"displayName": "x", "userName": "x@example.com"})   # people and groups still


def test_the_legacy_project_path_scim_is_retired_under_enforce(client, world, owner):
    with world.acting_as(world.ctx(owner)):
        out = client.post(f"/scim/v2/{world.project_a}/Groups",
                          json={"displayName": "admins",
                                api_main.BOSS_SCIM_GROUP_EXTENSION: {"roleKeys": ["ceo"]}})
    # the guard: sys_scim's scim:provision only; a person is refused before the handler, and a
    # SCIM principal reaches the handler, which answers 410 under enforce
    assert out.status_code in (403, 410), out.text


def test_the_scim_handler_answers_gone_under_enforce(monkeypatch):
    monkeypatch.setattr(identity_guard, "identity_enforced", lambda: True)
    answer = api_main.require_enterprise_scim_access(None, None, "p")
    assert answer is not None and answer.status_code == 410


def test_admin_scim_records_cannot_carry_access_under_enforce(client, world, owner):
    with world.acting_as(world.ctx(owner)):
        out = client.post("/v1/enterprise/scim/groups", json={
            "project_id": world.project_a, "display_name": "g", "role_keys": ["ceo"],
            "permissions": [], "actor": "x"}, headers=SAME_ORIGIN)
    assert out.status_code == 403, out.text
    assert out.json()["detail"]["reason"] == "scim_roles_retired"


# ---- the SCIM route collision -----------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/scim/v2/Groups/Users", "/scim/v2/Users/Groups"])
def test_the_identity_scim_paths_are_never_a_legacy_project(path):
    scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
    for route in app.router.routes:
        match, _ = route.matches(scope) if hasattr(route, "matches") else (Match.NONE, None)
        if match == Match.FULL:
            assert route.endpoint.__module__.endswith("identity_scim"), (path, route.path)
            return
    pytest.fail(f"nothing matched {path}")


def test_a_legacy_project_path_still_matches():
    scope = {"type": "http", "method": "GET", "path": "/scim/v2/boss-dogfood/Users", "root_path": ""}
    hits = [r for r in app.router.routes
            if hasattr(r, "matches") and r.matches(scope)[0] == Match.FULL]
    assert hits and hits[0].endpoint.__name__ == "scim_list_users"


# ---- production label ------------------------------------------------------------------------------

def test_relabelling_a_target_production_needs_deployment_production(client, world):
    manager = world.person("f2-target-manager")
    role = world.custom_role("target-manager", ("deployment_target:manage", "deployment:read"))
    world.bind(manager, role, project=world.project_a)
    target = f"dt_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                        "target_kind, target_ref, created_by) VALUES (:i, :p, 'app', 'r', 'staging', "
                        "'ssh', 'ref', 'seed')"), {"i": target, "p": world.project_a})
    with world.acting_as(world.ctx(manager)):
        promoted = client.patch(f"/v1/deployment-targets/{target}", json={"environment": "production"},
                                headers=SAME_ORIGIN)
        renamed = client.patch(f"/v1/deployment-targets/{target}", json={"app_name": "app2"},
                               headers=SAME_ORIGIN)
    assert promoted.status_code == 403, promoted.text
    assert promoted.json().get("permission") == "deployment:production", promoted.text
    assert renamed.status_code != 403, renamed.text
    with world.engine.connect() as db:
        env = db.execute(text("SELECT environment FROM deployment_targets WHERE id = :i"),
                         {"i": target}).scalar()
    assert env == "staging"


# ---- GET /v1/mcp/servers --------------------------------------------------------------------------

def test_reading_the_mcp_server_list_needs_no_csrf(client, world, owner):
    with world.acting_as(world.ctx(owner)):
        read = client.get("/v1/mcp/servers", params={"project_id": world.project_a})
        probe = client.get("/v1/mcp/servers", params={"project_id": world.project_a,
                                                      "check": "now"})
    assert read.status_code == 200, read.text
    assert probe.status_code == 403 and probe.json() == {"error": "csrf_failed"}, probe.text


# ---- the socket peer -------------------------------------------------------------------------------

def test_the_identity_peer_is_the_socket_not_a_forwarded_address():
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/v1/x", "headers": [],
             "client": ("10.1.2.3", 1), "boss.raw_peer": "203.0.113.9", "query_string": b""}
    creds = identity_guard._credentials(Request(scope))  # noqa: SLF001
    assert creds.peer_ip == "203.0.113.9"
    assert creds.client_ip == "10.1.2.3"
