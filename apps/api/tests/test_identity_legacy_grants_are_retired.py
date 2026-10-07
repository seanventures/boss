"""After the claim, the legacy member tables are not a grant path (IDENTITY.md §6.11, §12; FX-B P0-6).

Two halves of one hole:
  * POST /v1/members, /v1/org-members and /v1/break-glass/grants still wrote roles and capabilities
    under enforce, and
  * the per-boot identity seed re-imported every legacy member's capabilities as role bindings,
    with none of §6.11's limits — so an Admin could hand a taken-over placeholder an Owner-only
    capability (enterprise_manage -> identity_provider:manage) that landed at the next boot.

Now the routes refuse access-carrying bodies under enforce (422 legacy_roles_retired), and the
seed imports legacy access only up to and including the claim.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _admin(world):
    admin = world.person("legacy-admin")
    world.bind(admin, "admin")
    return admin


@pytest.mark.parametrize("url,body", [
    ("/v1/members", lambda w: {"project_id": w.project_a, "member_id": "eve", "display_name": "Eve",
                               "member_type": "human", "role_keys": ["ceo"], "permissions": ["task_create"]}),
    ("/v1/members", lambda w: {"project_id": w.project_a, "member_id": "eve", "display_name": "Eve",
                               "member_type": "human", "role_keys": ["viewer"],
                               "permissions": ["enterprise_manage"]}),
    ("/v1/org-members", lambda w: {"org_id": w.org, "project_id": w.project_a, "member_id": "eve",
                                   "display_name": "Eve", "member_type": "human",
                                   "role_keys": ["admin"], "permissions": ["member_manage"]}),
    ("/v1/break-glass/grants", lambda w: {"project_id": w.project_a, "target_member_id": "eve",
                                          "reason": "outage", "scope": {"kind": "project"},
                                          "granted_permissions": ["deploy"], "ttl_minutes": 30}),
])
def test_under_enforce_a_legacy_record_cannot_carry_access(world, url, body):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(_admin(world))):
        out = client.post(url, json=body(world))
    assert out.status_code == 422 and "legacy_roles_retired" in out.text, (url, out.status_code, out.text)


def _legacy_member(world, member_id: str, capabilities: list[str]) -> None:
    with world.engine.begin() as db:
        db.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, role_keys, "
            "permissions, status, created_by) VALUES (:id, :p, :m, :m, 'human', '[]'::jsonb, "
            "CAST(:perms AS jsonb), 'active', 'test')"),
            {"id": f"pm_{uuid.uuid4().hex[:10]}", "p": world.project_a, "m": member_id,
             "perms": json.dumps(capabilities)})


def _placeholder_bindings(world, member_id: str) -> list[str]:
    from boss_engine.identity import migration

    with world.engine.begin() as db:
        found = migration.mapping(db, world.org, migration.LegacyKind.MEMBER_ID, member_id)
        if not found:
            return []
        return [r[0] for r in db.execute(text(
            "SELECT role_id FROM identity_role_bindings WHERE subject_type = 'principal' "
            "AND subject_id = :p"), {"p": found["principal_id"]}).all()]


def _seed(world):
    from boss_engine.identity import migration

    with world.engine.begin() as db:
        return migration.seed(db, org_id=world.org)


def test_before_the_claim_the_seed_maps_legacy_access(world):
    """The control: the migration still does its job on an unclaimed install."""
    member = f"pre{uuid.uuid4().hex[:6]}"
    _legacy_member(world, member, ["enterprise_manage"])
    _seed(world)
    assert _placeholder_bindings(world, member), "an unclaimed install's members must be mapped"


def test_after_the_claim_the_seed_imports_no_legacy_access(world):
    world.claimed()
    member = f"post{uuid.uuid4().hex[:6]}"
    _legacy_member(world, member, ["enterprise_manage", "member_manage"])
    report = _seed(world)
    assert _placeholder_bindings(world, member) == [], "a post-claim legacy row became a grant"
    assert any("not imported" in note for note in report.notes)


def test_the_claims_own_seed_still_maps_what_exists_at_the_claim(world):
    from boss_engine.identity import migration

    world.claimed()
    member = f"atc{uuid.uuid4().hex[:6]}"
    _legacy_member(world, member, ["task_create"])
    with world.engine.begin() as db:
        migration.seed(db, org_id=world.org, apply_claimed=False, at_claim=True)
    assert _placeholder_bindings(world, member)
