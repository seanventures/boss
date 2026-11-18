"""THE RUN'S UID CAN ALWAYS ADD OBJECTS TO ITS OWN .git.

THE CASE (2026-11-18 17:36 UTC, laptop, release head a17f440f7, Recipe box follow-up
crun_47c3aea2add242b3ad4a): after the brief-items send-back (code_task.brief_items_sent_back at
17:35:25, then brief_items_judged reworked+reverted at 17:36:26) the run failed on
`git add failed: error: insufficient permission for adding an object to repository database
.git/objects / error: tests/recipes.test.js: failed to insert into database` in
sandbox._stage_all <- diff_summary. (Not lineage reuse: lineage_sandboxes is empty on the laptop.)

WHY, reproduced on the install's own image with real uids (boss = BOSS, 61649 = the run):
  * provisioning clones with a per-spawn umask 0: every .git/objects fan-out dir is 2777;
  * the send-back takes `_tree_snapshot` — `git add -A` + `write-tree` into a temporary index — by
    plain `subprocess.run` as the API's uid (boss) under umask 022. Writing the tree's blobs
    created 50 of the fan-out directories `drwxr-sr-x boss`;
  * the run's uid (no groups: only "other" bits) then could not add an object whose name falls
    in one of them: "insufficient permission for adding an object to repository database".
    Its last line, "unable to index file 'X'", also made #217's wording blame the INDEX.

THE FIX, as a class:
  * the snapshot/restore pair runs its git through owned_process — AS THE RUN'S UID when the run
    holds one — with its temporary index inside .git, removed after (restored files are the
    run's own, too);
  * BOSS's sandbox git repairs a refused object store like a refused index
    (workspace_perms.repair_git_objects: fd-based, O_NOFOLLOW from BOSS's sandbox root, only
    directories BOSS owns, fchmod) and retries once; the words name what refused.

Real git; the run's uid is simulated at the owned_process seam (it may only use "other" bits on
what BOSS's uid owns). No model, no database.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import governed_code_task_service as gcts
from boss_engine.services import owned_process, sandbox, sandbox_identity, workspace_perms

OTHER = os.geteuid() + 1
REFUSED = ("error: insufficient permission for adding an object to repository database "
           ".git/objects\nerror: f1.txt: failed to insert into database\n"
           "error: unable to index file 'f1.txt'\nfatal: updating files failed")


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env=env)


def _provisioned(tmp_path) -> Path:
    """A clone as provisioning leaves it: every object directory open (umask 0)."""
    ws = tmp_path.resolve() / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("# Recipe box\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "base")
    for d in (ws / ".git" / "objects").iterdir():
        os.chmod(d, 0o2777 if d.is_dir() else d.stat().st_mode)
    os.chmod(ws / ".git" / "objects", 0o2777)
    return ws


def _boss_snapshot_as_it_was(ws: Path, tmp_path) -> None:
    """The old _tree_snapshot: BOSS's uid, umask 022, a temporary index."""
    for i in range(60):
        (ws / f"f{i}.txt").write_text(f"v{i}\n")
    old = os.umask(0o022)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "tmp.index")}
        _git(ws, "add", "-A", env=env)
        _git(ws, "write-tree", env=env)
    finally:
        os.umask(old)


def _closed_object_dirs(ws: Path) -> list[str]:
    """Fan-out directories the run's uid ("other") could not add an object into."""
    return [d.name for d in (ws / ".git" / "objects").iterdir()
            if d.is_dir() and d.stat().st_uid == os.geteuid()
            and not stat.S_IMODE(d.stat().st_mode) & 0o002]


def _sandbox_as_run_uid(ws: Path, monkeypatch):
    ident = SimpleNamespace(uid=OTHER, run_dir=ws)
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)

    def as_run_uid(argv, *, env=None, cwd=None, **kw):
        assert kw.get("sandboxed") is True and kw.get("owner") == "run-owner"
        if "add" in argv and _closed_object_dirs(ws):
            return subprocess.CompletedProcess(argv, 128, "", REFUSED)
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True)

    monkeypatch.setattr(owned_process, "run", as_run_uid)
    box = sandbox.EphemeralSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=60,
                                                   sandbox_validation_output_limit_chars=10000),
                                   task_id="crun_test")
    box.path = ws
    box.diff_base_sha = _git(ws, "rev-parse", "HEAD").stdout.strip()
    box._sandbox = ident
    box._git_owner = "run-owner"
    return box


