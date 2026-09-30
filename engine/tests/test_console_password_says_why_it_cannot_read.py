"""console_password.py says WHY it cannot read .env — missing is not a symlink.

Run on a laptop checkout where BOSS was never installed, it answered ".env must be a regular
.env file, not a symlink" about a file that did not exist, and the operator went looking for a
symlink. The usual cause is the wrong machine; the message now says so (2026-09-30).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install" / "console_password.py"


def _show(env_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "show", "--env-file", str(env_file)],
                          capture_output=True, text=True, timeout=30)


def test_a_missing_env_points_at_the_server_not_at_a_symlink(tmp_path: Path) -> None:
    result = _show(tmp_path / ".env")
    assert result.returncode != 0
    assert "symlink" not in result.stderr
    assert "No BOSS install here" in result.stderr
    assert "ssh user@your-server" in result.stderr


def test_a_symlinked_env_is_still_refused_as_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.env"
    real.write_text("BOSS_CONSOLE_PASSWORD=x\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    result = _show(link)
    assert result.returncode != 0
    assert "not a symlink" in result.stderr
