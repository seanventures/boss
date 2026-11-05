"""The next source swap must not wedge on the known sticky mount.

boss-1, 2026-11-05: an update's `rm -rf ~/boss.prev` left `~/boss.prev/.boss/selfsrc`
behind. Docker creates that mount as root, and the container user owns what it
writes there, so `rm` exits "Permission denied". The following update then
stopped at "previous source remains at ~/boss.prev" until somebody moved the
directory by hand.

`ship_source` is the swap every install and update runs. A leftover that is
only that known directory is recovered here, with no sudo and no manual move.
A real previous tree — anything else — still refuses, and is not deleted.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REMOTE_SRC = (ROOT / "scripts" / "install" / "remote.sh").read_text()

FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-p|-l) shift 2 ;;
    -t|-T|-q|-N) shift ;;
    *) break ;;
  esac
done
shift  # the host
exec bash -c "$1"
"""


def _ship(tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the real `ship_source` against a fixture host dir. No SSH, no Docker."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "install").mkdir(parents=True)
    before_dispatch = REMOTE_SRC[:REMOTE_SRC.index('case "$CMD" in')]
    (repo / "scripts" / "install" / "remote_test.sh").write_text(before_dispatch + "ship_source\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("shipped\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/usr/bin/env bash\nexit 1\n")  # no passwordless sudo
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    for path in (sudo, ssh):
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    host_dir = tmp_path / "host" / "boss"
    return subprocess.run(
        ["bash", str(repo / "scripts" / "install" / "remote_test.sh"), "install",
         "--host", "fake-host", "--dir", str(host_dir), "--ref", "HEAD"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=60)


def _lock_selfsrc(host_dir: Path, *, also_boss: bool) -> Path:
    """A selfsrc `rm -rf` cannot delete, the way a container or root owner leaves it.

    No chown on this machine (sudo wants a password). Mode 555 on the directory
    that holds the file is the same failure `rm` prints: Permission denied.
    """
    selfsrc = host_dir.parent / "boss.prev" / ".boss" / "selfsrc"
    selfsrc.mkdir(parents=True)
    # A seeded mirror is nested. Everything under selfsrc is the known mount.
    (selfsrc / "boss.git").mkdir()
    (selfsrc / "boss.git" / "HEAD").write_text("ref: refs/heads/main\n")
    keep = selfsrc / "keep"
    keep.write_text("container-owned\n")
    # The container owns the directory and what it wrote, not only the top
    # inode: a nested dir left at 755 is still deletable by us, which is not
    # the failure this fixture stands in for.
    (selfsrc / "boss.git").chmod(0o555)
    selfsrc.chmod(0o555)
    if also_boss:
        selfsrc.parent.chmod(0o555)
    return keep


def _unlock(host_dir: Path) -> None:
    root = host_dir.parent
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)


@pytest.mark.parametrize("also_boss", [False, True], ids=["container-owned-selfsrc", "root-owned-boss"])
def test_an_unremovable_selfsrc_does_not_block_the_next_swap(tmp_path: Path, also_boss: bool) -> None:
    host_dir = tmp_path / "host" / "boss"
    host_dir.mkdir(parents=True)
    (host_dir / "live.txt").write_text("still the install\n")
    _lock_selfsrc(host_dir, also_boss=also_boss)
    try:
        result = _ship(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "previous source remains" not in result.stdout + result.stderr
        shipped = (host_dir / ".boss-shipped-commit").read_text().strip()
        assert len(shipped) == 40
        # The swap moved the live install aside; it did not delete it to get past the guard.
        assert (host_dir.parent / "boss.prev" / "live.txt").read_text() == "still the install\n"
        # rm could not delete the mount (no sudo). It was renamed off the blocking name.
        stamped = list(host_dir.parent.glob("boss.prev.*"))
        assert len(stamped) == 1
        assert (stamped[0] / ".boss" / "selfsrc" / "keep").read_text() == "container-owned\n"
        assert (stamped[0] / ".boss" / "selfsrc" / "boss.git" / "HEAD").read_text().startswith("ref:")
        assert not (host_dir / ".boss" / "selfsrc" / "keep").exists()
    finally:
        _unlock(host_dir)


def test_an_unexpected_leftover_still_refuses_and_is_not_deleted(tmp_path: Path) -> None:
    host_dir = tmp_path / "host" / "boss"
    host_dir.mkdir(parents=True)
    (host_dir / "live.txt").write_text("still the install\n")
    prev = host_dir.parent / "boss.prev"
    (prev / "apps").mkdir(parents=True)
    (prev / "apps" / "keep.txt").write_text("do not delete\n")
    (prev / ".env").write_text("SECRET=stay\n")
    selfsrc = prev / ".boss" / "selfsrc"
    selfsrc.mkdir(parents=True)
    (selfsrc / "keep").write_text("container-owned\n")

    result = _ship(tmp_path)

    assert result.returncode != 0
    assert "previous source remains" in result.stderr
    assert (prev / "apps" / "keep.txt").read_text() == "do not delete\n"
    assert (prev / ".env").read_text() == "SECRET=stay\n"
    assert (selfsrc / "keep").read_text() == "container-owned\n"
    assert (host_dir / "live.txt").read_text() == "still the install\n"
    assert not (host_dir / ".boss-shipped-commit").exists()
    assert list(host_dir.parent.glob("boss.prev.*")) == []


def test_a_selfsrc_symlink_is_not_treated_as_the_known_mount(tmp_path: Path) -> None:
    host_dir = tmp_path / "host" / "boss"
    host_dir.mkdir(parents=True)
    (host_dir / "live.txt").write_text("still the install\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not follow\n")
    prev = host_dir.parent / "boss.prev"
    prev.mkdir()
    (prev / ".boss").symlink_to(outside, target_is_directory=True)

    result = _ship(tmp_path)

    assert result.returncode != 0
    assert "previous source remains" in result.stderr
    assert (outside / "keep.txt").read_text() == "do not follow\n"
    assert (host_dir / "live.txt").read_text() == "still the install\n"
    assert prev.is_dir()
