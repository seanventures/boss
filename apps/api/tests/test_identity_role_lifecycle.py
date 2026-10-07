"""Access-role lifecycle rules that had no test or no API (IDENTITY.md §6.9-§6.11; FX-B, part 3).

  * Deleting a role still bound is 409 role_in_use; POST …/reassign moves every binding (each
    under §6.11) and deletes the emptied role, all or nothing.
  * Owner and Auditor (org-only) bound at a project are refused, through every binding route.
  * The impact preview counts pages, API operations and MCP tools gained or lost, and the page
    table it reads matches the console's own declarations.
  * Raising an agent's Passport ceiling needs the caller to hold the new permissions (§6.11).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

ROOT = Path(__file__).resolve().parents[3]
H = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


@pytest.fixture
def admin(world):
    a = world.person("role-admin")
    world.bind(a, "admin")
    return a


def _client():
    return TestClient(app, raise_server_exceptions=False)


def _bindings_of(world, role_id):
    with world.engine.connect() as db:
        return db.execute(text("SELECT subject_id, scope_type, scope_id FROM identity_role_bindings "
                               "WHERE role_id = :r ORDER BY subject_id"), {"r": role_id}).all()


def test_a_bound_role_is_not_deleted_and_reassign_moves_everyone_then_deletes(world, admin):
    old = world.custom_role("old-builders", ("project:read", "code_task:read", "code_task:create"))
    new = world.custom_role("new-builders", ("project:read", "code_task:read", "code_task:create",
                                             "code_task:steer"))
    p1, p2 = world.person("b1"), world.person("b2")
    group = world.group("builders", members=(p2,))
    world.bind(p1, old, project=world.project_a)
    world.bind(group, old, subject_type="group")
    client = _client()
    with world.acting_as(world.ctx(admin)):
        refused = client.delete(f"/v1/identity/access-roles/{old}", headers=H)
        assert refused.status_code == 409 and refused.json()["error"] == "role_in_use"
        assert refused.json()["bindings"] == 2
        moved = client.post(f"/v1/identity/access-roles/{old}/reassign",
                            json={"to_role_id": new, "delete": True}, headers=H)
    assert moved.status_code == 200, moved.text
    assert moved.json()["moved"] == 2 and moved.json()["deleted"] is True
    assert sorted((r[0], r[1]) for r in _bindings_of(world, new)) == sorted(
        [(p1.id, "project"), (group, "org")])
    with world.engine.connect() as db:
        assert db.execute(text("SELECT 1 FROM identity_access_roles WHERE id = :r"), {"r": old}).first() is None


def test_reassign_is_all_or_nothing_and_never_raises_the_caller(world, admin):
    """A move the caller may not make (to a role holding what they lack) changes nothing at all."""
    old = world.custom_role("plain", ("project:read",))
    owner_only = world.custom_role("owner-only", ("project:read", "identity_provider:manage"))
    p1, p2 = world.person("c1"), world.person("c2")
    world.bind(p1, old)
    world.bind(p2, old)
    client = _client()
    with world.acting_as(world.ctx(admin)):
        out = client.post(f"/v1/identity/access-roles/{old}/reassign",
                          json={"to_role_id": owner_only, "delete": True}, headers=H)
    assert out.status_code == 403, out.text
    assert len(_bindings_of(world, old)) == 2 and _bindings_of(world, owner_only) == []


def test_nobody_reassigns_their_own_binding(world, admin):
    old = world.custom_role("mine", ("project:read",))
    new = world.custom_role("mine2", ("project:read",))
    world.bind(admin, old)
    with world.acting_as(world.ctx(admin)):
        out = _client().post(f"/v1/identity/access-roles/{old}/reassign",
                             json={"to_role_id": new, "delete": False}, headers=H)
    assert out.status_code == 403 and "self_binding" in out.text, out.text


def test_reassign_to_an_org_only_role_refuses_a_project_binding(world, admin):
    old = world.custom_role("proj", ("project:read",))
    p = world.person("d1")
    world.bind(p, old, project=world.project_a)
    with world.acting_as(world.ctx(admin)):
        out = _client().post(f"/v1/identity/access-roles/{old}/reassign",
                             json={"to_role_id": "arl_auditor", "delete": False}, headers=H)
    assert out.status_code == 422 and out.json()["error"] == "scope_not_bindable", out.text


@pytest.mark.parametrize("role", ["arl_owner", "arl_auditor"])
def test_owner_and_auditor_are_never_bound_at_a_project(world, role):
    owner = world.person("proj-owner")
    world.bind(owner, "owner")
    target = world.person("someone")
    body = {"subject_type": "principal", "subject_id": target.id, "role_id": role}
    client = _client()
    with world.acting_as(world.ctx(owner)):
        out = client.post(f"/v1/identity/projects/{world.project_a}/role-bindings", json=body, headers=H)
    assert out.status_code == 422 and out.json()["error"] == "scope_not_bindable", out.text
    with world.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE subject_id = :s"),
                          {"s": target.id}).scalar() == 0


def test_a_role_of_only_org_permissions_cannot_be_made_project_bindable(world, admin):
    client = _client()
    with world.acting_as(world.ctx(admin)):
        out = client.post("/v1/identity/access-roles", json={
            "key": "org_only_x", "name": "Org only", "permissions": ["user:read"],
            "bindable_scopes": ["org", "project"]}, headers=H)
    assert out.status_code == 422 and out.json()["error"] == "org_only_role", out.text


# ---- impact preview ---------------------------------------------------------------------------------

def test_the_impact_preview_counts_pages_routes_and_tools(world, admin):
    role = world.custom_role("viewerish", ("project:read",))
    p = world.person("e1")
    world.bind(p, role)
    with world.acting_as(world.ctx(admin)):
        out = _client().post(f"/v1/identity/access-roles/{role}/preview",
                             json={"permissions": ["project:read", "audit:read", "code_task:read"]}, headers=H)
    assert out.status_code == 200, out.text
    body = out.json()
    assert "Audit" in body["pages"]["gained"] and body["pages"]["lost"] == []
    assert body["routes"]["gained"] > 0 and body["routes"]["lost"] == 0
    assert body["tools"]["gained"] > 0
    assert body["counts"]["people"] == 1
    with world.acting_as(world.ctx(admin)):
        less = _client().post(f"/v1/identity/access-roles/{role}/preview",
                              json={"permissions": []}, headers=H).json()
    assert less["routes"]["lost"] > 0 and less["routes"]["gained"] == 0


def _console_pages_from_typescript():
    pages = (ROOT / "apps/web/lib/identity/pages.ts").read_text()
    areas = (ROOT / "apps/web/lib/identity/areas.ts").read_text()
    start = areas.index("export const AREA_ACCESS")
    block = areas[start:areas.index("};", start)]
    amap, legacy = {}, set()
    for m in re.finditer(r'^\s+"?([\w-]+)"?: p\("([^"]+)"\),([^\n]*)$', block, re.M):
        amap[m.group(1)] = m.group(2)
        if "legacy id" in m.group(3):
            legacy.add(m.group(1))
    rows = []
    for m in re.finditer(r'\{ path: "([^"]+)", label: "([^"]+)", tip: "[^"]*", access: '
                         r'(\{ permission: "([^"]+)" \}|\{ anyScope: "([^"]+)" \}|\{ authenticated: true \}'
                         r'|area\("([^"]+)"\))', pages):
        permission = m.group(4) or m.group(5) or (amap.get(m.group(6)) if m.group(6) else None)
        if permission:
            rows.append((m.group(1), permission))
    rows += [(f"area:{k}", v) for k, v in amap.items() if k not in legacy]
    return rows


def test_the_page_table_matches_the_consoles_declarations():
    from boss_engine.identity.console_pages import CONSOLE_PAGES

    ours = [(path, permission) for path, _label, permission in CONSOLE_PAGES]
    theirs = _console_pages_from_typescript()
    assert len(theirs) > 30, "the TypeScript scan found too few pages: it is broken"
    assert ours == theirs, "regenerate engine/boss_engine/identity/console_pages.py from pages.ts/areas.ts"


# ---- Passport ceiling: §6.11 "the caller holds the new ceiling" ----------------------------------------

def test_raising_an_agents_ceiling_needs_the_caller_to_hold_it(world):
    admin = world.person("ceiling-admin")
    world.bind(admin, world.custom_role("ceiling-mgr", ("agent_passport:manage", "agent_passport:read",
                                                       "project:read", "code_task:read")))
    agent = world.agent("ceiling-agent", ceiling=("code_task:read",), sponsor=admin)
    client = _client()
    with world.acting_as(world.ctx(admin)):
        raised = client.patch(f"/v1/identity/agents/{agent.id}/passport",
                              json={"ceiling": ["code_task:read", "code_task:create"]}, headers=H)
        kept = client.patch(f"/v1/identity/agents/{agent.id}/passport",
                            json={"ceiling": ["code_task:read", "project:read"]}, headers=H)
        lowered = client.patch(f"/v1/identity/agents/{agent.id}/passport", json={"ceiling": []}, headers=H)
    assert raised.status_code == 403 and raised.json().get("permission") == "code_task:create", raised.text
    assert kept.status_code == 200, kept.text
    assert lowered.status_code == 200, lowered.text
