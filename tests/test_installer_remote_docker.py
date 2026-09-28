"""Remote status must work when Docker access requires passwordless sudo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REMOTE = Path(__file__).resolve().parents[1] / "scripts/install/remote.sh"


def test_remote_status_uses_sudo_docker_when_regular_user_cannot(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ssh.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        'command="${*: -1}"\n'
        'printf "%s\\n" "$command" >> "$BOSS_TEST_SSH_LOG"\n'
        'case "$command" in\n'
        '  *"docker info >/dev/null"*)\n'
        '    [[ "$command" == *"sudo -n docker info"* ]] && exit 0\n'
        '    exit 13 ;;\n'
        '  *"free -m"*) echo 16384 ;;\n'
        '  *"df -BG"*) echo 100 ;;\n'
        '  *"sudo -n docker ps --filter name=boss-community"*) echo "  boss-community-api-1  Up  127.0.0.1:8000" ;;\n'
        '  *".boss-shipped-commit"*) echo fixture-commit ;;\n'
        'esac\n'
    )
    ssh.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "BOSS_TEST_SSH_LOG": str(log),
    }

    result = subprocess.run(
        ["bash", str(REMOTE), "--host", "bossqa@example.test", "status"],
        env=env, text=True, capture_output=True, timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "boss-community-api-1" in result.stdout
    commands = log.read_text()
    assert "docker info >/dev/null" in commands
    assert "sudo -n docker info >/dev/null" in commands
    assert "sudo -n docker ps --filter name=boss-community" in commands
