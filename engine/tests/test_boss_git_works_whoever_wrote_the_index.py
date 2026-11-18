"""BOSS'S GIT IN A SANDBOX WORKS WHOEVER WROTE .git LAST.

THE CASE (2026-11-18 15:51 UTC, laptop, rc 4d9706d78, Easy Mode "Recipe box",
crun_6883ec7cbd67401a9ce8): write_tests, implement, run_tests and review_panel all finished, then
the run failed on `git add failed: fatal: .git/index: index file open failed: Permission denied`
in `EphemeralSandbox.diff()`, and the workspace save on the way out failed the same way, so the
finished work was not kept either.

WHY, proven on the install's own image (git 2.39.5, setpriv as the run's uid):

  * BOSS's own git on a run's checkout runs AS the run's uid (sandbox._git_once, through
    boss-sandbox-exec). That uid has no supplementary groups: only the "other" bits reach it.
  * The review seat was Codex (llm_calls: codex at 15:51:03.58), which has no usable inner
    read-only mode in a container, so it judges a copy and `readonly_guard` fingerprints the live
    tree with `git status` — run by the API process itself, as its own uid (boss, 100; the container starts as root and the entrypoint drops). `git status` refreshes the
    index and REWRITES .git/index as that uid.
  * The hourly backup ran 15:50:48–15:51:24 (backups row bkp_20261118T155048Z_7c00ec) and held
    `os.umask(0o077)` around pg_dump. The umask is process-wide: the guard's git inherited it,
    and .git/index came back `boss 0600`. The run's `git add -A` 0.5 s later could not open it.
    (umask 022 gives boss 0644, which the run's uid can still read: the backup window is why it
    happened once in the whole DB.)

THE FIX, as a class:
  * no process-wide umask anywhere in BOSS (backup, shim socket, sandbox dirs, toolchain cache,
    template trial set modes by chmod) — guarded below;
  * watching git never writes (GIT_OPTIONAL_LOCKS=0 in readonly_guard);
  * BOSS's sandbox git repairs a .git another uid wrote (opens it to the run's uid) and tries
    once more; staging falls back to the sandbox's own index; a failure after both says in words
    what failed and that Retry continues from the saved work.

Real git in a temporary repository. The run's uid is simulated at the one seam BOSS's git goes
through (owned_process.run with the sandbox owner): it may open a file only through its "other"
bits, exactly as the dropped uid does. No model, no database.
"""
from __future__ import annotations

import ast
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import owned_process, readonly_guard, sandbox, sandbox_identity
from boss_engine.services import workspace_perms

ROOT = Path(sandbox.__file__).resolve().parents[3]
DENIED = "fatal: .git/index: index file open failed: Permission denied"


def _plain_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path, monkeypatch, *, index_ok=None):
    """A checkout whose BOSS git runs "as the run's uid": refused when that uid could not open
    the index file git would use (only its "other" bits apply)."""
    _plain_git(tmp_path, "init", "-q")
    _plain_git(tmp_path, "config", "user.email", "t@example.com")
    _plain_git(tmp_path, "config", "user.name", "t")
    (tmp_path / "recipes.js").write_text("export const recipes = [];\n")
    _plain_git(tmp_path, "add", "-A")
    _plain_git(tmp_path, "commit", "-qm", "base")
    base = _plain_git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    ident = SimpleNamespace(uid=os.geteuid() + 1, run_dir=tmp_path)  # never BOSS's own uid
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)
    calls = []

    def as_run_uid(argv, *, env=None, cwd=None, **kwargs):
        assert kwargs.get("sandboxed") is True and kwargs.get("owner") == "run-owner"
        calls.append(argv)
        index = Path((env or {}).get("GIT_INDEX_FILE") or Path(cwd) / ".git" / "index")
        readable = (index_ok(index) if index_ok is not None
                    else (not index.exists() or stat.S_IMODE(index.stat().st_mode) & 0o004))
        if not readable:
            return subprocess.CompletedProcess(argv, 128, "", DENIED + "\n")
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True,
                              timeout=kwargs.get("timeout"))

    monkeypatch.setattr(owned_process, "run", as_run_uid)
    box = sandbox.EphemeralSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=60,
                                                   sandbox_validation_output_limit_chars=10000),
                                   task_id="crun_test")
    box.path = tmp_path
    box.diff_base_sha = base
    box._sandbox = ident
    box._git_owner = "run-owner"
    return box, calls


