"""Regression tests for BOSSAPP-1483 and BOSSAPP-1486.

Covers:
- BOSSAPP-1483:
  * Rolled-back project purge never deletes stored subscription pool logins; committed delete_project
    deletes them only after commit.
  * Moving an org pool off a deleted project prefers the org's model home project over older projects.
  * Add-account sign-ins are keyed by (actor, target_pool): another actor or pool gets 409 busy
    and cannot hijack or cancel (even with fresh=True) the first caller's add-account sign-in.
  * API org-level permission check on org-scoped subscription pool writes (403 for project-only,
    201/200 for org-level entitlement:manage).
- BOSSAPP-1486:
  * boss-sandbox-exec op_adopt sets run_dir to boss:<uid> 01770 (BOSS-owned, group-writable,
    sticky) so the sandbox uid cannot rename BOSS-created entries.
  * sandbox_identity.held_identity resolves run uid from st_gid (new boss:<uid>) and st_uid (legacy).
  * On Linux with CAP_CHOWN / root, verifies a two-uid sticky run_dir blocks rename by the run uid.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
_NEEDS_DB = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database",
)


@pytest.fixture(scope="module")
def eng():
    from boss_engine.config import Settings
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine, Settings())
    yield engine
    engine.dispose()


def _mk_org_with_projects(eng, prefix: str, count: int = 3):
    sfx = uuid4().hex[:8]
    org = f"org_{prefix}_{sfx}"
    ws = f"ws_{prefix}_{sfx}"
    projs = [f"proj_{prefix}_{i}_{sfx}" for i in range(count)]
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w)"),
                  {"w": ws, "o": org})
        for p in projs:
            c.execute(
                text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                {"p": p, "w": ws},
            )
    return {"org": org, "ws": ws, "projs": projs, "sfx": sfx}


# ── BOSSAPP-1483 ──────────────────────────────────────────────────────────────────────────────


@_NEEDS_DB
def test_1483_rollback_keeps_pool_logins_and_org_pool_prefers_model_home(eng, monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services import org_models, projects_delete, subscription_pools as sp

    w = _mk_org_with_projects(eng, "1483a", count=3)
    p_del, p_older, p_home = w["projs"]
    settings = Settings()

    deleted_keys: list[str] = []
    stored: dict[str, str] = {}

    class _MemStore:
        def put(self, k: str, v: str) -> None:
            stored[k] = v

        def get(self, k: str) -> str:
            return stored[k]

        def delete(self, k: str) -> None:
            deleted_keys.append(k)
            stored.pop(k, None)

    monkeypatch.setattr(sp, "_store", lambda _s: _MemStore())

    with eng.begin() as c:
        # Set p_home (the NEWEST project) as the org's model home.
        org_models.set_home(c, w["org"], p_home, "ceo")
        # Create an org pool anchored on p_del, and a project pool on p_del with one member.
        org_pool = sp.create_pool(
            c, p_del, engine="claude_code", name="Org pool", scope="org", actor="ceo",
            settings=settings,
        )
        proj_pool = sp.create_pool(
            c, p_del, engine="codex", name="Proj pool", scope="project", actor="ceo",
            settings=settings,
        )
        sp.add_member(
            c, settings, p_del, proj_pool["id"], label="seat-1",
            credential='{"tokens": {"access_token": "tok-proj-seat-1"}}', actor="ceo",
        )
    assert len(stored) >= 1

    # 1. Simulate a rolled-back purge: NO stored logins may be deleted before commit!
    with pytest.raises(RuntimeError, match="forced rollback"):
        with eng.begin() as c:
            res = projects_delete.purge(c, settings, p_del, actor="ceo")
            assert len(res["pending_pool_logins"]) >= 1
            raise RuntimeError("forced rollback")
    assert deleted_keys == [], "rolled-back purge must never delete stored logins"
    assert len(stored) >= 1

    # 2. Now run purge + post-commit deletion: org pool moves to p_home (model home, NOT p_older!)
    #    and project pool logins are deleted AFTER commit.
    with eng.begin() as c:
        done = projects_delete.purge(c, settings, p_del, actor="ceo")
        assert deleted_keys == []  # still not deleted before commit
    removed_n = sp.delete_purged_pool_logins(settings, done["pending_pool_logins"], project_id=p_del)
    assert removed_n >= 1
    assert len(deleted_keys) >= 1

    with eng.begin() as c:
        moved_to = c.execute(
            text("SELECT project_id FROM subscription_pools WHERE id = :i"),
            {"i": org_pool["id"]},
        ).scalar_one()
    assert moved_to == p_home, f"expected org pool to move to model home {p_home}, got {moved_to}"


def test_1483_add_account_sign_in_keyed_by_actor_and_target_pool():
    from boss_engine.services import subscription_sign_in as si

    si.reset_for_tests()
    argv = (sys.executable, "-c", "import time; print('https://example.com/login', flush=True); time.sleep(10)")
    try:
        s1 = si.start(
            "claude_code", project_id="p1", actor="alice", org_id="org1",
            add_account=True, target_pool="project:p1", argv=argv,
            signed_in=lambda: False,
        )
        # Different actor in same org/pool: 409 busy (NOT handed alice's session).
        with pytest.raises(si.SignInError) as exc1:
            si.start(
                "claude_code", project_id="p1", actor="bob", org_id="org1",
                add_account=True, target_pool="project:p1", argv=argv,
                signed_in=lambda: False,
            )
        assert exc1.value.status == 409

        # Different actor with fresh=True + add_account=True: STILL 409 (never cancels alice!).
        with pytest.raises(si.SignInError) as exc2:
            si.start(
                "claude_code", project_id="p1", actor="bob", org_id="org1",
                add_account=True, fresh=True, target_pool="project:p1", argv=argv,
                signed_in=lambda: False,
            )
        assert exc2.value.status == 409

        # Same actor targeting a DIFFERENT pool: 409 busy (never routes into wrong pool).
        with pytest.raises(si.SignInError) as exc3:
            si.start(
                "claude_code", project_id="p2", actor="alice", org_id="org1",
                add_account=True, target_pool="project:p2", argv=argv,
                signed_in=lambda: False,
            )
        assert exc3.value.status == 409

        # Same actor + same target pool reopens her own live session cleanly.
        same = si.start(
            "claude_code", project_id="p1", actor="alice", org_id="org1",
            add_account=True, target_pool="project:p1", argv=argv,
            signed_in=lambda: False,
        )
        assert same["sign_in_id"] == s1["sign_in_id"]
        assert si.status(s1["sign_in_id"])["state"] != "cancelled"
    finally:
        si.reset_for_tests()


@_NEEDS_DB
def test_1483_api_org_pool_write_requires_org_level_entitlement_manage(eng, monkeypatch):
    from apps.api.app.routers import identity_access
    from apps.api.app.routers.subscription_pools import build_subscription_pools_router
    from boss_engine.config import Settings
    from boss_engine.services import subscription_pools as sp

    w = _mk_org_with_projects(eng, "1483api", count=1)
    pid = w["projs"][0]
    settings = Settings()

    class _MemStore:
        def __init__(self):
            self.data: dict[str, str] = {}

        def put(self, k: str, v: str) -> None:
            self.data[k] = v

        def delete(self, k: str) -> None:
            self.data.pop(k, None)

    monkeypatch.setattr(sp, "_store", lambda _s: _MemStore())

    with eng.begin() as c:
        org_pool = sp.create_pool(
            c, pid, engine="claude_code", name="Org Pool", scope="org", actor="ceo",
            settings=settings,
        )

    allow_org = {"ok": False}

    def _fake_require_permission(_auth, _perm, _scope, *, label=""):
        if not allow_org["ok"]:
            raise HTTPException(status_code=403, detail=f"org permission required ({label})")

    monkeypatch.setattr(identity_access, "require_permission", _fake_require_permission)

    app = FastAPI()
    app.include_router(
        build_subscription_pools_router(
            engine=eng,
            settings=lambda: settings,
            require_reader=lambda _c, _p: None,
            require_write_actor=lambda _c, _p, claimed, _k: claimed or "dev",
        )
    )
    client = TestClient(app)

    # Without org-level entitlement:manage -> 403 on org pool write (status + add member).
    r1 = client.patch(
        f"/v1/subscription-pools/{org_pool['id']}",
        json={"project_id": pid, "status": "disabled", "actor": "project_admin"},
    )
    assert r1.status_code == 403
    r2 = client.post(
        f"/v1/subscription-pools/{org_pool['id']}/members",
        json={"project_id": pid, "label": "seat-2", "credential": "sk-test-2", "actor": "project_admin"},
    )
    assert r2.status_code == 403

    # With org-level entitlement:manage -> allowed!
    allow_org["ok"] = True
    r3 = client.post(
        f"/v1/subscription-pools/{org_pool['id']}/members",
        json={"project_id": pid, "label": "seat-2", "credential": "sk-test-2", "actor": "org_admin"},
    )
    assert r3.status_code == 201, r3.text
    r4 = client.patch(
        f"/v1/subscription-pools/{org_pool['id']}",
        json={"project_id": pid, "status": "disabled", "actor": "org_admin"},
    )
    assert r4.status_code == 200, r4.text


# ── BOSSAPP-1486 ──────────────────────────────────────────────────────────────────────────────


def test_1486_sandbox_exec_adopt_uses_boss_owned_sticky_group_writable_run_dir(tmp_path, monkeypatch):
    from boss_engine.services import sandbox_identity as si

    c_src = (
        Path(__file__).resolve().parents[2] / "scripts" / "sandbox" / "boss-sandbox-exec.c"
    ).read_text(encoding="utf-8")
    assert "fchown(fd, getuid(), uid)" in c_src
    assert "fchmod(fd, 01770)" in c_src
    assert "fchown(fd, uid, getgid())" not in c_src

    # held_identity resolves the leased uid from st_gid (new boss:<uid> 01770) as well as st_uid.
    run_root = tmp_path / "sandboxes"
    run_root.mkdir()
    key = si.owner_key("run-1486-test")
    run_dir = run_root / key
    run_dir.mkdir()
    monkeypatch.setattr(si, "run_root", lambda: run_root)
    monkeypatch.setattr(si, "SHIM_ROOT", str(tmp_path / "shim"))
    (tmp_path / "shim").mkdir()

    fake_uid = si.UID_BASE + 42
    real_lstat = os.lstat

    def _fake_lstat(path):
        st = real_lstat(path)
        if Path(path) == run_dir:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o1770,
                st_uid=os.geteuid(),
                st_gid=fake_uid,
            )
        return st

    monkeypatch.setattr(os, "lstat", _fake_lstat)
    held = si.held_identity("run-1486-test")
    assert held is not None
    assert held.uid == fake_uid


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="two-uid sticky rename race requires Linux root to drop to unprivileged uid",
)
def test_1486_linux_two_uid_sticky_run_dir_prevents_rename_of_boss_entries(tmp_path):
    run_dir = tmp_path / "run_1486"
    run_dir.mkdir(mode=0o700)
    boss_child = run_dir / "ws-boss-owned"
    boss_child.mkdir(mode=0o2777)
    sandbox_uid = 61055
    os.chown(run_dir, 0, sandbox_uid)
    os.chmod(run_dir, 0o1770)
    os.chown(boss_child, 0, sandbox_uid)

    # Unprivileged sandbox_uid can traverse run_dir and write inside boss_child,
    # but CANNOT rename or unlink boss_child in sticky boss-owned run_dir!
    proc = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import os, pathlib, sys; "
                f"os.setgid({sandbox_uid}); os.setuid({sandbox_uid}); "
                f"p = pathlib.Path({str(boss_child)!r}); "
                "(p / 'ok.txt').write_text('hi'); "
                "try:\n"
                f"    os.rename({str(boss_child)!r}, {str(run_dir / 'hijacked')!r})\n"
                "    sys.exit(2)\n"
                "except PermissionError:\n"
                "    sys.exit(0)\n"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert boss_child.exists() and not (run_dir / "hijacked").exists()
