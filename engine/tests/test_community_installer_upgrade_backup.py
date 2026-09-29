"""A pasted installer rerun must leave an older Community install recoverable."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def _run(*args: str | Path, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, text=True,
        capture_output=True, check=False, timeout=60,
    )


def _git(*args: str | Path, cwd: Path) -> str:
    result = _run("git", *args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def install_pair(tmp_path: Path) -> tuple[Path, str, str, dict[str, str], Path]:
    """Two local commits plus fake Docker; no daemon, network, or provider call."""
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    origin.mkdir()
    source.mkdir()
    _git("init", "--bare", cwd=origin)
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "BOSS installer test", cwd=source)
    _git("config", "user.email", "installer-test@example.invalid", cwd=source)
    _git("remote", "add", "origin", origin, cwd=source)
    (source / "scripts/install").mkdir(parents=True)
    (source / "scripts/install/community-compose.yml").write_text("services: {}\n")
    (source / "install.sh").write_bytes((REPO / "install.sh").read_bytes())
    for helper in ("restore_preserved_env.sh", "reconcile_api_credentials.py"):
        (source / "scripts/install" / helper).write_bytes(
            (REPO / "scripts/install" / helper).read_bytes()
        )
    (source / "version").write_text("old\n")
    backup = source / "scripts/install/backup.sh"
    backup.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "[[ $1 == --dir ]]\n"
        "dir=$2\n"
        "git -C \"$dir\" rev-parse HEAD >> \"$BOSS_TEST_BACKUP_TRACE\"\n"
        "[[ ${BOSS_TEST_BACKUP_FAIL:-0} != 1 ]] || exit 19\n"
        "mkdir -p \"$BOSS_TEST_BACKUP_DIR\"\n"
        "cp \"$dir/.env\" \"$BOSS_TEST_BACKUP_DIR/.env\"\n"
        "git -C \"$dir\" rev-parse HEAD > \"$BOSS_TEST_BACKUP_DIR/source-commit\"\n"
        "printf 'database fixture\\n' > \"$BOSS_TEST_BACKUP_DIR/boss.dump\"\n"
        "printf 'artifact fixture\\n' > \"$BOSS_TEST_BACKUP_DIR/artifacts.tar\"\n"
        "printf '%s\\n' \"$BOSS_TEST_BACKUP_DIR\"\n"
    )
    _git("add", ".", cwd=source)
    _git("commit", "-m", "old install", cwd=source)
    old = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "origin", "main", cwd=source)
    clone = _run("git", "clone", "--branch", "main", origin, installed)
    assert clone.returncode == 0, clone.stderr
    private_env = b"BOSS_API_KEY=private-test-fixture\nBOSS_DB_PASSWORD=existing\n"
    (installed / ".env").write_bytes(private_env)
    (source / "version").write_text("new\n")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "new install", cwd=source)
    new = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "origin", "main", cwd=source)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == info ]]; then exit 0; fi\n"
        "if [[ $1 == compose && $2 == version ]]; then echo 'Docker Compose test'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$BOSS_TEST_DOCKER_TRACE\"\n"
        "exit 86\n"
    )
    docker.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BOSS_TEST_BACKUP_TRACE": str(tmp_path / "backup-trace"),
        "BOSS_TEST_BACKUP_DIR": str(tmp_path / "recovery"),
        "BOSS_TEST_DOCKER_TRACE": str(tmp_path / "docker-trace"),
        "BOSS_REPO_REF": "main",
        "BOSS_REPO_URL": str(origin),
    }
    return installed, old, new, env, tmp_path


def _install(installed: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(
        "bash", REPO / "install.sh", "--dir", installed, "--yes",
        "--skip-model-key", "--no-install-deps", cwd=REPO, env=env,
    )


def test_pasted_installer_backs_up_before_switching_source(install_pair):
    installed, old, new, env, tmp = install_pair
    prior_env = (installed / ".env").read_bytes()

    result = _install(installed, env)

    # Fake Docker deliberately stops the installer at its first image build.
    assert result.returncode == 86, result.stderr + result.stdout
    assert (tmp / "backup-trace").read_text().strip() == old
    assert (tmp / "recovery/source-commit").read_text().strip() == old
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert b"BOSS_API_KEY=private-test-fixture\n" in (installed / ".env").read_bytes()
    assert b"BOSS_DB_PASSWORD=existing\n" in (installed / ".env").read_bytes()
    assert (tmp / "recovery/.env").read_bytes() == prior_env
    assert "private-test-fixture" not in result.stdout + result.stderr


def test_backup_failure_leaves_checkout_and_credentials_untouched(install_pair):
    installed, old, _, env, tmp = install_pair
    env["BOSS_TEST_BACKUP_FAIL"] = "1"
    prior_env = (installed / ".env").read_bytes()

    result = _install(installed, env)

    assert result.returncode != 0
    assert "Recovery backup failed" in result.stderr
    assert _git("rev-parse", "HEAD", cwd=installed) == old
    assert (installed / ".env").read_bytes() == prior_env
    assert not (tmp / "docker-trace").exists()


def test_upgrade_reuses_its_valid_backup_without_a_second_snapshot(install_pair):
    installed, old, new, env, tmp = install_pair
    prior_env = (installed / ".env").read_bytes()

    result = _run("bash", REPO / "scripts/install/upgrade.sh", "--dir", installed,
                  cwd=REPO, env=env)

    assert result.returncode != 0  # fake Docker stops the fetched installer at build
    assert (tmp / "backup-trace").read_text().splitlines() == [old]
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert b"BOSS_API_KEY=private-test-fixture\n" in (installed / ".env").read_bytes()
    assert b"BOSS_DB_PASSWORD=existing\n" in (installed / ".env").read_bytes()
    assert (tmp / "recovery/.env").read_bytes() == prior_env


def test_unmatched_prior_backup_cannot_skip_a_new_snapshot(install_pair):
    installed, old, _, env, tmp = install_pair
    backup = tmp / "claimed-backup"
    backup.mkdir()
    for filename in (".env", "boss.dump", "artifacts.tar"):
        (backup / filename).write_text("fixture\n")
    (backup / "source-commit").write_text("another-commit\n")
    env["BOSS_PREUPGRADE_BACKUP_DIR"] = str(backup)

    result = _install(installed, env)

    assert result.returncode != 0
    assert "does not match the installed commit" in result.stderr
    assert _git("rev-parse", "HEAD", cwd=installed) == old
    assert not (tmp / "backup-trace").exists()
    assert not (tmp / "docker-trace").exists()


def test_same_revision_rerun_does_not_require_a_running_database(install_pair):
    installed, _, new, env, tmp = install_pair
    _git("fetch", "origin", "main", cwd=installed)
    _git("merge", "--ff-only", "FETCH_HEAD", cwd=installed)

    result = _install(installed, env)

    assert result.returncode == 86, result.stderr + result.stdout
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert not (tmp / "backup-trace").exists()
