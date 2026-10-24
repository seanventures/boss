"""backup.sh's record of what commit an archive-only install is at (BOSSAPP-604).

remote.sh now writes the full 40-hex commit to `.boss-shipped-commit`, but an install made
before that fix (or one whose marker is, for some other reason, short or a tag's own object
hash rather than the commit it points at) still needs a sane backup record: resolve it when
a Git checkout is here to resolve it against, otherwise refuse with words that say how to fix
it — never silently copy an ambiguous value into the backup's own `source-commit` file.

Runs the exact `if/else` block backup.sh uses (extracted verbatim, with INSTALL_DIR and
BACKUP set), so this is the real script's logic, not a reimplementation of it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKUP_SRC = (ROOT / "scripts" / "install" / "backup.sh").read_text()
BLOCK = BACKUP_SRC[BACKUP_SRC.index('if [[ -d "$INSTALL_DIR/.git"'):
                   BACKUP_SRC.index("\nfi\n", BACKUP_SRC.index('if [[ -d "$INSTALL_DIR/.git"')) + 3]
assert BLOCK.startswith('if [[ -d "$INSTALL_DIR/.git"') and "source-commit" in BLOCK


def _run(tmp_path: Path, *, has_dot_git: bool, marker: str | None) -> subprocess.CompletedProcess:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()
    if has_dot_git:
        subprocess.run(["git", "init", "-q"], cwd=install_dir, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=install_dir,
                       check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=install_dir, check=True)
        (install_dir / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=install_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=install_dir, check=True)
    if marker is not None:
        (install_dir / ".boss-shipped-commit").write_text(marker)
    script = tmp_path / "block.sh"
    script.write_text(f"set -euo pipefail\nINSTALL_DIR={install_dir}\nBACKUP={backup}\n{BLOCK}\n")
    result = subprocess.run(["bash", str(script)], text=True, capture_output=True, timeout=30)
    result.source_commit_path = backup / "source-commit"
    return result


def test_a_full_sha_marker_passes_through(tmp_path: Path) -> None:
    sha = "a" * 40
    result = _run(tmp_path, has_dot_git=False, marker=sha)
    assert result.returncode == 0, result.stderr
    assert result.source_commit_path.read_text().strip() == sha


def test_a_git_checkout_resolves_head_directly(tmp_path: Path) -> None:
    result = _run(tmp_path, has_dot_git=True, marker=None)
    assert result.returncode == 0, result.stderr
    got = result.source_commit_path.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", got)


def test_an_unresolvable_marker_refuses_with_a_fix_it_message(tmp_path: Path) -> None:
    # A short sha, or a tag's own object hash — either way, not 40 hex chars, and with no
    # Git checkout here, nothing can resolve it.
    result = _run(tmp_path, has_dot_git=False, marker="a1b2c3d")
    assert result.returncode != 0
    assert "remote.sh update" in result.stderr or "remote.sh update" in result.stdout
    assert not result.source_commit_path.exists()


def test_a_missing_marker_also_refuses_rather_than_write_garbage(tmp_path: Path) -> None:
    result = _run(tmp_path, has_dot_git=False, marker=None)
    assert result.returncode != 0
    assert not result.source_commit_path.exists()
