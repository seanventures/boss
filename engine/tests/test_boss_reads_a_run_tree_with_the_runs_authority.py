"""BOSS READS A RUN'S TREE WITH THE RUN'S AUTHORITY, AND SHARES WHAT IT WRITES ONLY INSIDE THE
RUN'S OWN DIRECTORY (follow-ups from the security review of PR #219).

1. A HARDLINKED .git/info/exclude (or .cursor/mcp.json) is never read: on a host with
   fs.protected_hardlinks=0 the run's uid can hardlink a BOSS secret there, and BOSS would copy
   its content into a file the run can read. workspace_perms.read_nofollow refuses st_nlink != 1,
   as the repairs already do.
2. 0604 / 0666 only inside the bound identity's own run directory, which admits no one else
   (<uid>:boss 2770): workspace_perms.shared_mode. Anywhere else (the warm-lineage path, no
   identity) the file is 0600.
3. BOSS's read-only git on a run's tree (git_safety.tree_git) runs AS THE RUN'S UID when one is
   held, pinned to the tree's own .git, never with an excludes file the tree's config names; as
   BOSS's uid it refuses a tree inside the sandbox root that points git at another object store
   (objects/info/alternates), which git has no switch to ignore.

Real git; the run's uid is simulated at the owned_process / sandbox_identity seam. No model, no
database.
"""
from __future__ import annotations

import inspect
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import (build_checkout, git_safety, harness_delivery, owned_process,
                                  runner_dispatch, sandbox, sandbox_identity, verification_target,
                                  workspace_perms)
from boss_engine.services import governed_code_task_service as gcts

OTHER = os.geteuid() + 1


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(path: Path, content: str = "# app\n") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _boss_only(tmp_path) -> Path:
    d = tmp_path.resolve() / "bossdata"
    d.mkdir()
    (d / "secret.key").write_text("secret-line\n")
    os.chmod(d, 0o700)
    return d


def _box(ws: Path, ident=None):
    box = sandbox.EphemeralSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=60,
                                                   sandbox_validation_output_limit_chars=10000),
                                   task_id="crun_test")
    box.path = ws
    box._sandbox = ident
    return box


def _run_dir(tmp_path, mode=0o2770) -> Path:
    d = tmp_path.resolve() / "runs" / "owner-1"
    d.mkdir(parents=True)
    os.chmod(d.parent, 0o711)
    os.chmod(d, mode)
    return d


# ── 1. a hardlink is never read ─────────────────────────────────────────────────────────────

def test_read_nofollow_refuses_a_hardlinked_file(tmp_path):
    ws, boss = _repo(tmp_path.resolve() / "ws"), _boss_only(tmp_path)
    exclude = ws / ".git" / "info" / "exclude"
    exclude.unlink(missing_ok=True)
    os.link(boss / "secret.key", exclude)

    assert workspace_perms.read_nofollow(ws, ".git/info/exclude") is None
    os.unlink(exclude)
    exclude.write_text("mine\n")
    assert workspace_perms.read_nofollow(ws, ".git/info/exclude") == b"mine\n"


def test_a_hardlinked_exclude_is_never_copied_into_a_file_the_run_reads(tmp_path):
    ws, boss = _repo(tmp_path.resolve() / "ws"), _boss_only(tmp_path)
    exclude = ws / ".git" / "info" / "exclude"
    exclude.unlink(missing_ok=True)
    os.link(boss / "secret.key", exclude)
    before = stat.S_IMODE(os.stat(boss / "secret.key").st_mode)

    box = _box(ws)
    box._write_local_excludes()

    assert (boss / "secret.key").read_text() == "secret-line\n"     # not rewritten through
    assert stat.S_IMODE(os.stat(boss / "secret.key").st_mode) == before
    assert os.stat(exclude).st_nlink == 2                            # left alone, never copied


# ── 2. shared modes only inside the run's own 2770 directory ─────────────────────────────────

def test_shared_mode_only_inside_the_bound_run_directory(tmp_path):
    run_dir = _run_dir(tmp_path)
    ident = SimpleNamespace(uid=OTHER, run_dir=run_dir)
    inside = run_dir / "ws"
    inside.mkdir()
    outside = tmp_path.resolve() / "lineage-sandboxes" / "ws"
    outside.mkdir(parents=True)

    assert workspace_perms.shared_mode(inside, 0o604, ident=ident) == 0o604
    assert workspace_perms.shared_mode(outside, 0o604, ident=ident) == 0o600
    assert workspace_perms.shared_mode(run_dir, 0o666, ident=ident) == 0o600
    assert workspace_perms.shared_mode(inside, 0o666) == 0o600       # no identity bound
    os.chmod(run_dir, 0o2777)                                        # others can enter it
    assert workspace_perms.shared_mode(inside, 0o666, ident=ident) == 0o600