def _rewritten_by_boss_under_backup_umask(index: Path) -> None:
    """What the review seat's guard did to the index during the backup: rewritten 0600."""
    os.chmod(index, 0o600)


def test_an_index_boss_rewrote_0600_is_repaired_and_the_diff_works(tmp_path, monkeypatch):
    box, _ = _repo(tmp_path, monkeypatch)
    (tmp_path / "recipes.js").write_text("export const recipes = ['soup'];\n")
    (tmp_path / "box.js").write_text("export {};\n")
    _rewritten_by_boss_under_backup_umask(tmp_path / ".git" / "index")

    summary = box.diff_summary()

    assert sorted(summary.paths) == ["box.js", "recipes.js"]
    assert box.git_dir_repaired == [".git/index"]
    # (the retried `add -A` then wrote a fresh index of its own)
    assert stat.S_IMODE((tmp_path / ".git" / "index").stat().st_mode) & 0o004
    # diff() (the PR's files, the workspace save on any exit) reads the same way.
    assert sorted(c.path for c in box.diff()) == ["box.js", "recipes.js"]


def test_every_boss_git_command_repairs_once_and_retries(tmp_path, monkeypatch):
    box, calls = _repo(tmp_path, monkeypatch)
    (tmp_path / "recipes.js").write_text("changed\n")
    _rewritten_by_boss_under_backup_umask(tmp_path / ".git" / "index")

    assert box.has_changes() is True             # `git status`, not only `add -A`
    assert sum(1 for a in calls if "status" in a) == 2


def test_an_index_boss_cannot_repair_says_in_words_and_points_at_the_retry(tmp_path, monkeypatch):
    box, _ = _repo(tmp_path, monkeypatch, index_ok=lambda index: False)
    monkeypatch.setattr(workspace_perms, "repair_git_index", lambda *a, **k: [])
    (tmp_path / "recipes.js").write_text("changed\n")

    with pytest.raises(sandbox.SandboxError) as caught:
        box.diff_summary()

    words = str(caught.value)
    assert words.startswith("BOSS could not collect this build's changes")
    assert "index file open failed: Permission denied" in words
    assert "Retry continues from the saved work" in words
    assert not (tmp_path / ".git" / "boss-index").exists()   # no second index, no missed files


@pytest.mark.parametrize("error", [
    "git rev-parse failed: fatal: ambiguous argument 'no-such-ref'",
    # The agent can make any `add -A` say "Permission denied" (a worktree file chmod 000): that
    # is not the index, and never starts a repair.
    "git add failed: error: open(\"secret.txt\"): Permission denied",
])
def test_a_refusal_that_is_not_the_index_is_not_repaired(tmp_path, monkeypatch, error):
    box, _ = _repo(tmp_path, monkeypatch)
    repaired = []
    monkeypatch.setattr(workspace_perms, "repair_git_index",
                        lambda *a, **k: repaired.append(a) or [".git/index"])
    assert box._repair_git_dir(sandbox.SandboxError(error)) is False
    assert repaired == []


# ── the repair itself: exactly the index, never through a link ─────────────────────────────

OTHER = os.geteuid() + 1        # "the run's uid": anyone but BOSS


def _git_dir(tmp_path) -> Path:
    ws = tmp_path.resolve() / "ws"
    (ws / ".git" / "objects" / "ab").mkdir(parents=True)
    (ws / ".git" / "index").write_bytes(b"DIRC")
    os.chmod(ws / ".git" / "index", 0o600)
    (ws / ".git" / "objects" / "ab" / "cd").write_bytes(b"x")
    os.chmod(ws / ".git" / "objects" / "ab" / "cd", 0o400)
    return ws


def _secret(tmp_path) -> Path:
    """A BOSS-owned file outside the sandbox (master.key, a backup) the run's uid wants opened."""
    outside = tmp_path.resolve() / "boss-only"
    outside.mkdir()
    key = outside / "index"
    key.write_bytes(b"KEY")
    os.chmod(key, 0o600)
    os.chmod(outside, 0o700)
    return key


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_repair_opens_exactly_the_index_and_its_lock(tmp_path):
    ws = _git_dir(tmp_path)
    (ws / ".git" / "index.lock").write_bytes(b"")
    os.chmod(ws / ".git" / "index.lock", 0o600)

    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == [".git/index", ".git/index.lock"]
    assert _mode(ws / ".git" / "index") == 0o666
    assert _mode(ws / ".git" / "objects" / "ab" / "cd") == 0o400      # nothing else in .git
    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == []    # already usable
    # The run's own index is its business; no sandbox uid, nothing to do.
    os.chmod(ws / ".git" / "index", 0o600)
    assert workspace_perms.repair_git_index(ws, run_uid=os.geteuid()) == []
    assert workspace_perms.repair_git_index(ws, run_uid=None) == []