def test_object_dirs_boss_created_are_repaired_and_the_runs_add_works(tmp_path, monkeypatch):
    ws = _provisioned(tmp_path)
    _boss_snapshot_as_it_was(ws, tmp_path)
    closed = _closed_object_dirs(ws)
    assert closed                                       # the incident's state

    box = _sandbox_as_run_uid(ws, monkeypatch)
    for i in range(60):
        (ws / f"f{i}.txt").write_text(f"rework {i}\n")
    summary = box.diff_summary()

    assert len(summary.paths) == 60
    # Every directory BOSS had closed is open again (new ones the retried add made in this test
    # are "the run's own" in production: it creates them).
    objects = ws / ".git" / "objects"
    assert all(stat.S_IMODE((objects / d).stat().st_mode) & 0o002 for d in closed)
    assert {f".git/objects/{d}" for d in closed} <= set(box.git_dir_repaired)


def test_an_object_store_boss_cannot_repair_is_named_in_words(tmp_path, monkeypatch):
    ws = _provisioned(tmp_path)
    _boss_snapshot_as_it_was(ws, tmp_path)
    box = _sandbox_as_run_uid(ws, monkeypatch)
    monkeypatch.setattr(workspace_perms, "repair_git_objects", lambda *a, **k: [])
    (ws / "f1.txt").write_text("changed\n")

    with pytest.raises(sandbox.SandboxError) as caught:
        box.diff_summary()

    words = str(caught.value)
    assert "git refused this checkout's object store (.git/objects)" in words
    assert "index (.git/index)" not in words
    assert "Retry continues from the saved work" in words


def test_a_worktree_file_the_run_locked_is_not_a_store_refusal():
    worktree = ("git add failed: error: open(\"secret.txt\"): Permission denied\n"
                "error: unable to index file 'secret.txt'\nfatal: adding files failed")
    assert sandbox.EphemeralSandbox._store_refused(sandbox.SandboxError(worktree)) is False
    assert sandbox.EphemeralSandbox._store_refused(sandbox.SandboxError(REFUSED)) is True


# ── BOSS's tree snapshot/restore runs as the run's uid ─────────────────────────────────────

def test_the_tree_snapshot_and_restore_run_through_the_run_uid_seam(tmp_path, monkeypatch):
    ws = _provisioned(tmp_path)
    seen = []

    def run(argv, *, env=None, cwd=None, **kw):
        seen.append((argv, kw.get("sandboxed"), env.get("GIT_INDEX_FILE")))
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True)

    monkeypatch.setattr(owned_process, "run", run)
    (ws / "README.md").write_text("# Recipe box, passing\n")
    (ws / "new.txt").write_text("kept\n")

    tree = gcts._tree_snapshot(ws)
    (ws / "README.md").write_text("# rework that broke it\n")
    (ws / "stray.txt").write_text("not in the snapshot\n")
    assert gcts._tree_restore(ws, tree) is True

    assert tree and (ws / "README.md").read_text() == "# Recipe box, passing\n"
    assert (ws / "new.txt").exists() and not (ws / "stray.txt").exists()
    # Every git of the pair went through owned_process (wrapped as the run's uid when the run
    # holds one), never opted out; its index lived in .git and is gone.
    assert seen and all(sb is not False for _, sb, _ in seen)
    assert all(Path(i).parent == ws / ".git" for _, _, i in seen)
    assert not list((ws / ".git").glob("boss-tree-*.index"))
    assert all("core.hooksPath=/dev/null" in argv for argv, _, _ in seen)


def test_no_control_plane_git_writes_a_runs_tree_by_plain_subprocess():
    """The snapshot/restore pair never spawns git with BOSS's own uid behind owned_process."""
    import inspect

    for fn in (gcts._tree_git, gcts._tree_snapshot, gcts._tree_restore):
        assert "subprocess.run" not in inspect.getsource(fn)


# ── the repair itself: only BOSS's object directories, never through a link ────────────────

