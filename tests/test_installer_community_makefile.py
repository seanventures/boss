"""Community command targets work in the installer's current Docker session."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MAKEFILE = REPO / "Makefile.community"


def _run_make(tmp_path: Path, *, direct: bool, elevated: bool, target: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    (tmp_path / "Makefile").write_bytes(MAKEFILE.read_bytes())
    (tmp_path / ".env").write_text("BOSS_API_KEY=fixture\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = info ]; then [ "$DIRECT_DOCKER" = 1 ]; exit; fi\n'
        'printf "docker %s\\n" "$*" >> "$DOCKER_CALL_LOG"\n'
    )
    docker.chmod(0o755)
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        '[ "$1" = -n ] && [ "$2" = docker ] || exit 2\n'
        "shift 2\n"
        'if [ "$1" = info ]; then [ "$ELEVATED_DOCKER" = 1 ]; exit; fi\n'
        'printf "sudo -n docker %s\\n" "$*" >> "$DOCKER_CALL_LOG"\n'
    )
    sudo.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        DIRECT_DOCKER="1" if direct else "0",
        ELEVATED_DOCKER="1" if elevated else "0",
        DOCKER_CALL_LOG=str(log),
    )
    result = subprocess.run(
        ["make", "--no-print-directory", "-s", target],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=10, check=False,
    )
    return result, log.read_text().splitlines() if log.exists() else []


@pytest.mark.skipif(shutil.which("make") is None, reason="make is unavailable on this test host")
def test_community_targets_use_direct_docker_when_available(tmp_path: Path) -> None:
    result, calls = _run_make(tmp_path, direct=True, elevated=True, target="status")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "docker compose -f scripts/install/community-compose.yml --env-file .env --profile web ps"
    ]


@pytest.mark.skipif(shutil.which("make") is None, reason="make is unavailable on this test host")
def test_community_metatron_uses_noninteractive_sudo_in_fresh_session(tmp_path: Path) -> None:
    result, calls = _run_make(tmp_path, direct=False, elevated=True, target="metatron")
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0].startswith(
        "sudo -n docker compose -f scripts/install/community-compose.yml --env-file .env exec api "
    )
    assert "python scripts/metatron_tui.py" in calls[0]


@pytest.mark.skipif(shutil.which("make") is None, reason="make is unavailable on this test host")
def test_community_targets_explain_missing_docker_access(tmp_path: Path) -> None:
    result, calls = _run_make(tmp_path, direct=False, elevated=False, target="status")
    assert result.returncode != 0
    assert "Docker is unavailable to this user" in result.stderr
    assert "noninteractive sudo access" in result.stderr
    assert not calls


@pytest.mark.skipif(shutil.which("make") is None, reason="make is unavailable on this test host")
def test_community_help_works_without_docker_access(tmp_path: Path) -> None:
    result, calls = _run_make(tmp_path, direct=False, elevated=False, target="help")
    assert result.returncode == 0, result.stderr
    assert "make metatron" in result.stdout
    assert not calls
