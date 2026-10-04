"""NOTHING INSTALLED IS EVER COMMITTED.

On 2026-10-04 a worktree's `node_modules` symlink (pointing at the main checkout's install) was
committed by accident. Merging it replaced the main checkout's real `node_modules` with a symlink
to itself — every test run on every worktree then failed. `.gitignore` said `node_modules/`, which
matches a directory but not a symlink of that name.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_node_modules_or_venv_is_tracked():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.splitlines()
    bad = [p for p in tracked
           if any(part in ("node_modules", ".venv", "venv") for part in Path(p).parts)]
    assert bad == [], f"installed dependencies are tracked: {bad[:10]}"


def test_the_ignore_rule_also_matches_a_symlink():
    rules = (ROOT / ".gitignore").read_text().splitlines()
    assert "node_modules" in rules, "`node_modules/` only matches a directory; a symlink slips through"
