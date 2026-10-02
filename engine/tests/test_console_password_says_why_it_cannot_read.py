"""console_password.py says WHY it cannot read .env — missing is not a symlink.

Run on a laptop checkout where BOSS was never installed, it answered ".env must be a regular
.env file, not a symlink" about a file that did not exist, and the operator went looking for a
symlink. The usual cause is the wrong machine; the message now says so (2026-09-30).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install" / "console_password.py"


def _show(env_file: Path, home: Path) -> subprocess.CompletedProcess:
    # HOME is the test's own: the script looks for an install at ~/boss, and the machine running
    # the tests may well have one.
    return subprocess.run([sys.executable, str(SCRIPT), "show", "--env-file", str(env_file)],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "HOME": str(home)})


def test_a_missing_env_points_at_the_server_not_at_a_symlink(tmp_path: Path) -> None:
    result = _show(tmp_path / ".env", tmp_path / "home")
    assert result.returncode != 0
    assert "symlink" not in result.stderr
    assert "No BOSS install here" in result.stderr
    assert "ssh -t user@your-server" in result.stderr


def test_a_symlinked_env_is_still_refused_as_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.env"
    real.write_text("BOSS_CONSOLE_PASSWORD=x\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    result = _show(link, tmp_path / "home")
    assert result.returncode != 0
    assert "not a symlink" in result.stderr


def test_the_wrong_directory_on_the_right_machine_points_at_the_install(tmp_path: Path) -> None:
    """Run from a development checkout with BOSS installed beside it in ~/boss — the commonest
    case — it named the server and SSH instead of the install sitting right there."""
    home = tmp_path / "home"
    (home / "boss").mkdir(parents=True)
    (home / "boss" / ".env").write_text("BOSS_CONSOLE_PASSWORD=x\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    result = _show(checkout / ".env", home)
    assert result.returncode != 0
    assert f"there is one at {home / 'boss'}" in result.stderr


def test_refusing_a_pipe_says_how_to_get_a_terminal_over_ssh(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BOSS_CONSOLE_PASSWORD=x\n")
    env.chmod(0o600)  # the script reads only a private .env
    result = _show(env, tmp_path / "home")  # capture_output: stdout is a pipe, not a terminal
    assert result.returncode != 0
    assert "outside a terminal" in result.stderr and "ssh -t" in result.stderr