def test_an_index_swapped_for_a_symlink_is_never_followed(tmp_path):
    ws, key = _git_dir(tmp_path), _secret(tmp_path)
    os.unlink(ws / ".git" / "index")
    os.symlink(key, ws / ".git" / "index")

    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == []
    assert _mode(key) == 0o600


def test_a_git_dir_swapped_for_a_symlink_is_never_followed(tmp_path):
    ws, key = _git_dir(tmp_path), _secret(tmp_path)
    os.rename(ws / ".git", ws / "git.real")
    os.symlink(key.parent, ws / ".git")

    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == []
    assert _mode(key) == 0o600


def test_a_workspace_swapped_for_a_symlink_is_never_followed(tmp_path):
    ws, key = _git_dir(tmp_path), _secret(tmp_path)
    (key.parent / ".git").mkdir()
    os.rename(key, key.parent / ".git" / "index")
    os.rename(ws, tmp_path.resolve() / "ws.real")
    os.symlink(key.parent, ws)

    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == []
    assert _mode(key.parent / ".git" / "index") == 0o600


def test_a_hardlinked_index_is_never_changed(tmp_path):
    ws, key = _git_dir(tmp_path), _secret(tmp_path)
    os.unlink(ws / ".git" / "index")
    os.link(key, ws / ".git" / "index")              # same inode as BOSS's secret

    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == []
    assert _mode(key) == 0o600


def test_the_swap_race_between_listing_and_open_is_closed(tmp_path, monkeypatch):
    """The reviewer's PoC raced a path walk: here the swap lands at the worst moment, right
    after the .git fd is open and before the index is opened. The fd still names the real
    .git; the link put in place of the index is refused (O_NOFOLLOW)."""
    ws, key = _git_dir(tmp_path), _secret(tmp_path)
    real_open = os.open

    def swapping_open(path, flags, *a, dir_fd=None, **k):
        if path == "index" and dir_fd is not None:
            os.unlink(ws / ".git" / "index")
            os.symlink(key, ws / ".git" / "index")
        return real_open(path, flags, *a, dir_fd=dir_fd, **k)

    monkeypatch.setattr(workspace_perms.os, "open", swapping_open)
    assert workspace_perms.repair_git_index(ws, run_uid=OTHER) == []
    assert _mode(key) == 0o600


# ── the read-only guard: never writes, never runs tree code ────────────────────────────────