def test_the_exclude_is_0666_only_inside_the_run_directory(tmp_path):
    run_dir = _run_dir(tmp_path)
    ident = SimpleNamespace(uid=OTHER, run_dir=run_dir)
    inside = _repo(run_dir / "ws")
    outside = _repo(tmp_path.resolve() / "lineage-sandboxes" / "ws")

    _box(inside, ident)._write_local_excludes()
    _box(outside, ident)._write_local_excludes()

    assert stat.S_IMODE(os.stat(inside / ".git/info/exclude").st_mode) == 0o666
    assert stat.S_IMODE(os.stat(outside / ".git/info/exclude").st_mode) == 0o600


@pytest.mark.parametrize("where, want", [("inside", 0o604), ("outside", 0o600)])
def test_cursor_mcp_json_is_0604_only_inside_the_run_directory(tmp_path, monkeypatch, where, want):
    from boss_engine.services.code_task_mcp import HarnessMcp

    run_dir = _run_dir(tmp_path)
    ident = SimpleNamespace(uid=OTHER, run_dir=run_dir)
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    ws = (run_dir if where == "inside" else tmp_path.resolve() / "elsewhere") / "ws"
    ws.mkdir(parents=True)
    src = tmp_path / "mcp-src.json"
    src.write_text('{"mcpServers": {"docs": {"type": "http", "url": "https://d.example/mcp",'
                   ' "headers": {"Authorization": "Bearer t"}}}}')
    mcp = HarnessMcp(path=str(src), servers=["docs"], disallowed=[], gateway_token="")
    kit = harness_delivery.deliver(engine="cursor", mode="cursor_workspace", mcp=mcp,
                                   workspace=ws, gateway=False)
    try:
        assert stat.S_IMODE(os.stat(ws / ".cursor" / "mcp.json").st_mode) == want
    finally:
        kit.cleanup()


# ── 3. read-only git on a run's tree ─────────────────────────────────────────────────────────

def _as_run_uid(monkeypatch, run_dir: Path):
    """An identity is held: owned_process.run must be the wrapped (sandboxed) spawn."""
    ident = SimpleNamespace(uid=OTHER, run_dir=run_dir)
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)
    seen = []

    def run(argv, *, env=None, cwd=None, **kw):
        seen.append((argv, kw, env))
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                              text=kw.get("text", True), input=kw.get("input"))

    monkeypatch.setattr(owned_process, "run", run)
    return seen


def test_tree_git_runs_as_the_run_uid_pinned_with_nothing_the_tree_configured(tmp_path,
                                                                              monkeypatch):
    run_dir = _run_dir(tmp_path)
    ws = _repo(run_dir / "ws")
    seen = _as_run_uid(monkeypatch, run_dir)

    got = git_safety.tree_git(ws, "rev-parse", "HEAD", owner="run-owner")

    assert got is not None and got.returncode == 0 and len(got.stdout.strip()) == 40
    argv, kw, env = seen[-1]
    assert kw.get("sandboxed") is not False and kw.get("owner") == "run-owner"
    assert "core.excludesFile=/dev/null" in argv and "core.hooksPath=/dev/null" in argv
    assert env["GIT_DIR"] == str(ws / ".git") and env["GIT_WORK_TREE"] == str(ws)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_OPTIONAL_LOCKS"] == "0"


def test_tree_git_refuses_a_dot_git_that_is_a_link(tmp_path):
    real = _repo(tmp_path.resolve() / "real")
    ws = tmp_path.resolve() / "ws"
    ws.mkdir()
    os.symlink(real / ".git", ws / ".git")
    assert git_safety.tree_git(ws, "rev-parse", "HEAD") is None


