"""boss-1, 2026-11-05: an update failed with "previous source remains at ~/boss.prev".

Docker creates the read-only mount point `.boss/selfsrc` as root, so the plain `rm -rf
$DIR.prev` after every good update failed, and the NEXT update refused to swap. remote.sh's
clear_prev removes the tree, else (no permission, no sudo) moves it aside with a stamp — the
way is always clear.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "scripts" / "install" / "remote.sh"


def _clear_prev(tmp_path: Path) -> subprocess.CompletedProcess:
    body = re.search(r"^clear_prev\(\) \{.*?^\}", REMOTE.read_text(), re.S | re.M)
    assert body, "remote.sh has no clear_prev()"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/usr/bin/env bash\nexit 1\n")      # no passwordless sudo here
    sudo.chmod(sudo.stat().st_mode | stat.S_IEXEC)
    script = f'rsh() {{ bash -c "$*"; }}\nDIR={tmp_path / "boss"}\n{body.group(0)}\nclear_prev\n'
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=30,
                          env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})


def test_a_removable_previous_tree_is_removed(tmp_path: Path) -> None:
    (tmp_path / "boss.prev" / "apps").mkdir(parents=True)
    result = _clear_prev(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "boss.prev").exists()
    assert not list(tmp_path.glob("boss.prev.*"))


def test_a_tree_holding_an_unremovable_mount_point_is_moved_aside(tmp_path: Path) -> None:
    locked = tmp_path / "boss.prev" / ".boss"
    (locked / "selfsrc").mkdir(parents=True)
    locked.chmod(0o555)                                   # like the root-owned .boss
    try:
        result = _clear_prev(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "boss.prev").exists(), "the next update would refuse to swap"
        assert len(list(tmp_path.glob("boss.prev.*"))) == 1
    finally:
        for p in tmp_path.glob("boss.prev*/.boss"):
            p.chmod(0o755)