def _committed(tmp_path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _plain_git(tmp_path, "init", "-q")
    _plain_git(tmp_path, "config", "user.email", "t@example.com")
    _plain_git(tmp_path, "config", "user.name", "t")
    (tmp_path / "recipes.txt").write_text("x\n")
    _plain_git(tmp_path, "add", "-A")
    _plain_git(tmp_path, "commit", "-qm", "base")
    return tmp_path / "recipes.txt"


def test_the_read_only_guard_never_rewrites_the_index(tmp_path):
    tracked = _committed(tmp_path)
    index = tmp_path / ".git" / "index"

    def stale():                                     # same content, new stat: refresh wants a write
        later = index.stat().st_mtime + 10
        os.utime(tracked, (later, later))
        return index.stat().st_ino, index.stat().st_mtime_ns

    before = stale()
    readonly_guard.snapshot(tmp_path)
    assert (index.stat().st_ino, index.stat().st_mtime_ns) == before

    # The control: a plain `git status` (what the guard ran before) rewrites it.
    before = stale()
    _plain_git(tmp_path, "status", "--porcelain")
    assert (index.stat().st_ino, index.stat().st_mtime_ns) != before


def _planted(tmp_path, key: str, marker: Path) -> None:
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    os.chmod(script, 0o755)
    _plain_git(tmp_path, "config", key, str(script))


def test_the_read_only_guard_never_runs_a_planted_fsmonitor(tmp_path):
    tracked = _committed(tmp_path)
    marker = tmp_path.parent / f"{tmp_path.name}-PWNED"
    _planted(tmp_path, "core.fsmonitor", marker)
    tracked.write_text("y\n")

    snap = readonly_guard.snapshot(tmp_path)

    assert not marker.exists()
    assert snap is not None and "recipes.txt" in snap["prints"]
    # The control: git as before runs it.
    _plain_git(tmp_path, "status", "--porcelain")
    assert marker.exists()


def test_the_read_only_guard_never_runs_a_planted_filter(tmp_path):
    tracked = _committed(tmp_path)
    marker = tmp_path.parent / f"{tmp_path.name}-PWNED"
    (tmp_path / ".gitattributes").write_text("*.txt filter=evil\n")
    _planted(tmp_path, "filter.evil.clean", marker)
    _plain_git(tmp_path, "config", "filter.evil.required", "true")
    later = (tmp_path / ".git" / "index").stat().st_mtime + 10
    os.utime(tracked, (later, later))                # stat-dirty: status would run clean

    readonly_guard.snapshot(tmp_path)

    assert not marker.exists()
    _plain_git(tmp_path, "status", "--porcelain")
    assert marker.exists()


# ── the guard's git runs as the run's uid, pinned to the tree ──────────────────────────────

def _record_guard_git(monkeypatch, *, ident):
    """Bind `ident` as the run identity the guard sees; record every git it spawns (and run it,
    as the run's uid would)."""
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)
    seen = []

    def run(argv, *, sandboxed=None, **kw):
        seen.append((list(argv), sandboxed, dict(kw.get("env") or {})))
        kw.pop("owner", None)
        return subprocess.run(argv, **kw)

    monkeypatch.setattr(owned_process, "run", run)
    return seen


def test_a_sandboxed_runs_guard_git_runs_as_the_run_uid_pinned_to_its_tree(tmp_path, monkeypatch):
    tracked = _committed(tmp_path)
    seen = _record_guard_git(monkeypatch, ident=SimpleNamespace(uid=OTHER, run_dir=tmp_path))
    tracked.write_text("changed\n")

    snap = readonly_guard.snapshot(tmp_path / ".")

    assert snap is not None and "recipes.txt" in snap["prints"] and "unverifiable" not in snap
    assert seen and all(sandboxed is True for _, sandboxed, _ in seen)    # never as BOSS
    top = os.path.realpath(tmp_path)
    assert all(env["GIT_DIR"] == f"{top}/.git" and env["GIT_WORK_TREE"] == top
               and env["GIT_OPTIONAL_LOCKS"] == "0" for _, _, env in seen)


def test_a_run_directory_with_no_identity_is_unverifiable_and_runs_no_git(tmp_path, monkeypatch):
    _committed(tmp_path)
    seen = _record_guard_git(monkeypatch, ident=None)
    monkeypatch.setattr(sandbox_identity, "run_root", lambda: tmp_path.parent)

    snap = readonly_guard.snapshot(tmp_path)

    assert "no run identity" in snap["unverifiable"]
    assert seen == []
    report = readonly_guard.compare(snap)
    assert report["unverifiable"] and "could not be checked" in readonly_guard.words(report)


def test_core_worktree_cannot_blind_the_guard(tmp_path):
    """The run's uid sets core.worktree to a directory that is not a repository: the guard used
    to follow it, fail, return None — and None read as "no change"."""
    tracked = _committed(tmp_path / "ws")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = readonly_guard.snapshot(tmp_path / "ws")
    _plain_git(tmp_path / "ws", "config", "core.worktree", str(elsewhere))
    tracked.write_text("the reviewer wrote this\n")

    report = readonly_guard.compare(before)

    assert report is not None and report["paths"] == ["recipes.txt"]
    assert report["unexplained"] == ["recipes.txt"]


@pytest.mark.parametrize("how", ["symlink", "gitfile"])
def test_a_git_dir_that_is_not_a_real_directory_is_unverifiable(tmp_path, how):
    _committed(tmp_path / "ws")
    before = readonly_guard.snapshot(tmp_path / "ws")
    git = tmp_path / "ws" / ".git"
    os.rename(git, tmp_path / "real.git")
    if how == "symlink":
        os.symlink(tmp_path / "real.git", git)
    else:
        git.write_text(f"gitdir: {tmp_path / 'real.git'}\n")

    report = readonly_guard.compare(before)

    assert report is not None and "not a real directory" in report["unverifiable"]


