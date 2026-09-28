"""A fresh VM user may need sudo to inspect a healthy Docker stack."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


CHECKUP = Path(__file__).resolve().parents[1] / "scripts/install/checkup.sh"


def _run_checkup(
    tmp_path: Path, *, sudo_allowed: bool, ssh_connection: bool = False, public_url: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s %s\\n" "${BOSS_TEST_DOCKER_ALLOWED:-0}" "$*" >> "$BOSS_TEST_LOG"\n'
        '[[ "${BOSS_TEST_DOCKER_ALLOWED:-0}" == 1 ]] || exit 13\n'
        'case "$1" in\n'
        '  info) exit 0 ;;\n'
        '  ps) case "$*" in\n'
        '    *name=boss-community-db-*) echo boss-community-db-1 ;;\n'
        '    *name=boss-community-api-*) echo boss-community-api-1 ;;\n'
        '    *name=boss-community-worker-*) echo boss-community-worker-1 ;;\n'
        '    *name=boss-community*) printf "%s\\n" boss-community-db-1 boss-community-api-1 boss-community-worker-1 boss-community-web-1 ;;\n'
        '  esac ;;\n'
        '  exec) echo http://127.0.0.1:8000 ;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == -n ]] || exit 90\n'
        'shift\n'
        '[[ "${BOSS_TEST_SUDO_ALLOWED:-0}" == 1 ]] || exit 1\n'
        'BOSS_TEST_DOCKER_ALLOWED=1 exec "$@"\n'
    )
    sudo.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'url="${*: -1}"\n'
        'case "$url" in\n'
        '  */health) exit 0 ;;\n'
        '  */v1/settings*) echo "{\\"connected_engines\\":[]}" ;;\n'
        '  */host-advice) echo "{\\"headline\\":\\"\\"}" ;;\n'
        '  */setup/status*) echo "{\\"github\\":{\\"done\\":false}}" ;;\n'
        '  *) exit 22 ;;\n'
        'esac\n'
    )
    curl.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("BOSS_API_KEY=fixture\nBOSS_DEFAULT_PROJECT_ID=boss-dogfood\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "BOSS_ENV_FILE": str(env_file),
        "BOSS_TEST_LOG": str(log),
        "BOSS_TEST_SUDO_ALLOWED": "1" if sudo_allowed else "0",
    }
    if ssh_connection:
        env["SSH_CONNECTION"] = "client 12345 server 22"
    if public_url:
        env["BOSS_PUBLIC_URL"] = "https://boss.example.test"
    result = subprocess.run(
        ["bash", str(CHECKUP)], env=env, text=True, capture_output=True, timeout=20,
    )
    return result, log.read_text()


def test_checkup_uses_passwordless_sudo_for_all_docker_reads(tmp_path: Path) -> None:
    result, commands = _run_checkup(tmp_path, sudo_allowed=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "containers: 4 running" in result.stdout
    assert "BOSS is ready." in result.stdout
    assert "0 info" in commands and "1 info" in commands
    assert "1 ps" in commands and "1 exec" in commands
    assert "0 ps" not in commands and "0 exec" not in commands


def test_checkup_reports_docker_access_problem_without_fake_missing_services(tmp_path: Path) -> None:
    result, commands = _run_checkup(tmp_path, sudo_allowed=False)

    assert result.returncode == 1
    assert "Docker is unavailable" in result.stdout
    assert "containers: missing" not in result.stdout
    assert " ps " not in commands and " exec " not in commands


def test_ssh_loopback_console_requires_tunnel_without_failing_host_checkup(tmp_path: Path) -> None:
    result, _ = _run_checkup(tmp_path, sudo_allowed=True, ssh_connection=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "forward both console :3000 and API :8000 over SSH" in result.stdout
    assert "BOSS is ready." in result.stdout


def test_public_url_cannot_use_loopback_console_api(tmp_path: Path) -> None:
    result, _ = _run_checkup(tmp_path, sudo_allowed=True, public_url=True)

    assert result.returncode == 1
    assert "Rebuild web with --public-url" in result.stdout
