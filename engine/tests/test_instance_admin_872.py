"""BOSSAPP-872: an instance admin runs the install apart from any org's data.

Until the first one is named, org Owner/Admin hold instance:* through the catalogue (nothing
changes for an install that never names one). Once one is named, ONLY instance admins hold
instance:* — and an instance admin needs no org role, so holds nothing else through it.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.identity import authz, instance_admin
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity.scope import ResolvedScope

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def world(eng):
    sfx = uuid4().hex[:8]
    org = f"org_872_{sfx}"
    w = {"org": org, "owner": f"usr_own_{sfx}", "ops": f"usr_ops_{sfx}", "other": f"usr_oth_{sfx}",
         "agent": f"agt_872_{sfx}"}
    with eng.begin() as c:
        # Only this test's admins count: start from an install with none.
        c.execute(text("UPDATE identity_instance_admins SET ended_at = now(), ended_by = 'test' "
                       "WHERE ended_at IS NULL"))
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": org})
        for pid, kind in ((w["owner"], "user"), (w["ops"], "user"), (w["other"], "user"),
                          (w["agent"], "agent")):
            c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                           "VALUES (:i, :o, :k, 'active', :i)"), {"i": pid, "o": org, "k": kind})
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, "
                       "role_id, scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', :s, "
                       "'arl_owner', 'org', :o, 'test')"),
                  {"id": f"bnd_872_{sfx}", "o": org, "s": w["owner"]})
    authz.HELD_CACHE.clear()
    yield w
    with eng.begin() as c:
        c.execute(text("UPDATE identity_instance_admins SET ended_at = now(), ended_by = 'test' "
                       "WHERE ended_at IS NULL"))
    authz.HELD_CACHE.clear()


def _ctx(w, who):
    return AuthContext(Principal(w[who], PrincipalKind.USER, w["org"], PrincipalStatus.ACTIVE, who),
                       AuthMethod.SESSION)


def _holds(eng, w, who) -> frozenset[str]:
    with eng.connect() as c:
        return authz.effective_permissions(_ctx(w, who), ResolvedScope.org_level(w["org"]),
                                           db=c).permissions


def _may_read(eng, w, who) -> bool:
    with eng.connect() as c:
        return authz.check(_ctx(w, who), "instance:read", ResolvedScope.org_level(w["org"]),
                           db=c).allowed


def test_before_any_instance_admin_the_org_owner_runs_the_install(eng, world):
    assert {"instance:read", "instance:manage"} <= _holds(eng, world, "owner")
    assert _may_read(eng, world, "owner")
    assert not _may_read(eng, world, "ops")


def test_naming_one_moves_instance_powers_off_org_roles(eng, world):
    with eng.begin() as c:
        instance_admin.grant(c, world["ops"], actor=world["owner"])
    ops = _holds(eng, world, "ops")
    assert {"instance:read", "instance:manage"} <= ops
    assert ops == {"instance:read", "instance:manage"}           # no org data comes with it
    assert _may_read(eng, world, "ops")
    owner = _holds(eng, world, "owner")
    assert not owner & {"instance:read", "instance:manage"}
    assert "project:read" in owner                               # the org role is otherwise intact
    assert not _may_read(eng, world, "owner")
    with eng.connect() as c:
        why = authz.effective_permissions(_ctx(world, "ops"), ResolvedScope.org_level(world["org"]),
                                          db=c, with_why=True).why["instance:read"]
    assert why[0].role_key == "instance_admin"


def test_the_last_instance_admin_cannot_be_removed(eng, world):
    with eng.begin() as c:
        instance_admin.grant(c, world["ops"], actor=world["owner"])
    with pytest.raises(instance_admin.InstanceAdminError, match="last instance admin"), eng.begin() as c:
        instance_admin.revoke(c, world["ops"], actor=world["ops"])
    with eng.begin() as c:
        instance_admin.grant(c, world["other"], actor=world["ops"])
        instance_admin.revoke(c, world["ops"], actor=world["other"])
        names = [a["principal_id"] for a in instance_admin.list_admins(c)]
    assert names == [world["other"]]
    assert not _may_read(eng, world, "ops")


def test_only_an_active_person_can_be_one(eng, world):
    with pytest.raises(instance_admin.InvalidPrincipal), eng.begin() as c:
        instance_admin.grant(c, world["agent"], actor=world["owner"])
    with pytest.raises(instance_admin.InvalidPrincipal), eng.begin() as c:
        instance_admin.grant(c, "usr_nobody", actor=world["owner"])


def test_the_backups_list_is_an_instance_read(eng, world):
    with eng.begin() as c:
        c.execute(text("INSERT INTO backups (id, status, path) VALUES (:i, 'verified', '/srv/x.dump')"),
                  {"i": f"bk_{uuid4().hex[:8]}"})
        rows = instance_admin.list_backups(c, limit=5)
    assert rows and "path" not in rows[0]                        # a host path is not shown