def _store(tmp_path) -> Path:
    ws = tmp_path.resolve() / "ws"
    objects = ws / ".git" / "objects"
    for name in ("ab", "cd", "pack", "info"):
        (objects / name).mkdir(parents=True)
        os.chmod(objects / name, 0o755)
    (objects / "ab" / "0123").write_bytes(b"blob")
    os.chmod(objects / "ab" / "0123", 0o444)
    (objects / "notes").mkdir()
    os.chmod(objects / "notes", 0o755)
    return ws


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_repair_opens_exactly_the_object_directories(tmp_path):
    ws = _store(tmp_path)
    changed = workspace_perms.repair_git_objects(ws, run_uid=OTHER, trusted_root=tmp_path)

    assert sorted(changed) == [".git/objects", ".git/objects/ab", ".git/objects/cd",
                               ".git/objects/info", ".git/objects/pack"]
    objects = ws / ".git" / "objects"
    assert _mode(objects / "ab") & 0o777 == 0o777
    assert _mode(objects / "ab" / "0123") == 0o444          # objects stay read-only
    assert _mode(objects / "notes") == 0o755                # not an object directory
    assert workspace_perms.repair_git_objects(ws, run_uid=OTHER) == []
    assert workspace_perms.repair_git_objects(ws, run_uid=os.geteuid()) == []
    assert workspace_perms.repair_git_objects(ws, run_uid=None) == []


def test_a_fan_out_dir_swapped_for_a_symlink_is_never_followed(tmp_path):
    ws = _store(tmp_path)
    outside = tmp_path.resolve() / "boss-only"
    outside.mkdir()
    os.chmod(outside, 0o700)
    os.rmdir(ws / ".git" / "objects" / "cd")
    os.symlink(outside, ws / ".git" / "objects" / "cd")

    changed = workspace_perms.repair_git_objects(ws, run_uid=OTHER)

    assert ".git/objects/cd" not in changed
    assert _mode(outside) == 0o700


def test_an_objects_dir_swapped_for_a_symlink_is_never_followed(tmp_path):
    ws = _store(tmp_path)
    outside = tmp_path.resolve() / "boss-only"
    (outside / "ab").mkdir(parents=True)
    os.chmod(outside / "ab", 0o700)
    os.rename(ws / ".git" / "objects", tmp_path / "objects.real")
    os.symlink(outside, ws / ".git" / "objects")

    assert workspace_perms.repair_git_objects(ws, run_uid=OTHER) == []
    assert _mode(outside / "ab") == 0o700


# ── BOSS never writes or deletes THROUGH a path the run's uid controls (PR #219 review) ─────

def _boss_only(tmp_path) -> Path:
    """A BOSS directory outside the run: a key the run's uid would like gone or rewritten."""
    d = tmp_path.resolve() / "bossdata"
    d.mkdir()
    (d / "victim.key").write_text("KEY")
    (d / "exclude").write_text("BOSS FILE\n")
    (d / "mcp.json").write_text("BOSS FILE\n")
    os.chmod(d, 0o700)
    return d


def test_restore_never_deletes_through_a_dir_swapped_after_the_listing(tmp_path, monkeypatch):
    ws, boss = _provisioned(tmp_path), _boss_only(tmp_path)
    tree = gcts._tree_snapshot(ws)
    (ws / "d").mkdir()
    (ws / "d" / "victim.key").write_text("the run's own")        # untracked: to be removed
    real = gcts._tree_git

    def swap_after_listing(root, args, index):
        done = real(root, args, index)
        if args[0] == "ls-files":                                  # the attacker wins the race
            os.rename(ws / "d", ws / "d.real")
            os.symlink(boss, ws / "d")
        return done

    monkeypatch.setattr(gcts, "_tree_git", swap_after_listing)
    assert gcts._tree_restore(ws, tree) is True
    assert (boss / "victim.key").read_text() == "KEY"


def test_the_snapshot_pair_is_pinned_to_its_tree(tmp_path):
    ws, boss = _provisioned(tmp_path), _boss_only(tmp_path)
    os.chmod(boss, 0o755)
    (boss / "keep.txt").write_text("BOSS's")
    tree = gcts._tree_snapshot(ws)
    _git(ws, "config", "core.worktree", str(boss))                # planted by the run
    (ws / "stray.txt").write_text("x")

    assert gcts._tree_restore(ws, tree) is True
    assert not (ws / "stray.txt").exists()                        # the run's tree, not boss's
    assert (boss / "keep.txt").exists() and (boss / "victim.key").exists()


