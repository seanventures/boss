"""R2 #1: under enforce, the PDP decides — the legacy members table is never asked.

The legacy member check (`MemberDirectoryService.require_member` / `get_member` / `get_org_member`, which
read project_members / org_members) names none of the identity principals: not a person, not a
service account, not the bridge principals (sys_legacy_console for BOSS_API_KEY). A check that
still ran under enforce refused them all — a fresh install died on its last step because
install.sh's GET /v1/edition with BOSS_API_KEY answered 403 "Actor 'sys_legacy_console' is not an
active member of org", and every person was refused GET /v1/mcp-confirmations.

THE CLASS, three ways:
  * the installer's own probes, with the legacy console key, on a fresh (unclaimed) install;
  * a tripwire on the legacy check: every GET of the real app, called under enforce by the legacy
    console key and by an Owner's session, and NOT ONE legacy member check runs (outside the reviewed
    DATA lookups in LOOKUPS_NOT_CHECKS);
  * statically, every function that calls the legacy check either defers to the guard under
    enforce or is a reviewed legacy-only path (LEGACY_ONLY, each with its reason).
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_identity_fxd_every_get_is_read_only import GETS, _request
from boss_engine.services.members import MemberDirectoryService

ROOT = pathlib.Path(__file__).resolve().parents[3]
KEY = "legacy-console-key-r2-no-member-check-000000"
LEGACY_CALLS = ("require_member", "get_member", "get_org_member")

#: Handlers whose member read is a DATA lookup, not an access check (the route's own permission
#: was checked by the guard): "<function>": "why".
LOOKUPS_NOT_CHECKS = {
    "get_project_member": "GET /v1/members/{member_id} reads the named legacy member row "
                          "(user:read decided by the guard); it is the data, not the check",
}

#: Functions that call the legacy check and are unreachable under enforce: "<file>:<function>": why.
LEGACY_ONLY = {
    "apps/api/app/main.py:_require_human_mcp_member": "called only by _require_human_mcp_reviewer and "
        "the legacy transport's confirmation replay, both on the non-enforced branch",
    "apps/api/app/main.py:_require_human_mcp_reviewer": "every caller takes the identity branch "
        "(_mcp_reviewer_under_identity) when identity_enforced()",
    "apps/api/app/main.py:get_project_member": LOOKUPS_NOT_CHECKS["get_project_member"],
    "apps/api/app/main.py:create_break_glass_grant": "refused 422 legacy_roles_retired under "
        "enforce (_refuse_legacy_access_under_enforce) before it reads a member",
    "engine/boss_engine/services/members.py:*": "the legacy member service itself",
    "engine/boss_engine/services/saml.py:create_session": "the legacy enterprise SAML login "
        "authenticates a legacy member (no identity principal exists yet); identity SSO is "
        "identity.sso, which never reads members",
}
#: A caller that defers under enforce says so in its body.
DEFERS = ("identity_enforced()", "enforced", "_viewer.decide(", "_viewer.may(")


def _legacy_callers() -> list[tuple[str, str, str]]:
    out = []
    for base in ("apps/api/app", "engine/boss_engine"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            source = path.read_text(encoding="utf-8")
            for fn in ast.walk(ast.parse(source)):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr in LEGACY_CALLS + ("_require_human_mcp_member",)
                       or isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "_require_human_mcp_member"
                       for n in ast.walk(fn)):
                    out.append((rel, fn.name, ast.get_source_segment(source, fn) or ""))
    return out


def test_every_legacy_member_check_defers_to_the_guard_under_enforce():
    callers = _legacy_callers()
    assert len(callers) > 10
    wrong = [f"{rel}:{name}" for rel, name, body in callers
             if f"{rel}:{name}" not in LEGACY_ONLY and f"{rel}:*" not in LEGACY_ONLY
             and not any(d in body for d in DEFERS)]
    assert not wrong, ("these call the legacy member check with no enforce branch: route them "
                       f"through the PDP (identity.viewer.decide / may) or justify them: {wrong}")
    known = {f"{rel}:{name}" for rel, name, _ in callers} | {k for k in LEGACY_ONLY if k.endswith(":*")}
    stale = sorted(set(LEGACY_ONLY) - known)
    assert not stale, f"LEGACY_ONLY names functions that no longer call the check: {stale}"


@pytest.fixture
def legacy_console_key(monkeypatch):
    """BOSS_API_KEY as install.sh writes it (mapped to `ceo`), BOSS_AUTHZ_MODE=enforce."""
    from boss_engine.config import get_settings
    from boss_engine.identity import legacy, migration

    for settings in {id(s): s for s in (main.settings, get_settings())}.values():
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", KEY)
        monkeypatch.setattr(settings, "api_key_principals", f"{KEY}:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key", KEY)
        monkeypatch.setattr(settings, "authz_mode", "enforce")
        monkeypatch.setattr(settings, "authz_router_modes", "")
    legacy.invalidate_cache()
    migration.forget_seeded()
    yield {"Authorization": f"Bearer {KEY}"}
    legacy.invalidate_cache()
    migration.forget_seeded()


@pytest.fixture
def tripwire(monkeypatch):
    """Every legacy member check made while it is armed, with the handler that made it."""
    calls: list[str] = []

    def wrap(name):
        real = getattr(MemberDirectoryService, name)

        def spy(self, *args, **kwargs):
            frames = [f.function for f in inspect.stack()[1:12]]
            caller = next((f for f in frames if f not in LEGACY_CALLS + ("spy",) and not f.startswith("_get_org")),
                          "?")
            if caller not in LOOKUPS_NOT_CHECKS:
                calls.append(f"{name} from {caller}")
            return real(self, *args, **kwargs)
        monkeypatch.setattr(MemberDirectoryService, name, spy)

    for name in LEGACY_CALLS:
        wrap(name)
    return calls


def _project(engine) -> str:
    from sqlalchemy import text

    with engine.connect() as db:
        return db.execute(text("SELECT p.id FROM projects p ORDER BY p.created_at, p.id LIMIT 1")).scalar()


def test_the_installers_probes_answer_200_with_the_legacy_console_key_under_enforce(
        engine, legacy_console_key, tripwire):
    """install.sh's last checks: GET /v1/edition (die on non-200) and GET /v1/providers, with the
    key it wrote, on a fresh install — plus the reads the console makes first with that key."""
    project = _project(engine)
    client = TestClient(main.app, raise_server_exceptions=False)
    edition = client.get(f"/v1/edition?project_id={project}", headers=legacy_console_key)
    assert edition.status_code == 200, edition.text
    assert '"community"' in edition.text
    for path in ("/v1/providers", "/v1/roles", "/v1/role-skills", "/v1/code-tasks"):
        got = client.get(f"{path}?project_id={project}", headers=legacy_console_key)
        assert got.status_code == 200, (path, got.status_code, got.text[:200])
    assert not tripwire, tripwire


@pytest.fixture
def owner_world(engine):
    from sqlalchemy import text

    with engine.connect() as db:
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
    project = _project(engine)
    world = IdentityWorld(engine, org, "ws-unused", project, project)
    owner = world.person("r2-owner-no-legacy")
    world.bind(owner, "owner")
    return world, owner


def _sweep(engine, client, world, who, headers=None) -> list[str]:
    statuses = []
    for method, path, access in GETS:
        req = _request(engine, world, who, method, path, access)
        if headers:
            req["headers"] = {**req.get("headers", {}), **headers}
        statuses.append(client.request(**req).status_code)
    return statuses


def test_no_get_runs_a_legacy_member_check_for_an_owner_under_enforce(engine, owner_world, tripwire):
    world, owner = owner_world
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(owner)):
        statuses = _sweep(engine, client, world, owner)
    assert len(statuses) > 500
    assert not tripwire, sorted(set(tripwire))


def test_no_get_runs_a_legacy_member_check_for_the_legacy_console_key_under_enforce(
        engine, owner_world, legacy_console_key, tripwire):
    world, owner = owner_world
    client = TestClient(main.app, raise_server_exceptions=False)
    statuses = _sweep(engine, client, world, owner, headers=legacy_console_key)
    assert len(statuses) > 500
    assert not tripwire, sorted(set(tripwire))


def test_every_person_may_list_mcp_confirmations_under_enforce(engine, owner_world):
    """FX-D's sweep: GET /v1/mcp-confirmations refused every person (even the Owner) with the
    legacy org-member check."""
    world, owner = owner_world
    viewer = world.person("r2-viewer-mcp")
    world.bind(viewer, "viewer")
    client = TestClient(main.app, raise_server_exceptions=False)
    for who in (owner, viewer):
        with world.acting_as(world.ctx(who)):
            got = client.get(f"/v1/mcp-confirmations?project_id={world.project_a}")
        assert got.status_code == 200, (who.id, got.status_code, got.text[:200])
        assert "items" in got.json()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_dual_control_under_enforce_takes_two_people_holding_the_approver_roles(
        client, engine, monkeypatch):
    """approve_changeset's dual control asked the legacy members table for the approver role keys
    (aws.ec2.instance.create needs [ceo, cto]): under enforce it refused everyone. Now the roles
    come from the PDP's binding paths plus the seat the claimant took over (`ceo`); each person
    fills ONE role, so it still takes two different people."""
    import dataclasses
    import uuid as _uuid

    from sqlalchemy import text as _text

    from apps.api.tests.test_aws_changeset_flow import (
        EC2_RUN_INSTANCES_RESPONSE,
        INFRA_RESERVATION_USD_MICROS,
        FakeAwsClient,
        create_aws_provider_account,
        install_fake_aws,
        reserve_for_infra_provision,
    )
    from boss_engine.identity import authz

    install_fake_aws(monkeypatch, {"ec2": FakeAwsClient({"run_instances": EC2_RUN_INSTANCES_RESPONSE})})
    provider = create_aws_provider_account(client, "r2-dual-control")
    reservation_id = reserve_for_infra_provision(client)
    created = client.post("/v1/changesets", json={
        "project_id": "boss-dogfood", "provider_account_id": provider["id"],
        "title": "r2 dual control", "summary": "dual control under enforce",
        "expected_cost_usd_micros": INFRA_RESERVATION_USD_MICROS, "reservation_id": reservation_id,
        "rollback_plan": ["Terminate it."],
        "operations": [{"skill_id": "aws.ec2.instance.create", "action": "create",
                        "resource_type": "ec2_instance", "resource_ref": "r2-dual",
                        "desired_state": {"image_id": "ami-1", "instance_type": "t3.micro",
                                          "name": "r2-dual", "environment": "preview"},
                        "risk_tier": 3}],
        "actor": "ceo"})
    assert created.status_code == 201, created.text
    cs = created.json()["id"]

    with engine.connect() as db:
        org = db.execute(_text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                               "WHERE p.id = 'boss-dogfood'")).scalar()
    world = IdentityWorld(engine, org, "ws-unused", "boss-dogfood", "boss-dogfood")
    claimant = dataclasses.replace(world.person("r2-claimant"), legacy_member_ids=("ceo",))
    world.bind(claimant, "owner")
    stranger = world.person("r2-admin-no-approver-role")
    world.bind(stranger, "admin")
    cto = world.person("r2-cto")
    role_id = f"arl_r2cto{_uuid.uuid4().hex[:6]}"
    with engine.begin() as db:
        db.execute(_text("DELETE FROM identity_access_roles WHERE org_id = :o AND key = 'cto'"), {"o": org})
        db.execute(_text("INSERT INTO identity_access_roles (id, org_id, key, name, builtin, bindable_scopes) "
                         "VALUES (:id, :o, 'cto', 'CTO', false, ARRAY['org','project'])"), {"id": role_id, "o": org})
        for perm in ("changeset:approve", "infra:read", "project:read"):
            db.execute(_text("INSERT INTO identity_access_role_permissions (role_id, permission_key) "
                             "VALUES (:r, :p)"), {"r": role_id, "p": perm})
        authz.bump_epoch(org, db=db, reason="test.r2.cto")
    world.bind(cto, role_id)

    api = TestClient(main.app, raise_server_exceptions=False)

    def approve(who):
        with world.acting_as(world.ctx(who)):
            return api.post(f"/v1/changesets/{cs}/approve", json={"actor": "ceo"})

    refused = approve(stranger)
    assert refused.status_code == 403 and "approver_role_required" in refused.text, refused.text
    first = approve(claimant)
    assert first.status_code == 200 and first.json()["status"] == "pending_approval", first.text
    again = approve(claimant)
    assert again.status_code == 422, again.text          # the same person cannot fill the second role
    second = approve(cto)
    assert second.status_code == 200 and second.json()["status"] == "approved", second.text


def test_a_person_is_an_active_user_principal_not_a_legacy_member(engine, owner_world):
    """agent_identity.is_person decided "is this approver a person?" (host deploy approvals) from
    project_members only: under enforce no identity person is there, so nobody but the claimant
    could approve a host deployment."""
    from boss_engine.identity.principal import PrincipalKind, PrincipalStatus
    from boss_engine.services.agent_identity import is_person

    world, owner = owner_world
    gone = world.person("r2-disabled", status=PrincipalStatus.DISABLED)
    robot = world.principal("r2-robot", kind=PrincipalKind.SERVICE_ACCOUNT)
    with engine.begin() as db:
        verdicts = {who.id: is_person(who.id, db, world.project_a) for who in (owner, gone, robot)}
    assert verdicts == {owner.id: True, gone.id: False, robot.id: False}
