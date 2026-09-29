"""Lifecycle scripts must retain an existing install unless purge is explicit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNINSTALL = REPO / "scripts/install/uninstall.sh"
UPGRADE = REPO / "scripts/install/upgrade.sh"
INSTALLER = REPO / "install.sh"
REMOTE = REPO / "scripts/install/remote.sh"


def test_compose_forwards_only_operator_github_route(tmp_path: Path) -> None:
    if not shutil.which("docker"):
        return
    env_file = tmp_path / ".env"
    base = (
        "BOSS_API_KEY=fixture\nBOSS_WORKER_API_KEY=worker-fixture\n"
        "INFISICAL_ENCRYPTION_KEY=fixture\n"
        "INFISICAL_AUTH_SECRET=fixture\nBOSS_CONSOLE_PASSWORD=fixture\n"
        "BOSS_CONSOLE_SESSION_SECRET=fixture\n"
    )
    for extra, expected in (("", ""), ("BOSS_GITHUB_OWNER=operator\nBOSS_GITHUB_REPO=app\n", "operator")):
        env_file.write_text(base + extra)
        result = subprocess.run(
            ["docker", "compose", "-f", str(REPO / "scripts/install/community-compose.yml"),
             "--env-file", str(env_file), "config", "--format", "json"],
            text=True, capture_output=True, check=True,
        )
        services = json.loads(result.stdout)["services"]
        for name in ("api", "worker"):
            assert services[name]["environment"]["BOSS_GITHUB_OWNER"] == expected
            assert services[name]["environment"]["BOSS_GITHUB_REPO"] == ("app" if expected else "")


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
    assert all(command.endswith(" stop") or command.endswith(" stop\n") or " stop" in command
               for command in commands)
    assert all("down" not in command for command in commands)
    assert all("--volumes" not in command for command in commands)


def test_backup_and_uninstall_accept_command_scoped_passwordless_sudo(tmp_path: Path) -> None:
    install, env, _ = _fixture(tmp_path)
    (install / ".boss-shipped-commit").write_text("fixture-commit\n")
    (tmp_path / "bin" / "docker").write_text("#!/bin/sh\nexit 42\n")
    sudo_log = tmp_path / "sudo.log"
    sudo = tmp_path / "bin" / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$SUDO_CALL_LOG"\n'
        '[ "$1" = -n ] && [ "$2" = docker ] || exit 42\n'
        "shift 2\n"
        'case "$*" in\n'
        '  info) exit 0 ;;\n'
        '  *"pg_isready"*) exit 9 ;;\n'
        '  *config*) printf "%s\\n" \'{"services":{"api":{"image":"boss-community-api:local"}}}\'; exit 0 ;;\n'
        '  *"image inspect"*) exit 0 ;;\n'
        '  *ps*) printf "db\\n"; exit 0 ;;\n'
        '  *compose*) exit 0 ;;\n'
        'esac\n'
        'exit 42\n'
    )
    sudo.chmod(0o755)
    env.update(SUDO_CALL_LOG=str(sudo_log), BOSS_BACKUP_DIR=str(tmp_path / "backups"))

    uninstall = _run(UNINSTALL, install, env, "--yes")
    backup = _run(REPO / "scripts/install/backup.sh", install, env)

    assert uninstall.returncode == 0, uninstall.stderr
    assert "BOSS Community stopped" in uninstall.stdout
    assert backup.returncode != 0
    assert "not ready" in backup.stderr
    calls = sudo_log.read_text().splitlines()
    assert all(call.startswith("-n docker ") for call in calls)
    assert any(call.endswith(" stop") or " stop" in call for call in calls)
    assert all("down" not in call for call in calls)
    assert any("pg_isready -U boss -d boss" in call for call in calls)


def test_piped_uninstall_uses_explicit_install_dir(tmp_path: Path) -> None:
    install, env, log = _fixture(tmp_path)
    result = subprocess.run(
        ["bash", "-s", "--", "--dir", str(install), "--yes"],
        input=UNINSTALL.read_text(), env=env, text=True,
        capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert "BOSS Community stopped." in result.stdout
    assert " stop" in log.read_text()
    assert "down" not in log.read_text()
    assert (install / ".env").exists()


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
    (upstream / "install.sh").write_text("#!/bin/sh\necho 'Backup failed' >&2\nexit 7\n")
    subprocess.run(["git", "-C", str(upstream), "add", "install.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "new"],
        check=True,
    )

    result = _run(UPGRADE, install, os.environ.copy())
    assert result.returncode != 0
    assert "Backup failed" in result.stderr
    assert "Upgrade failed" in result.stderr
    assert subprocess.check_output(["git", "-C", str(install), "rev-parse", "HEAD"], text=True).strip() == old_sha
    assert (install / "install.sh").read_text() == "echo old\n"
    assert (install / ".env").read_text() == "BOSS_API_KEY=keep\n"


def test_upgrade_bootstraps_backup_helper_from_target_ref(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "scripts/install").mkdir(parents=True)
    (upstream / "scripts/install/community-compose.yml").write_text("name: proof\nservices: {}\n")
    (upstream / "install.sh").write_text("echo old\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "old without backup helper"],
        check=True,
    )
    install = tmp_path / "boss"
    subprocess.run(["git", "clone", "-q", str(upstream), str(install)], check=True)
    (install / ".env").write_text("BOSS_API_KEY=keep\n")
    assert not (install / "scripts/install/backup.sh").exists()
    (upstream / "scripts/install/backup.sh").write_text(
        "#!/bin/sh\n"
        'test -f "$2/.env" || exit 9\n'
        "cat >/dev/null\n"
        "echo /tmp/fetched-backup\n"
    )
    (upstream / "install.sh").write_text(
        "#!/bin/bash\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --dir ]; then install_dir="$2"; shift 2; continue; fi\n'
        "  shift\n"
        "done\n"
        'git -C "$install_dir" merge --ff-only FETCH_HEAD >/dev/null\n'
        'test -f "$install_dir/.env"\n'
        'touch "$install_dir/upgrade-ran"\n'
    )
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "new with backup helper"],
        check=True,
    )

    result = _run(UPGRADE, install, os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Upgrade complete:" in result.stdout
    assert (install / "upgrade-ran").exists()
    assert (install / ".env").read_text() == "BOSS_API_KEY=keep\n"
    piped = subprocess.run(
        ["bash", "-s", "--", "--dir", str(install)], input=UPGRADE.read_text(),
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert piped.returncode == 0, piped.stdout + piped.stderr
    assert "Upgrade complete:" in piped.stdout


def test_installer_rerun_preserves_existing_credentials(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "scripts/install").mkdir(parents=True)
    for filename in (
        "community-compose.yml", "restore_preserved_env.sh",
        "reconcile_api_credentials.py", "gen_infisical_keys.sh",
    ):
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
        "BOSS_GITHUB_OWNER=operator\nBOSS_GITHUB_REPO=operator-project\n"
        "BOSS_CUSTOM_OPERATOR_SETTING=retain\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        "  compose) if [ \"$2\" = version ]; then echo 'Docker Compose version v2'; else cat >/dev/null; fi; exit 0 ;;\n"
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
    previous_worker_key = None
    for _ in range(2):
        result = _run(
            INSTALLER, install, test_env,
            "--yes", "--skip-model-key", "--no-web", "--no-install-deps",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        lines = env_file.read_text().splitlines()
        worker_key = next(line.split("=", 1)[1] for line in lines
                          if line.startswith("BOSS_WORKER_API_KEY="))
        assert worker_key and worker_key != "existing-api"
        if previous_worker_key is not None:
            assert worker_key == previous_worker_key
        previous_worker_key = worker_key
        assert f"BOSS_API_KEY_PRINCIPALS=existing-api:ceo,{worker_key}:system" in lines
        for retained in (
            "BOSS_DB_PASSWORD=existing-db", "BOSS_API_KEY=existing-api",
            "BOSS_CONSOLE_PASSWORD=existing-console-password-1234567890",
            "BOSS_CONSOLE_SESSION_SECRET=existing-console-session-secret-1234567890",
            "BOSS_GITHUB_OWNER=operator", "BOSS_GITHUB_REPO=operator-project",
            "BOSS_CUSTOM_OPERATOR_SETTING=retain",
        ):
            assert retained in lines

    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(upstream), str(fresh)], check=True)
    result = _run(
        INSTALLER, fresh, test_env,
        "--yes", "--skip-model-key", "--no-web", "--no-install-deps",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    fresh_lines = (fresh / ".env").read_text().splitlines()
    fresh_owner = next(line.split("=", 1)[1] for line in fresh_lines
                       if line.startswith("BOSS_API_KEY="))
    fresh_worker = next(line.split("=", 1)[1] for line in fresh_lines
                        if line.startswith("BOSS_WORKER_API_KEY="))
    assert fresh_owner and fresh_worker and fresh_owner != fresh_worker
    assert f"BOSS_API_KEY_PRINCIPALS={fresh_owner}:ceo,{fresh_worker}:system" in fresh_lines
    assert "BOSS_GITHUB_OWNER=" in fresh_lines
    assert "BOSS_GITHUB_REPO=" in fresh_lines
    piped = subprocess.run(
        ["bash", "-s", "--", "--dir", str(fresh), "--yes", "--skip-model-key",
         "--no-web", "--no-install-deps"],
        input=INSTALLER.read_text(), env=test_env, text=True,
        capture_output=True, timeout=20, check=False,
    )
    assert piped.returncode == 0, piped.stdout + piped.stderr
    assert "BOSS Community is running." in piped.stdout


def test_remote_failed_build_retains_previous_source(tmp_path: Path) -> None:
    install = tmp_path / "remote-boss"
    (install / "scripts/install").mkdir(parents=True)
    (install / ".env").write_text("BOSS_API_KEY=remote-private\n")
    (install / ".boss-shipped-commit").write_text("old-commit\n")
    (install / "prior-source-marker").write_text("old source\n")
    (install / "scripts/install/backup.sh").write_text("#!/bin/sh\necho /tmp/private-backup\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/bin/sh\nwhile [ \"$#\" -gt 1 ]; do shift; done\nexec sh -c \"$1\"\n")
    ssh.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = info ]; then exit 0; fi\n'
        'case " $* " in *" build "*) exit 7 ;; esac\n'
        "exit 0\n"
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(REMOTE), "--host", "proof", "--dir", str(install),
         "update", "--ref", "HEAD"],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "previous source" in result.stderr
    previous = tmp_path / "remote-boss.prev"
    assert previous.joinpath("prior-source-marker").read_text() == "old source\n"
    assert previous.joinpath(".env").read_text() == "BOSS_API_KEY=remote-private\n"
    assert install.joinpath(".env").read_text() == "BOSS_API_KEY=remote-private\n"
    assert not list(install.glob(".env.worker-next.*"))


def test_remote_successful_update_migrates_legacy_worker_key(tmp_path: Path) -> None:
    install = tmp_path / "remote-boss"
    (install / "scripts/install").mkdir(parents=True)
    (install / ".env").write_text("BOSS_API_KEY=remote-private\n")
    (install / ".boss-shipped-commit").write_text("old-commit\n")
    (install / "scripts/install/backup.sh").write_text(
        "#!/bin/sh\necho /tmp/private-backup\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/bin/sh\nwhile [ \"$#\" -gt 1 ]; do shift; done\nexec sh -c \"$1\"\n")
    ssh.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(REMOTE), "--host", "proof", "--dir", str(install),
         "update", "--ref", "HEAD"],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in (install / ".env").read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    worker_key = values["BOSS_WORKER_API_KEY"]
    assert worker_key and worker_key != values["BOSS_API_KEY"]
    assert values["BOSS_API_KEY_PRINCIPALS"] == (
        f"remote-private:ceo,{worker_key}:system"
    )
    assert not list(install.glob(".env.worker-next.*"))
    assert not tmp_path.joinpath("remote-boss.prev").exists()


def test_connect_claude_without_a_token_fails_before_install(tmp_path: Path) -> None:
    log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nexit 0\n")
    docker.chmod(0o755)
    env = os.environ.copy()
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(INSTALLER), "--connect-claude", "--yes", "--dir", str(tmp_path / "boss")],
        env=env, text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode != 0
    assert "No Claude setup token was provided" in result.stderr
    assert "Nothing was stored" in result.stderr
    assert "test-claude-token" not in result.stdout + result.stderr
    assert not log.exists()
    assert not (tmp_path / "boss").exists()


def test_backup_output_excludes_cli_session_material(tmp_path: Path) -> None:
    token = "sk-ant-oat01-session-token-material-not-for-backup"
    codex = '{"tokens":{"access_token":"codex-session-secret-material-0123456789"}}'
    install = tmp_path / "boss"
    (install / "scripts/install").mkdir(parents=True)
    (install / "scripts/install/community-compose.yml").write_text(
        "name: boss-lifecycle-test\nservices: {}\n"
    )
    env_file = install / ".env"
    env_file.write_text(
        "BOSS_API_KEY=keep-api-key\n"
        "BOSS_DB_PASSWORD=keep-db\n"
        "BOSS_CONSOLE_SESSION_SECRET=keep-console-session\n"
        f"CLAUDE_CODE_OAUTH_TOKEN={token}\n"
        f"CODEX_AUTH_JSON={codex}\n"
    )
    (install / ".boss-shipped-commit").write_text("fixture-commit\n")
    log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['info']:\n"
        "    raise SystemExit(0)\n"
        "if 'image' in args and 'inspect' in args:\n"
        "    raise SystemExit(0)\n"
        "if 'config' in args:\n"
        "    print('{\"services\":{\"api\":{\"image\":\"boss-community-api:local\"}}}')\n"
        "    raise SystemExit(0)\n"
        "if 'ps' in args and '--status' in args:\n"
        "    print('db')\n"
        "    raise SystemExit(0)\n"
        "if 'ps' in args and '--all' in args:\n"
        "    print('db')\n"
        "    raise SystemExit(0)\n"
        "if 'pg_isready' in args:\n"
        "    raise SystemExit(0)\n"
        "if 'pg_dump' in args:\n"
        f"    sys.stdout.write('DUMP-HEADER\\n' + {token!r} + '\\n' + {codex!r} + '\\nDUMP-FOOTER\\n')\n"
        "    raise SystemExit(0)\n"
        "if 'psql' in args:\n"
        "    raise SystemExit(0)\n"
        "if 'tar' in args:\n"
        f"    sys.stdout.buffer.write(b'ART\\n' + {token!r}.encode() + b'\\n' + {codex!r}.encode())\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n"
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    backup_root = tmp_path / "backups"
    result = _run(REPO / "scripts/install/backup.sh", install, env, "--backup-dir", str(backup_root))
    assert result.returncode == 0, result.stderr
    backup = Path(result.stdout.strip())
    archived = b"".join(path.read_bytes() for path in backup.rglob("*") if path.is_file())
    assert token.encode() not in archived
    assert b"codex-session-secret-material-0123456789" not in archived
    copied = (backup / ".env").read_text()
    assert "BOSS_API_KEY=keep-api-key" in copied
    assert "BOSS_CONSOLE_SESSION_SECRET=keep-console-session" in copied
    assert "CLAUDE_CODE_OAUTH_TOKEN=" in copied.splitlines()
    assert "CODEX_AUTH_JSON=" in copied.splitlines()
    assert "DUMP-HEADER" in (backup / "boss.dump").read_text()
    assert b"ART\n" in (backup / "artifacts.tar").read_bytes()
    assert token in env_file.read_text()
    calls = log.read_text()
    assert "/var/lib/boss/artifacts" in calls
    assert "/home/boss/.claude" not in calls
    assert (backup / ".env").stat().st_mode & 0o077 == 0
