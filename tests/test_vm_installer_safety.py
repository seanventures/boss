"""The public bootstrap must not destroy an existing VM directory or checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"


def _run(tmp_path: Path, install_dir: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1:$2" in\n'
        "  info:|compose:version) echo 'Docker Compose version v2'; exit 0 ;;\n"
        "esac\n"
        "exit 88\n"
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--yes",
            "--skip-model-key",
            "--no-install-deps",
            "--dir",
            str(install_dir),
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
    )


def test_nonempty_non_git_directory_is_not_removed(tmp_path: Path) -> None:
    install_dir = tmp_path / "boss"
    install_dir.mkdir()
    marker = install_dir / "operator-data.txt"
    marker.write_text("keep this")

    result = _run(tmp_path, install_dir)

    assert result.returncode != 0
    assert "not empty and is not a Git checkout" in result.stdout
    assert marker.read_text() == "keep this"


def test_dirty_boss_checkout_is_not_updated(tmp_path: Path) -> None:
    install_dir = tmp_path / "boss"
    (install_dir / "scripts" / "install").mkdir(parents=True)
    (install_dir / "scripts" / "install" / "community-compose.yml").write_text("services: {}\n")
    tracked = install_dir / "install.sh"
    tracked.write_text("before\n")
    subprocess.run(["git", "init", "-q", str(install_dir)], check=True)
    subprocess.run(["git", "-C", str(install_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(install_dir), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "base"],
        check=True,
    )
    tracked.write_text("operator edit\n")

    result = _run(tmp_path, install_dir)

    assert result.returncode != 0
    assert "tracked local changes" in result.stdout
    assert tracked.read_text() == "operator edit\n"


def test_public_bind_option_is_rejected_before_any_install_action() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL), "--bind-address", "0.0.0.0"],
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode != 0
    assert "Community binds API/web to loopback" in result.stderr


def test_a_tree_shipped_by_remote_sh_is_used_not_refused(tmp_path: Path) -> None:
    """`remote.sh install` copies the laptop's commit into --dir, stamps .boss-shipped-commit and
    runs install.sh from inside it. Refusing it as "not a Git checkout" broke the documented
    remote install on every fresh host (found installing on a new VM, 2026-09-29)."""
    install_dir = tmp_path / "boss"
    (install_dir / "scripts" / "install").mkdir(parents=True)
    (install_dir / "scripts" / "install" / "community-compose.yml").write_text("services: {}\n")
    (install_dir / ".boss-shipped-commit").write_text("020f0e29\n")
    kept = install_dir / "operator-data.txt"
    kept.write_text("keep this")

    result = _run(tmp_path, install_dir)

    assert "not a Git checkout" not in result.stdout
    assert "Using the source remote.sh shipped (020f0e29" in result.stdout
    assert "Source ready" in result.stdout
    assert kept.read_text() == "keep this"


def test_the_marker_alone_does_not_make_any_directory_a_boss_tree(tmp_path: Path) -> None:
    """Without the compose file it is not a BOSS tree, marker or not, and is left alone."""
    install_dir = tmp_path / "boss"
    install_dir.mkdir()
    (install_dir / ".boss-shipped-commit").write_text("020f0e29\n")
    (install_dir / "operator-data.txt").write_text("keep this")

    result = _run(tmp_path, install_dir)

    assert result.returncode != 0
    assert "not empty and is not a Git checkout" in result.stdout
