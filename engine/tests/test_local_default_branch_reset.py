"""BOSSAPP-1439: detect default branch rewind or non-ancestor force-move in BOSS's bare repo."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import local_git_hooks, local_repo

from ._obs_seed import pg_engine


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)

    eng = pg_engine()
    conn = eng.connect()
    tx = conn.begin()
    org = f"org_{uuid.uuid4().hex[:8]}"
    pid = f"proj_{uuid.uuid4().hex[:8]}"
    conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
    conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :o)"),
                 {"w": f"ws_{org}", "o": org})
    conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                 {"p": pid, "w": f"ws_{org}"})

    described = local_repo.create(pid, "app", settings=Settings())
    conn.execute(
        text("INSERT INTO github_repository_bindings "
             "(id, project_id, owner, name, full_name, role, status, host, default_branch, created_by) "
             "VALUES (:i, :p, :o, :n, :f, 'primary', 'active', 'local', 'main', 'test')"),
        {"i": f"ghb_{pid}", "p": pid, "o": described["owner"], "n": described["repo"],
         "f": described["full_name"]})
    yield conn, pid, described
    tx.rollback()
    conn.close()


def test_reconcile_detects_default_branch_rewind_and_dedupes_event(world):
    conn, pid, described = world
    bare = local_repo.bare_path(described["owner"], described["repo"])
    sha_a = described["head_sha"]
    sha_b = local_repo.commit_files(bare, "main", {"app.py": "v2\n"}, "second commit")
    assert local_repo.recorded_default_sha(bare) == sha_b

    # Direct rewind of default branch inside BOSS's bare repo.
    local_repo.git(["update-ref", "refs/heads/main", sha_a], git_dir=bare)
    assert local_repo.branch_sha(bare, "main") == sha_a

    out1 = local_git_hooks.reconcile(conn, Settings(), pid)
    out2 = local_git_hooks.reconcile(conn, Settings(), pid)
    ev_rows = conn.execute(
        text("SELECT event_type, data FROM events WHERE project_id = :p "
             "AND event_type = 'local_repo.default_branch_moved'"),
        {"p": pid}).mappings().all()

    assert len(out1) == 1
    item = out1[0]
    assert item["kind"] == "rewound"
    assert item["default_branch_anomaly"]["kind"] == "rewound"
    assert item["default_branch_anomaly"]["previous_sha"] == sha_b
    assert item["default_branch_anomaly"]["current_sha"] == sha_a
    assert "rewound" in item["warnings"][0]
    assert "main" in item["warnings"][0]
    assert sha_a[:7] in item["warnings"][0] and sha_b[:7] in item["warnings"][0]
    # Second reconcile still surfaces the warning, without emitting a duplicate event.
    assert len(out2) == 1 and out2[0]["kind"] == "rewound"
    assert len(ev_rows) == 1
    assert ev_rows[0]["data"]["kind"] == "rewound"


def test_reconcile_detects_non_ancestor_force_move(world):
    conn, pid, described = world
    bare = local_repo.bare_path(described["owner"], described["repo"])
    sha_a = described["head_sha"]
    sha_b = local_repo.commit_files(bare, "main", {"app.py": "v2\n"}, "second commit")

    # Create a diverged commit C from A (not an ancestor of B, and B is not an ancestor of C).
    local_repo.commit_files(bare, "alt", {"alt.py": "diverged\n"}, "diverged commit", parent=sha_a)
    sha_c = local_repo.branch_sha(bare, "alt")
    assert sha_c and not local_repo.is_ancestor(bare, sha_b, sha_c) and not local_repo.is_ancestor(bare, sha_c, sha_b)

    local_repo.git(["update-ref", "refs/heads/main", sha_c], git_dir=bare)

    out = local_git_hooks.reconcile(conn, Settings(), pid)
    ev_rows = conn.execute(
        text("SELECT data FROM events WHERE project_id = :p "
             "AND event_type = 'local_repo.default_branch_moved'"),
        {"p": pid}).mappings().all()

    assert len(out) == 1
    item = out[0]
    assert item["kind"] == "force_moved"
    assert item["default_branch_anomaly"]["kind"] == "force_moved"
    assert "force-moved" in item["warnings"][0]
    assert "non-ancestor" in item["warnings"][0]
    assert sha_b[:7] in item["warnings"][0] and sha_c[:7] in item["warnings"][0]
    assert len(ev_rows) == 1


def test_reconcile_allows_normal_fast_forward_without_warning(world):
    conn, pid, described = world
    bare = local_repo.bare_path(described["owner"], described["repo"])
    sha_b = local_repo.commit_files(bare, "main", {"app.py": "v2\n"}, "second commit")

    # Fast-forward main to descendant D outside commit_files (e.g., direct git update-ref).
    local_repo.commit_files(bare, "topic", {"app.py": "v3\n"}, "third commit", parent=sha_b)
    sha_d = local_repo.branch_sha(bare, "topic")
    local_repo.git(["update-ref", "refs/heads/main", sha_d], git_dir=bare)

    out = local_git_hooks.reconcile(conn, Settings(), pid)
    ev_count = conn.execute(
        text("SELECT count(*) FROM events WHERE project_id = :p "
             "AND event_type = 'local_repo.default_branch_moved'"),
        {"p": pid}).scalar()

    assert not any(x.get("default_branch_anomaly") for x in out)
    assert local_repo.recorded_default_sha(bare) == sha_d
    assert ev_count == 0
