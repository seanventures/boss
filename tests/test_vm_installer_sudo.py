"""The piped VM installer uses the permitted command, not sudo credential validation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"


def _vm_commands(tmp_path: Path, *, allow_admin: bool, direct_docker: bool) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sudo.log"
    apt_installed = tmp_path / "apt-installed"
    for name, body in {
        "uname": 'if [ "$1" = -m ]; then echo x86_64; else echo Linux; fi',
        "id": 'if [ "$1" = -u ]; then echo 1000; else exit 2; fi',
        "apt-get": "exit 2",
        "dpkg": "exit 2",
        "systemctl": "exit 2",
        "docker": (
            'case "$*" in\n'
            '  "compose version") [ -f "$APT_INSTALLED" ] && echo "Docker Compose version v2" ;;\n'
            '  info) [ "$DIRECT_DOCKER" = 1 ] ;;\n'
            '  *) exit 2 ;;\n'
            "esac"
        ),
        "sudo": (
            'printf "%s\\n" "$*" >> "$SUDO_CALL_LOG"\n'
            '[ "$1" = -n ] && [ "$ALLOW_ADMIN" = 1 ] || exit 42\n'
            'shift\n'
            'case "$*" in\n'
            '  "env DEBIAN_FRONTEND=noninteractive apt-get update -qq") exit 0 ;;\n'
            '  "env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git make openssl python3")\n'
            '    touch "$APT_INSTALLED"; exit 0 ;;\n'
            '  "systemctl enable --now docker"|"docker info") exit 0 ;;\n'
            '  "docker compose version") echo "Docker Compose version v2"; exit 0 ;;\n'
            'esac\n'
            'exit 42'
        ),
    }.items():
        command = bin_dir / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        HOME=str(tmp_path),
        SUDO_CALL_LOG=str(log),
        APT_INSTALLED=str(apt_installed),
        ALLOW_ADMIN="1" if allow_admin else "0",
        DIRECT_DOCKER="1" if direct_docker else "0",
    )
    return env, log, apt_installed


def _dirty_install_dir(tmp_path: Path) -> Path:
    install_dir = tmp_path / "boss"
    install_dir.mkdir()
    (install_dir / "operator-data.txt").write_text("keep this")
    return install_dir


def _run(script: Path, install_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--yes", "--skip-model-key", "--no-web", "--dir", str(install_dir)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
    )


@pytest.mark.parametrize("allow_admin", [True, False])
def test_vm_dependencies_use_noninteractive_package_command(tmp_path: Path, allow_admin: bool) -> None:
    env, log, apt_installed = _vm_commands(tmp_path, allow_admin=allow_admin, direct_docker=True)
    install_dir = _dirty_install_dir(tmp_path)
    distro = tmp_path / "os-release"
    distro.write_text("ID=ubuntu\nVERSION_ID=24.04\nPRETTY_NAME='Ubuntu 24.04.5 LTS'\n")
    # Substitute only the distro probe so this test can exercise the real Linux
    # dependency branch on macOS too, without touching the host's /etc.
    source = INSTALL.read_text()
    assert source.count("-f /etc/os-release") == 1
    assert source.count(". /etc/os-release") == 1
    source = source.replace("-f /etc/os-release", f"-f {distro}")
    source = source.replace(". /etc/os-release", f". {distro}")
    test_installer = tmp_path / "install.sh"
    test_installer.write_text(source)

    result = _run(test_installer, install_dir, env)

    calls = log.read_text().splitlines()
    assert calls[0] == "-n env DEBIAN_FRONTEND=noninteractive apt-get update -qq"
    assert all(not call.startswith("-v") for call in calls)
    if allow_admin:
        assert apt_installed.exists()
        assert "-n env DEBIAN_FRONTEND=noninteractive apt-get install -y" in calls[1]
        assert "not empty and is not a Git checkout" in result.stdout
    else:
        assert not apt_installed.exists()
        assert len(calls) == 1
        assert "Could not update VM packages" in result.stdout
    assert result.returncode != 0
    assert (install_dir / "operator-data.txt").read_text() == "keep this"


def test_vm_docker_access_works_when_sudo_validation_would_fail(tmp_path: Path) -> None:
    env, log, apt_installed = _vm_commands(tmp_path, allow_admin=True, direct_docker=False)
    apt_installed.touch()
    install_dir = _dirty_install_dir(tmp_path)

    result = _run(INSTALL, install_dir, env)

    assert "Docker daemon is running" in result.stdout
    assert "not empty and is not a Git checkout" in result.stdout
    assert log.read_text().splitlines() == ["-n docker info", "-n docker compose version", "-n docker compose version"]
    assert (install_dir / "operator-data.txt").read_text() == "keep this"
