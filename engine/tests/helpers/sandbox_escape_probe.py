"""A sandboxed run reads its own tree and nothing of another project's (BOSSAPP-1325).

Owned-container qualification: run INSIDE a disposable container built from the BOSS image, as
the `boss` user, with this checkout's engine on PYTHONPATH — never on a host or a live install
(scripts: `make sandbox-escape-probe`, or the docker line in the PR). It lays out what BOSS writes
the way BOSS writes it (its own functions, its own umask), then asks the real helper
(boss-sandbox-exec) to read each path as a pool uid, a second run's uid and a sealed
(acceptance) uid. Prints one line per probe and exits non-zero on any escape or any legitimate
access that broke.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from boss_engine.services import private_dirs, sandbox_identity
from boss_engine.services.knowledge.repo_index import kg_index_root
from boss_engine.tools import builtin

HELPER = sandbox_identity.DEFAULT_HELPER


def as_uid(uid: int, script: str, cwd: str = "/", env: dict | None = None) -> bool:
    """`script` as `uid` through the real helper, with the environment a run's process gets
    (SandboxIdentity.env) when one is given."""
    proc = subprocess.run([HELPER, "run", str(uid), "--", "/bin/sh", "-c", script],
                          cwd=cwd, capture_output=True, text=True, timeout=30, env=env)
    if proc.returncode != 0 and os.environ.get("PROBE_VERBOSE"):
        print(f"  [{uid}] {script[:80]}: {proc.stderr.strip()[:300]}")
    return proc.returncode == 0


def can_read(uid: int, path: Path) -> bool:
    """Whether `uid` can read the CONTENT: a file's first byte (`head -c1`), a directory's
    listing. Never `ls` of a file, which needs only the parent's search bit (BOSSAPP-1327)."""
    if path.is_dir():
        return as_uid(uid, f"ls -A '{path}' >/dev/null 2>&1")
    return as_uid(uid, f"head -c1 '{path}' >/dev/null 2>&1")


def plant(directory: Path, name: str = "SECRET") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("another project's code\n")
    return f


def main() -> int:
    assert Path("/.dockerenv").exists(), "only inside a disposable container"
    assert os.getuid() != 0 and Path(HELPER).exists()
    os.umask(0o022)                                   # the control plane's own umask
    os.environ.setdefault(sandbox_identity.MODE_ENV, "required")

    # What BOSS writes, written by BOSS's own code.
    kg = plant(kg_index_root() / "ghrepo_other_project")
    cache = plant(Path(builtin._repo_cache()) / "acme__other")          # noqa: SLF001
    mirror = plant(Path("/var/lib/boss/repos/other/other.git"))
    private_import = Path(private_dirs.private_root()) / "import"
    a = sandbox_identity.acquire("probe-run-a", seed_logins=False)
    b = sandbox_identity.acquire("probe-run-b", seed_logins=False)
    assert a is not None and b is not None and a.uid != b.uid
    other_run = plant(b.run_dir / "checkout")
    own_tree = a.workspace("crun_probe-")
    plant(own_tree, "main.go")
    sealed_uid = sandbox_identity.SEALED_BASE + 1

    results: list[tuple[str, bool, bool]] = []          # (what, expected readable, readable)

    def check(what: str, uid: int, path: Path, expect: bool) -> None:
        results.append((what, expect, can_read(uid, path)))

    for who, uid in (("pool uid", a.uid), ("sealed uid", sealed_uid)):
        check(f"{who} -> another run's directory", uid, other_run, False)
        check(f"{who} -> KG index clone", uid, kg, False)
        check(f"{who} -> chat repo cache", uid, cache, False)
        check(f"{who} -> repo mirror", uid, mirror, False)
        check(f"{who} -> private imports", uid, private_import, False)
        check(f"{who} -> BOSS home", uid, Path("/home/boss"), False)
        check(f"{who} -> identity keys", uid, Path("/var/lib/boss/identity"), False)
    # What a run must still do.
    check("pool uid -> its own run directory", a.uid, own_tree / "main.go", True)
    results.append(("pool uid -> writes in its own tree", True,
                    as_uid(a.uid, f"echo ok > '{own_tree}/written.txt'")))
    results.append(("pool uid -> git in its own worktree", True, as_uid(
        a.uid, f"cd '{own_tree}' && git init -q . && git add -A && "
               "git -c user.name=p -c user.email=p@x commit -qm probe", cwd=str(own_tree),
        env=a.env(None))))
    results.append(("pool uid -> preview server binds its port", True, as_uid(
        a.uid, "python3 -c \"import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); "
               "s.listen(1); s.close()\"")))
    results.append(("BOSS -> writes the KG index", True, os.access(kg.parent, os.W_OK)))
    for d in (kg_index_root(), Path(builtin._repo_cache()), private_dirs.private_root()):  # noqa: SLF001
        results.append((f"BOSS-only mode on {d}", True, private_dirs.is_private(d)))

    bad = 0
    for what, expect, got in results:
        ok = expect == got
        bad += not ok
        wanted = "readable" if expect else "denied"
        note = "" if ok else f" (expected {wanted})"
        print(f"{'ok  ' if ok else 'FAIL'}  {what}: {'readable' if got else 'denied'}{note}")
    sandbox_identity.release("probe-run-a")
    sandbox_identity.release("probe-run-b")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
