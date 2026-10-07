"""F2 authorization fixes: the legacy principals, the mode, and the system member type.

H2  enforcement is the default (config and compose); shadow is explicit and posture shows it red.
H3  the worker's legacy key holds exactly what the worker calls (no identity writes, no
    owner:assign); every other sys_legacy_* loses identity writes at the claim and everything at
    the cutoff; an unconfigured sys_legacy_* holds nothing.
H5  which members are SYSTEM comes from configuration: a member row typed "system" never becomes
    an Owner-equivalent system principal.
LOW require_production fails closed with no decided mode; lineage reads the one mode source.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import Settings, get_settings
from boss_engine.identity import legacy, lineage, migration, posture
from boss_engine.identity.declarations import Permit
from boss_engine.identity.permissions_gen import PERMISSION_META

ROOT = Path(__file__).resolve().parents[3]
IDENTITY_WRITES = {k for k, m in PERMISSION_META.items()
                   if m.category == migration.IDENTITY_CATEGORY
                   and not (k.endswith(":read") or k.endswith(":read_all"))}


def _worker_route_permissions() -> dict[str, set[str]]:
    """permission -> the worker's routes that need it, from the worker's own source and the
    app's declarations (POST/GET as the worker calls them)."""
    src = "".join(p.read_text() for p in (ROOT / "apps/worker/boss_worker").glob("*.py"))
    called = set(re.findall(r'f?"(/v1/[^"]*)"', src))
    routes = [(e.methods, e.path, e.access) for e in identity_guard.route_inventory(app)
              if e.kind == "http" and isinstance(e.access, Permit)]
    out: dict[str, set[str]] = {}
    for path in called:
        norm = re.sub(r"\{[^}]*\}", "X", path)
        for methods, template, access in routes:
            if "POST" not in methods and template != "/v1/system/health":
                continue          # the worker POSTs everything but the health probe (GET)
            if re.fullmatch(re.sub(r"\{[^}]*\}", "[^/]+", template), norm):
                out.setdefault(access.permission, set()).add(template)
    return out


def test_h3_the_worker_key_holds_exactly_what_the_worker_calls():
    needed = _worker_route_permissions()
    assert needed, "found no worker routes"
    held = legacy.system_permissions(legacy.LEGACY_WORKER_PRINCIPAL)
    assert held == migration.WORKER_LEGACY_SET
    assert set(needed) <= held, sorted(set(needed) - held)                    # covers the worker
    unused = migration.WORKER_API_PERMISSIONS - set(needed)
    assert not unused, f"worker set holds permissions no worker route needs: {sorted(unused)}"
    assert not held & IDENTITY_WRITES, sorted(held & IDENTITY_WRITES)
    assert "owner:assign" not in held
    assert len(held) < 25                                                     # never Owner-equivalent


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _place(world, principal_id: str) -> None:
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "(:p, :o, 'system', 'active', :p) ON CONFLICT (id) DO UPDATE SET org_id = :o"),
                   {"p": principal_id, "o": world.org})


def test_h3_other_legacy_system_principals_narrow_at_the_claim_and_end_at_the_cutoff(world, monkeypatch):
    pid = "sys_legacy_metatron"
    _place(world, pid)
    with world.engine.connect() as db:
        before = legacy.system_permissions(pid, db=db)
    assert before == migration.LEGACY_OWNER_SET                               # pre-claim: today's reach
    world.claimed()
    with world.engine.connect() as db:
        after = legacy.system_permissions(pid, db=db)
    assert after == migration.CONSOLE_AFTER_CLAIM_SET and not after & IDENTITY_WRITES
    from dataclasses import replace
    from boss_engine.identity import policy

    real = policy.get_policy
    monkeypatch.setattr(policy, "get_policy",                                 # the cutoff has passed
                        lambda org, db=None: replace(real(org, db=db), legacy_cutoff_days=0))
    with world.engine.connect() as db:
        assert legacy.system_permissions(pid, db=db) == frozenset()


def test_h3_h5_an_unconfigured_legacy_system_principal_holds_nothing():
    assert migration.legacy_system_set("sys_legacy_mallory") == frozenset()
    assert legacy.system_permissions("sys_legacy_mallory") == frozenset()


def test_h5_a_member_row_typed_system_is_never_mapped_to_a_system_principal(world):
    member = f"mallory{uuid.uuid4().hex[:6]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
                        "permissions) VALUES (:id, :p, :m, 'Mallory', 'system', '[\"*\"]')"),
                   {"id": uuid.uuid4().hex, "p": world.project_a, "m": member})
        report = migration.seed(db, org_id=world.org)
    assert member not in report.system_members
    with world.engine.connect() as db:
        mapped = migration.mapping(db, world.org, legacy.LegacyKind.MEMBER_ID, member)
        assert mapped is not None and mapped["principal_id"].startswith("usr_")
        assert db.execute(text("SELECT 1 FROM identity_principals WHERE id = :p"),
                          {"p": migration.system_member_principal(member)}).first() is None
    assert migration.is_configured_system_member("metatron")
    assert migration.is_configured_system_member(get_settings().worker_principal)
    assert not migration.is_configured_system_member(member)


def test_h2_enforce_is_the_default_in_config_and_compose(monkeypatch):
    monkeypatch.delenv("BOSS_AUTHZ_MODE", raising=False)
    assert Settings(_env_file=None).authz_mode == "enforce"
    compose = (ROOT / "scripts/install/community-compose.yml").read_text()
    modes = re.findall(r"BOSS_AUTHZ_MODE: \$\{BOSS_AUTHZ_MODE:-(\w+)\}", compose)
    assert modes and set(modes) == {"enforce"}


def test_h2_posture_shows_anything_but_enforce_red():
    class S:
        authz_mode, authz_router_modes = "enforce", ""
    assert posture.enforcement_check(S)["status"] == "ok"
    S.authz_mode = "shadow"
    assert posture.enforcement_check(S)["status"] == "critical"
    S.authz_mode, S.authz_router_modes = "enforce", "github=shadow"
    check = posture.enforcement_check(S)
    assert check["status"] == "critical" and check["router_modes"] == {"github": "shadow"}


def test_require_production_fails_closed_when_no_mode_was_decided(monkeypatch):
    from apps.api.app.routers.identity_access import require_production

    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    token_mode = identity_guard.IDENTITY_MODE.set("")
    token_ctx = identity_guard.REQUEST_IDENTITY.set(None)
    try:
        with pytest.raises(HTTPException) as exc:
            require_production(None, "any-project", "production")
        assert exc.value.status_code == 401
        require_production(None, "any-project", "staging")                   # not production: nothing
    finally:
        identity_guard.IDENTITY_MODE.reset(token_mode)
        identity_guard.REQUEST_IDENTITY.reset(token_ctx)


def test_lineage_reads_the_same_mode_as_the_guard(monkeypatch):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "shadow")                          # the env says one thing,
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")              # the settings another
    assert lineage._enforced() is True                                        # noqa: SLF001
    monkeypatch.setattr(get_settings(), "authz_mode", "shadow")
    assert lineage._enforced() is False                                       # noqa: SLF001