def _box(ws: Path):
    box = sandbox.EphemeralSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=60,
                                                   sandbox_validation_output_limit_chars=10000),
                                   task_id="crun_test")
    box.path = ws
    return box


def test_a_symlinked_exclude_is_replaced_never_written_through(tmp_path):
    ws, boss = _provisioned(tmp_path), _boss_only(tmp_path)
    exclude = ws / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    if exclude.exists():
        exclude.unlink()
    os.symlink(boss / "exclude", exclude)
    box = _box(ws)

    box._write_local_excludes()
    box._write_left_out_block(["fixtureclock"])

    assert (boss / "exclude").read_text() == "BOSS FILE\n"
    assert not exclude.is_symlink()
    text = exclude.read_text()
    assert "# BOSS sandbox artifact ignores" in text and "/fixtureclock" in text
    assert "BOSS FILE" not in text                                 # the target was never read


def test_a_symlinked_git_info_is_never_written_into(tmp_path):
    ws, boss = _provisioned(tmp_path), _boss_only(tmp_path)
    import shutil
    shutil.rmtree(ws / ".git" / "info", ignore_errors=True)
    os.symlink(boss, ws / ".git" / "info")
    box = _box(ws)

    box._write_local_excludes()
    box._write_left_out_block(["fixtureclock"])

    assert (boss / "exclude").read_text() == "BOSS FILE\n"
    assert sorted(p.name for p in boss.iterdir()) == ["exclude", "mcp.json", "victim.key"]


def _cursor_kit(tmp_path, ws):
    from boss_engine.services import harness_delivery as hd
    from boss_engine.services.code_task_mcp import HarnessMcp

    path = tmp_path / "mcp-src.json"
    path.write_text('{"mcpServers": {"docs": {"type": "http", "url": "https://d.example/mcp",'
                    ' "headers": {"Authorization": "Bearer t"}}}}')
    mcp = HarnessMcp(path=str(path), servers=["docs"], disallowed=[], gateway_token="")
    return hd.deliver(engine="cursor", mode="cursor_workspace", mcp=mcp, workspace=ws,
                      gateway=False)


def test_cursor_mcp_json_replaces_a_planted_link_and_puts_it_back(tmp_path):
    ws, boss = tmp_path.resolve() / "ws", _boss_only(tmp_path)
    (ws / ".cursor").mkdir(parents=True)
    os.symlink(boss / "mcp.json", ws / ".cursor" / "mcp.json")

    kit = _cursor_kit(tmp_path, ws)
    try:
        written = ws / ".cursor" / "mcp.json"
        assert not written.is_symlink() and "d.example" in written.read_text()
        assert stat.S_IMODE(written.stat().st_mode) == 0o600      # no run uid here: BOSS only
        assert (boss / "mcp.json").read_text() == "BOSS FILE\n"
    finally:
        kit.cleanup()
    assert (boss / "mcp.json").read_text() == "BOSS FILE\n"


def test_cursor_mcp_json_is_never_written_through_a_linked_dot_cursor(tmp_path):
    ws, boss = tmp_path.resolve() / "ws", _boss_only(tmp_path)
    ws.mkdir()
    os.symlink(boss, ws / ".cursor")

    with pytest.raises(OSError):
        _cursor_kit(tmp_path, ws)
    assert (boss / "mcp.json").read_text() == "BOSS FILE\n"


def test_the_kernel_tree_sha_runs_through_the_run_uid_seam_pinned(tmp_path, monkeypatch):
    from boss_engine.kernel import workspace as kws

    ws = _provisioned(tmp_path)
    seen = []

    def run(argv, *, env=None, cwd=None, **kw):
        seen.append((argv, kw.get("sandboxed"), env))
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True)

    monkeypatch.setattr(owned_process, "run", run)
    (ws / "new.txt").write_text("x")
    sha = kws.tree_sha(ws)

    assert sha and len(sha) == 40
    assert seen and all(sb is not False and "core.fsmonitor=false" in argv
                        and env["GIT_DIR"] == str(ws / ".git") for argv, sb, env in seen)
    assert not list((ws / ".git").glob("boss-tree-*.index"))
    assert kws.tree_sha(tmp_path / "nowhere") is None