def test_planted_alternates_never_make_boss_read_another_repository(tmp_path, monkeypatch):
    """A run tree inside the sandbox root whose objects/info/alternates names a BOSS repository:
    as BOSS's uid (no identity held) git would read that repository's objects. Refused — as is
    every unbound read of a sandboxed tree (no check-then-run race)."""
    other = _repo(tmp_path.resolve() / "boss-repo", content="BOSS's own\n")
    secret = _git(other, "rev-parse", "HEAD").stdout.strip()
    run_root = tmp_path.resolve() / "runs"
    ws = _repo(run_root / "owner-1" / "ws")
    (ws / ".git" / "objects" / "info").mkdir(exist_ok=True)
    (ws / ".git" / "objects" / "info" / "alternates").write_text(str(other / ".git" / "objects")
                                                                 + "\n")
    assert _git(ws, "cat-file", "-t", secret).stdout.strip() == "commit"   # what git would do
    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(run_root))
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: None)

    assert git_safety.tree_git(ws, "cat-file", "-t", secret) is None
    assert git_safety.tree_git(ws, "rev-parse", "HEAD") is None
    assert verification_target.checkout_problem(ws, branch="main") is None   # cannot say
    # Held identity: git runs as the run's uid, which cannot read BOSS's repository.
    seen = _as_run_uid(monkeypatch, run_root / "owner-1")
    git_safety.tree_git(ws, "cat-file", "-t", secret, owner="run-owner")
    assert seen and seen[-1][1].get("sandboxed") is not False


def test_alternates_outside_the_sandbox_root_still_work_for_boss_own_trees(tmp_path, monkeypatch):
    other = _repo(tmp_path.resolve() / "cache")
    ws = tmp_path.resolve() / "self-fix"
    _git(tmp_path, "clone", "-q", "--shared", str(other), str(ws))
    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(tmp_path.resolve() / "runs"))
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: None)
    got = git_safety.tree_git(ws, "rev-parse", "HEAD")
    assert got is not None and got.returncode == 0


def test_a_planted_core_excludesfile_is_no_oracle_for_a_boss_file(tmp_path, monkeypatch):
    """core.excludesFile at a BOSS file: as BOSS's uid, git would hide every untracked file
    named like one of its lines, telling the run what the file holds. It is never read, nor is
    a .git/info/exclude that is a link to one."""
    boss = _boss_only(tmp_path)
    ws = _repo(tmp_path.resolve() / "ws")
    _git(ws, "config", "core.excludesFile", str(boss / "secret.key"))
    (ws / "secret-line").write_text("probe")
    (ws / "ignored.log").write_text("x")
    (ws / ".gitignore").write_text("*.log\n")
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: None)

    files = runner_dispatch._git_files(str(ws))
    assert files is not None and "secret-line" in files and "ignored.log" not in files

    exclude = ws / ".git" / "info" / "exclude"
    exclude.unlink(missing_ok=True)
    os.symlink(boss / "secret.key", exclude)
    assert "secret-line" in runner_dispatch._git_files(str(ws))
    os.unlink(exclude)
    exclude.write_text("secret-line\n")                    # the run's own regular exclude counts
    assert "secret-line" not in runner_dispatch._git_files(str(ws))


def test_carry_tree_lists_as_the_run_uid_when_one_is_held(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    src, dst = _repo(run_dir / "src"), _repo(run_dir / "dst")
    (src / "new.txt").write_text("turn output")
    seen = _as_run_uid(monkeypatch, run_dir)

    out = runner_dispatch.carry_tree(str(src), str(dst), owner="run-owner")

    assert (dst / "new.txt").read_text() == "turn output" and out["copied"] >= 1
    assert not src.exists()
    assert seen and all(kw.get("sandboxed") is not False and kw.get("owner") == "run-owner"
                        for argv, kw, _ in seen)
    gits = [argv for argv, _, _ in seen if argv[0] == "git"]
    assert len(gits) == 2 and all("core.excludesFile=/dev/null" in a for a in gits)
    copies = [argv for argv, _, _ in seen if argv[0] != "git"]
    assert len(copies) == 1 and copies[0][1:3] == ["-I", "-c"]


def _forbid_boss_side_writes(monkeypatch):
    """The API process (BOSS's uid) must not create, copy or unlink anything in either tree."""
    def boom(*a, **k):
        raise AssertionError(f"BOSS-side filesystem write: {a[:2]}")

    for name in ("makedirs", "symlink", "unlink", "mkdir", "rename"):   # (os is global: the
        monkeypatch.setattr(runner_dispatch.os, name, boom, raising=False)  # whole process)
    monkeypatch.setattr(runner_dispatch.shutil, "copy2", boom)


def test_carry_tree_never_writes_as_boss_through_a_symlinked_destination_dir(tmp_path,
                                                                              monkeypatch):
    """The #220 review's case: the turn left x/pwn.txt; dst/x is a link to a BOSS 0700 dir.
    The copy runs as the run's uid (here: simulated, the seam must be the sandboxed spawn), never
    as BOSS following the link."""
    run_dir = _run_dir(tmp_path)
    boss = _boss_only(tmp_path)
    src, dst = _repo(run_dir / "src"), _repo(run_dir / "dst")
    (src / "x").mkdir()
    (src / "x" / "pwn.txt").write_text("written by the run uid")
    os.symlink(boss, dst / "x")
    ident = SimpleNamespace(uid=OTHER, run_dir=run_dir)
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)
    spawned = []
    real_run = subprocess.run

    def as_run_uid(argv, *, env=None, cwd=None, **kw):
        spawned.append((argv, kw))
        assert kw.get("sandboxed") is not False and kw.get("owner") == "run-owner"
        if argv[0] == "git":
            return real_run(argv, env=env, cwd=cwd, capture_output=True, text=kw.get("text"))
        # The run's uid could not write into BOSS's 0700 dir: what the helper would report.
        return subprocess.CompletedProcess(argv, 0, '{"copied": 1, "removed": 0}', "")

    monkeypatch.setattr(owned_process, "run", as_run_uid)
    _forbid_boss_side_writes(monkeypatch)
    monkeypatch.setattr(runner_dispatch.shutil, "rmtree", lambda p, **k: None)

    out = runner_dispatch.carry_tree(str(src), str(dst), owner="run-owner")

    assert out == {"copied": 1, "removed": 0}
    assert sorted(p.name for p in boss.iterdir()) == ["secret.key"]
    assert any(argv[0] != "git" for argv, _ in spawned)


