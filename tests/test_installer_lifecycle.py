"""Lifecycle scripts must retain an existing install unless purge is explicit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNINSTALL = REPO / "scripts/install/uninstall.sh"
UPGRADE = REPO / "scripts/install/upgrade.sh"
INSTALLER = REPO / "install.sh"


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    install = tmp_path / "boss"
    (install / "scripts/install").mkdir(parents=True)
    (install / "scripts/install/community-compose.yml").write_text("name: boss-lifecycle-test\nservices: {}\n")
    (install / ".env").write_text("BOSS_DB_PASSWORD=old-db\nBOSS_API_KEY=old-api\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = info ]; then exit 0; fi\n'
        f'printf "%s\\n" "$*" >> "{log}"\n'
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return install, env, log


def _run(script: Path, install: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--dir", str(install), *args],
        env=env, text=True, capture_output=True, timeout=20, check=False,
    )


def test_dry_run_and_safe_uninstall_preserve_existing_env(tmp_path: Path) -> None:
    install, env, log = _fixture(tmp_path)
    initial = (install / ".env").read_bytes()

    dry = _run(UNINSTALL, install, env, "--dry-run")
    assert dry.returncode == 0
    assert not log.exists()
    assert (install / ".env").read_bytes() == initial

    for _ in range(2):  # a rerun must have the same safe contract
        result = _run(UNINSTALL, install, env, "--yes")
        assert result.returncode == 0, result.stderr
        assert (install / ".env").read_bytes() == initial
    commands = log.read_text().splitlines()
    assert len(commands) == 2
    assert all("down --remove-orphans" in command for command in commands)
    assert all("--volumes" not in command for command in commands)


def test_purge_requires_confirmation_even_with_yes(tmp_path: Path) -> None:
    install, env, log = _fixture(tmp_path)
    result = _run(UNINSTALL, install, env, "--yes", "--purge-data", "--confirm-purge", "wrong")
    assert result.returncode != 0
    assert (install / ".env").exists()
    assert not log.exists()


def test_failed_backup_cancels_purge_before_compose_down(tmp_path: Path) -> None:
    install, env, log = _fixture(tmp_path)
    backup = install / "scripts/install/backup.sh"
    backup.write_text("#!/bin/sh\nexit 9\n")
    result = _run(
        UNINSTALL, install, env, "--purge-data", "--confirm-purge", "DELETE-BOSS-DATA",
    )
    assert result.returncode != 0
    assert "Backup failed" in result.stderr
    assert (install / ".env").exists()
    assert not log.exists()


def test_explicit_purge_deletes_env_only_after_compose_succeeds(tmp_path: Path) -> None:
    install, env, log = _fixture(tmp_path)
    result = _run(
        UNINSTALL, install, env, "--purge-data", "--skip-backup",
        "--confirm-purge", "DELETE-BOSS-DATA",
    )
    assert result.returncode == 0, result.stderr
    assert "down --volumes --remove-orphans" in log.read_text()
    assert not (install / ".env").exists()


def test_upgrade_rejects_dirty_checkout_before_fetch_or_backup(tmp_path: Path) -> None:
    install, env, _ = _fixture(tmp_path)
    (install / "scripts/install/backup.sh").write_text("#!/bin/sh\nexit 0\n")
    (install / "install.sh").write_text("initial\n")
    subprocess.run(["git", "init", "-q", str(install)], check=True)
    subprocess.run(["git", "-C", str(install), "add", "scripts", "install.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(install), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "initial"],
        check=True,
    )
    (install / "install.sh").write_text("operator change\n")

    result = _run(UPGRADE, install, env)
    assert result.returncode != 0
    assert "Tracked local changes" in result.stderr
    assert (install / "install.sh").read_text() == "operator change\n"


def test_upgrade_routes_archive_install_to_remote_update(tmp_path: Path) -> None:
    install, env, _ = _fixture(tmp_path)
    (install / "scripts/install/backup.sh").write_text("#!/bin/sh\nexit 0\n")
    (install / ".boss-shipped-commit").write_text("abcdef12\n")
    result = _run(UPGRADE, install, env)
    assert result.returncode != 0
    assert "remote.sh archive" in result.stderr
    assert (install / ".env").exists()


def test_upgrade_backup_failure_leaves_checkout_and_env_unchanged(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "scripts/install").mkdir(parents=True)
    (upstream / "scripts/install/community-compose.yml").write_text("name: proof\nservices: {}\n")
    (upstream / "scripts/install/backup.sh").write_text("#!/bin/sh\nexit 7\n")
    (upstream / "install.sh").write_text("echo old\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "old"],
        check=True,
    )
    install = tmp_path / "boss"
    subprocess.run(["git", "clone", "-q", str(upstream), str(install)], check=True)
    (install / ".env").write_text("BOSS_API_KEY=keep\n")
    old_sha = subprocess.check_output(["git", "-C", str(install), "rev-parse", "HEAD"], text=True).strip()
    (upstream / "install.sh").write_text("echo new\n")
    subprocess.run(["git", "-C", str(upstream), "add", "install.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "new"],
        check=True,
    )

    result = _run(UPGRADE, install, os.environ.copy())
    assert result.returncode != 0
    assert "Backup failed" in result.stderr
    assert subprocess.check_output(["git", "-C", str(install), "rev-parse", "HEAD"], text=True).strip() == old_sha
    assert (install / "install.sh").read_text() == "echo old\n"
    assert (install / ".env").read_text() == "BOSS_API_KEY=keep\n"


def test_installer_rerun_preserves_existing_credentials(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "scripts/install").mkdir(parents=True)
    for filename in ("community-compose.yml", "restore_preserved_env.sh", "gen_infisical_keys.sh"):
        source = REPO / "scripts/install" / filename
        target = upstream / "scripts/install" / filename
        target.write_bytes(source.read_bytes())
        target.chmod(source.stat().st_mode)
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture"],
        check=True,
    )
    install = tmp_path / "boss"
    subprocess.run(["git", "clone", "-q", str(upstream), str(install)], check=True)
    env_file = install / ".env"
    env_file.write_text(
        "BOSS_DB_PASSWORD=existing-db\nBOSS_API_KEY=existing-api\n"
        "BOSS_CONSOLE_PASSWORD=existing-console-password-1234567890\n"
        "BOSS_CONSOLE_SESSION_SECRET=existing-console-session-secret-1234567890\n"
        "BOSS_CUSTOM_OPERATOR_SETTING=retain\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        "  compose) if [ \"$2\" = version ]; then echo 'Docker Compose version v2'; fi; exit 0 ;;\n"
        "esac\n"
        "exit 1\n"
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "out=; code=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then out="$2"; shift 2; continue; fi\n'
        '  if [ "$1" = -w ]; then code=200; shift 2; continue; fi\n'
        "  shift\n"
        "done\n"
        'if [ -n "$out" ] && [ "$out" != /dev/null ]; then printf \'{"edition":"community"}\' > "$out"; fi\n'
        'if [ -n "$code" ]; then printf "%s" "$code"; fi\n'
    )
    curl.chmod(0o755)
    test_env = os.environ.copy()
    test_env["PATH"] = f"{bin_dir}:{test_env['PATH']}"
    for _ in range(2):
        result = _run(
            INSTALLER, install, test_env,
            "--yes", "--skip-model-key", "--no-web", "--no-install-deps",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        lines = env_file.read_text().splitlines()
        for retained in (
            "BOSS_DB_PASSWORD=existing-db", "BOSS_API_KEY=existing-api",
            "BOSS_CONSOLE_PASSWORD=existing-console-password-1234567890",
            "BOSS_CONSOLE_SESSION_SECRET=existing-console-session-secret-1234567890",
            "BOSS_CUSTOM_OPERATOR_SETTING=retain",
        ):
            assert retained in lines
