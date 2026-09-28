"""The optional secret store uses the installer's Docker access mode."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install/bootstrap_infisical.sh"


@pytest.mark.parametrize(
    ("use_sudo", "direct_access", "expected_mode"),
    [
        ("1", False, "sudo"),
        ("auto", False, "sudo"),
        ("0", True, "direct"),
        ("auto", True, "direct"),
    ],
)
def test_bootstrap_uses_accessible_docker(
    tmp_path: Path, use_sudo: str, direct_access: bool, expected_mode: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INFISICAL_PORT=8080\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = info ]; then\n'
        '  [ "${BOSS_TEST_SUDO_DOCKER:-0}" = 1 ] || [ "$BOSS_TEST_DIRECT_ACCESS" = 1 ]; exit $?\n'
        "fi\n"
        'printf "%s %s\\n" "${BOSS_TEST_SUDO_DOCKER:-direct}" "$*" >> "$BOSS_TEST_DOCKER_CALLS"\n'
        '[ "$1" = compose ] || exit 91\n'
        'if [ "$BOSS_TEST_DIRECT_ACCESS" != 1 ] && [ "${BOSS_TEST_SUDO_DOCKER:-0}" != 1 ]; then exit 92; fi\n'
        "printf '%s\\n' '{\"token\":\"test-token-value\"}'\n"
    )
    docker.chmod(0o755)
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        '[ "$1" = -n ] && shift\n'
        'BOSS_TEST_SUDO_DOCKER=1 exec "$@"\n'
    )
    sudo.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" -X POST "*) printf \'%s\\n\' \'{"project":{"id":"test-project"}}\' ;;\n'
        "esac\n"
    )
    curl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        BOSS_DOCKER_USE_SUDO=use_sudo,
        BOSS_TEST_DIRECT_ACCESS="1" if direct_access else "0",
        BOSS_TEST_DOCKER_CALLS=str(calls),
        INFISICAL_ADMIN_PASSWORD="test-password",
        COMPOSE_FILE="scripts/install/community-compose.yml",
    )
    result = subprocess.run(
        ["sh", str(SCRIPT), str(env_file)],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    settings = env_file.read_text()
    assert "BOSS_INFISICAL_TOKEN=test-token-value" in settings
    assert "BOSS_INFISICAL_PROJECT_ID=test-project" in settings
    assert "test-token-value" not in result.stdout + result.stderr
    compose_calls = calls.read_text().splitlines()
    assert len(compose_calls) == 1
    assert compose_calls[0].startswith("1 compose" if expected_mode == "sudo" else "direct compose")
    assert " compose --profile secrets -f " in compose_calls[0]


def test_existing_token_does_not_require_docker_access(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BOSS_INFISICAL_TOKEN=already-present\n")
    result = subprocess.run(
        ["sh", str(SCRIPT), str(env_file)],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert "already holds" in result.stderr