def test_carry_tree_never_copies_a_symlinked_source_as_boss(tmp_path, monkeypatch):
    """src/leak is a link to a BOSS secret: BOSS never reads it; the copy runs as the run's uid,
    which recreates the LINK (never the content) in dst."""
    run_dir = _run_dir(tmp_path)
    boss = _boss_only(tmp_path)
    src, dst = _repo(run_dir / "src"), _repo(run_dir / "dst")
    os.symlink(boss / "secret.key", src / "leak")
    _as_run_uid(monkeypatch, run_dir)

    runner_dispatch.carry_tree(str(src), str(dst), owner="run-owner")

    assert (dst / "leak").is_symlink() and os.readlink(dst / "leak") == str(boss / "secret.key")


def test_a_sandboxed_tree_is_never_carried_without_its_identity(tmp_path, monkeypatch):
    run_root = tmp_path.resolve() / "runs"
    boss = _boss_only(tmp_path)
    src, dst = _repo(run_root / "o" / "src"), _repo(run_root / "o" / "dst")
    (src / "x").mkdir()
    (src / "x" / "pwn.txt").write_text("x")
    os.symlink(boss, dst / "x")
    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(run_root))
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: None)
    _forbid_boss_side_writes(monkeypatch)

    assert runner_dispatch.carry_tree(str(src), str(dst)) == {"copied": 0, "removed": 0}
    assert sorted(p.name for p in boss.iterdir()) == ["secret.key"] and src.exists()


def test_a_source_the_current_run_uid_cannot_read_is_kept_and_said(tmp_path, monkeypatch, caplog):
    run_dir = _run_dir(tmp_path)
    src, dst = _repo(run_dir / "src"), _repo(run_dir / "dst")
    ident = SimpleNamespace(uid=OTHER, run_dir=run_dir)              # src is owned by our uid
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: ident)
    monkeypatch.setattr(owned_process, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 128, b"", b"fatal: Permission denied"))

    with caplog.at_level("WARNING", logger="boss.runner_dispatch"):
        assert runner_dispatch.carry_tree(str(src), str(dst), owner="o") == {"copied": 0,
                                                                              "removed": 0}
    assert src.exists()
    assert f"holds uid {OTHER}" in caplog.text and "re-leased after the restart" in caplog.text


def test_every_named_read_only_git_goes_through_tree_git(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    ws = _repo(run_dir / "ws")
    (ws / "app.py").write_text("print(1)\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "app")
    seen = _as_run_uid(monkeypatch, run_dir)

    assert gcts._base_commit_sha(ws, "HEAD")
    assert build_checkout.app_files(ws) == ["app.py"]
    verification_target.checkout_problem(ws, branch="main")
    assert len(seen) >= 3 and all("core.excludesFile=/dev/null" in argv for argv, _, _ in seen)
    for fn in (gcts._base_commit_sha, gcts._ledger_acceptance_check, build_checkout.app_files,
               verification_target.checkout_problem, runner_dispatch._git_files):
        src = inspect.getsource(fn)
        assert "tree_git(" in src and 'subprocess.run(["git"' not in src and "boss_run(" not in src
    run_src = inspect.getsource(gcts)
    count = run_src[run_src.index("def _repo_file_count"):][:900]
    assert "tree_git(" in count and '"ls-files", "-z"' in count