def test_a_repo_that_stops_being_one_is_unverifiable_not_unchanged(tmp_path):
    _committed(tmp_path / "ws")
    before = readonly_guard.snapshot(tmp_path / "ws")
    import shutil
    shutil.rmtree(tmp_path / "ws" / ".git")
    assert readonly_guard.compare(before)["unverifiable"]


def test_a_submodules_own_filter_never_runs_as_boss(tmp_path):
    tracked = _committed(tmp_path / "ws")
    sub = tmp_path / "ws" / "sub"
    sub.mkdir()
    _committed(sub)
    marker = tmp_path / "PWNED"
    (sub / ".gitattributes").write_text("*.txt filter=z\n")
    _planted(sub, "filter.z.clean", marker)
    _plain_git(tmp_path / "ws", "add", "sub")                       # a gitlink
    _plain_git(tmp_path / "ws", "commit", "-qm", "sub")
    later = (sub / ".git" / "index").stat().st_mtime + 10
    os.utime(sub / "recipes.txt", (later, later))
    tracked.write_text("y\n")

    snap = readonly_guard.snapshot(tmp_path / "ws")

    assert "recipes.txt" in snap["prints"]
    assert not marker.exists()


def test_the_guard_never_reads_through_a_link_the_run_planted(tmp_path):
    tracked = _committed(tmp_path / "ws")
    secret = tmp_path / "boss-secret"
    secret.write_text("KEY")
    tracked.unlink()
    os.symlink(secret, tracked)                                     # a dirty "file"

    snap = readonly_guard.snapshot(tmp_path / "ws")

    assert snap["prints"]["recipes.txt"] == f"link:{secret}"


# ── a warm-reused checkout: every git as the run's uid ─────────────────────────────────────

def test_a_warm_reused_checkouts_git_runs_as_the_run_uid(tmp_path, monkeypatch):
    """The lineage's previous round wrote this .git: a clean filter, an ext:: remote. BOSS's git
    on it used to run as BOSS (`_sandbox` was never set on the warm path)."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _committed(seed)
    _plain_git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    warm = tmp_path / "warm"
    _plain_git(tmp_path, "clone", "-q", str(origin), str(warm))
    marker = tmp_path / "PWNED"
    (warm / ".gitattributes").write_text("*.txt filter=evil\n")
    _planted(warm, "filter.evil.clean", marker)
    _plain_git(warm, "config", "protocol.ext.allow", "always")

    ident = SimpleNamespace(uid=OTHER, run_dir=tmp_path)
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)
    monkeypatch.setattr(owned_process, "current_owner", lambda: "run-owner")
    seen = []

    def run(argv, *, sandboxed=None, owner=None, umask=None, **kw):
        seen.append((argv[3:5], sandboxed, owner))
        return subprocess.run(argv, **kw)

    monkeypatch.setattr(owned_process, "run", run)
    box = sandbox.EphemeralSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=60,
                                                   sandbox_validation_output_limit_chars=10000),
                                   task_id="crun_warm")
    box._provision_warm("o", "r", base_branch="main", work_branch="main", token=None,
                        repo_host="local", egress=None, warm_dir=warm)
    box.diff_summary()

    assert seen and all(sb is True and owner == "run-owner" for _, sb, owner in seen), seen


# ── no process-wide umask ──────────────────────────────────────────────────────────────────

def _umask_uses(path: Path) -> list[int]:
    """Every call of anything named umask (os.umask, an `import os as _os` alias, a bare
    `umask(...)` after `from os import umask`) and every import of it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "umask":
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(a.name == "umask" for a in node.names):
            lines.append(node.lineno)
    return lines


def test_the_umask_guard_sees_every_spelling(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("import os as _os\nfrom os import umask\n_os.umask(0)\numask(0o77)\n")
    assert sorted(_umask_uses(probe)) == [2, 3, 4]


def test_no_process_wide_umask_anywhere_in_boss():
    """os.umask is process-wide: one thread's 077 (the backup) made another thread's git write a
    run's index 0600; a 0 made every concurrent file world-writable. Modes are set per file, or
    per spawn (Popen's `umask`, a keyword, not a call)."""
    offenders = []
    for top in (ROOT / "engine" / "boss_engine", ROOT / "apps"):
        for path in top.rglob("*.py"):
            if "node_modules" in path.parts or "tests" in path.parts:
                continue
            for line in _umask_uses(path):
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == []
