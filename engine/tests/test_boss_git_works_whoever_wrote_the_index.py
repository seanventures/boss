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

    ident = SimpleNamespace(uid=os.geteuid() + 1)       # never BOSS's own uid
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
    assert ".git/index" in box.git_dir_repaired
    assert stat.S_IMODE((tmp_path / ".git" / "index").stat().st_mode) & 0o004
    assert box._private_index is None            # the real index works again
    # diff() (the PR's files, the workspace save on any exit) reads the same way.
    assert sorted(c.path for c in box.diff()) == ["box.js", "recipes.js"]


def test_every_boss_git_command_repairs_once_and_retries(tmp_path, monkeypatch):
    box, calls = _repo(tmp_path, monkeypatch)
    (tmp_path / "recipes.js").write_text("changed\n")
    _rewritten_by_boss_under_backup_umask(tmp_path / ".git" / "index")

    assert box.has_changes() is True             # `git status`, not only `add -A`
    assert sum(1 for a in calls if "status" in a) == 2


def test_an_index_boss_cannot_repair_falls_back_to_the_sandboxs_own(tmp_path, monkeypatch):

    def only_the_private_index(index):
        return index.name == "boss-index"

    box, _ = _repo(tmp_path, monkeypatch, index_ok=only_the_private_index)
    real = tmp_path / ".git" / "index"
    monkeypatch.setattr(workspace_perms, "repair_git_dir", lambda *a, **k: [])
    (tmp_path / "recipes.js").write_text("export const recipes = ['stew'];\n")
    (tmp_path / "new.js").write_text("export {};\n")

    summary = box.diff_summary()

    assert sorted(summary.paths) == ["new.js", "recipes.js"]
    assert box._private_index == str(tmp_path / ".git" / "boss-index")
    assert real.exists()                          # the real index is left alone
    assert sorted(c.path for c in box.diff()) == ["new.js", "recipes.js"]


def test_a_failure_after_both_says_in_words_and_points_at_the_retry(tmp_path, monkeypatch):
    box, _ = _repo(tmp_path, monkeypatch, index_ok=lambda index: False)
    monkeypatch.setattr(workspace_perms, "repair_git_dir", lambda *a, **k: [])
    (tmp_path / "recipes.js").write_text("changed\n")

    with pytest.raises(sandbox.SandboxError) as caught:
        box.diff_summary()

    words = str(caught.value)
    assert words.startswith("BOSS could not collect this build's changes")
    assert "index file open failed: Permission denied" in words
    assert "Retry continues from the saved work" in words


def test_a_refusal_that_is_not_about_permissions_is_not_repaired(tmp_path, monkeypatch):
    box, _ = _repo(tmp_path, monkeypatch)
    repaired = []
    monkeypatch.setattr(workspace_perms, "repair_git_dir",
                        lambda *a, **k: repaired.append(a) or [".git/index"])
    with pytest.raises(sandbox.SandboxError):
        box._git(["rev-parse", "no-such-ref"])
    assert repaired == []


def test_repair_opens_only_what_the_run_uid_does_not_own(tmp_path):
    git_dir = tmp_path / ".git"
    (git_dir / "objects" / "ab").mkdir(parents=True)
    (git_dir / "index").write_bytes(b"DIRC")
    os.chmod(git_dir / "index", 0o600)
    (git_dir / "objects" / "ab" / "cd").write_bytes(b"x")
    os.chmod(git_dir / "objects" / "ab" / "cd", 0o400)
    os.chmod(git_dir / "objects" / "ab", 0o700)

    changed = workspace_perms.repair_git_dir(git_dir, run_uid=os.geteuid() + 1)

    assert ".git/index" in changed and ".git/objects/ab" in changed
    assert stat.S_IMODE((git_dir / "index").stat().st_mode) == 0o666
    assert stat.S_IMODE((git_dir / "objects" / "ab" / "cd").stat().st_mode) == 0o444  # read-only stays
    assert stat.S_IMODE((git_dir / "objects" / "ab").stat().st_mode) == 0o777
    # Entries the run's uid owns are its own business; no sandbox uid, nothing to do.
    assert workspace_perms.repair_git_dir(git_dir, run_uid=os.geteuid()) == []
    assert workspace_perms.repair_git_dir(git_dir, run_uid=None) == []


def test_the_read_only_guard_never_rewrites_the_index(tmp_path):
    _plain_git(tmp_path, "init", "-q")
    _plain_git(tmp_path, "config", "user.email", "t@example.com")
    _plain_git(tmp_path, "config", "user.name", "t")
    tracked = tmp_path / "recipes.js"
    tracked.write_text("x\n")
    _plain_git(tmp_path, "add", "-A")
    _plain_git(tmp_path, "commit", "-qm", "base")
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


def _umask_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "umask" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"]


def test_no_process_wide_umask_anywhere_in_boss():
    """os.umask is process-wide: one thread's 077 (the backup) made another thread's git write a
    run's index 0600; a 0 made every concurrent file world-writable. Modes are set per file."""
    offenders = []
    for top in (ROOT / "engine" / "boss_engine", ROOT / "apps"):
        for path in top.rglob("*.py"):
            if "node_modules" in path.parts or "tests" in path.parts:
                continue
            for line in _umask_calls(path):
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == []
