"""Safe local checks for the Community install and remote model command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_checkup_treats_private_env_as_data(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOSS_API_PORT=49999\n"
        "BOSS_WEB_PORT=49998\n"
        "BOSS_DEFAULT_PROJECT_ID=boss-dogfood\n"
        f"BOSS_API_KEY=$(touch {marker})\n"
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install/checkup.sh")],
        env={**os.environ, "BOSS_ENV_FILE": str(env_file)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode in (0, 1)
    assert not marker.exists(), "a .env value must never be executed as shell code"
    assert "49999" in result.stdout


def _fake_ssh(tmp_path: Path, response: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload_path = tmp_path / "payload.json"
    fake = bin_dir / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'/v1/setup/model'*)\n"
        "    cat > \"$BOSS_TEST_PAYLOAD_PATH\"\n"
        f"    printf '%s\\n' '{response}'\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    fake.chmod(0o700)
    return bin_dir, payload_path


def test_remote_model_reads_key_from_stdin_and_reports_unverified(tmp_path: Path) -> None:
    bin_dir, payload_path = _fake_ssh(
        tmp_path, '{"ok": true, "provider": "custom", "verification_state": "unverified"}'
    )
    secret = "test-secret-should-not-appear-in-output"
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts/install/remote.sh"),
            "--host", "example.invalid", "model", "--provider", "custom",
            "--base-url", "http://model.internal/v1", "--model", "local-model",
        ],
        input=secret + "\n",
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "BOSS_TEST_PAYLOAD_PATH": str(payload_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(payload_path.read_text())
    assert payload["api_key"] == secret
    assert payload["base_url"] == "http://model.internal/v1"
    assert "not been verified" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_remote_model_error_with_provider_field_is_not_success(tmp_path: Path) -> None:
    bin_dir, payload_path = _fake_ssh(
        tmp_path, '{"detail": {"provider": "openrouter", "error": "bad key"}}'
    )
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts/install/remote.sh"),
            "--host", "example.invalid", "model", "--provider", "openrouter",
        ],
        input="test-secret\n",
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "BOSS_TEST_PAYLOAD_PATH": str(payload_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert payload_path.exists()
    assert "did not accept" in result.stdout
    assert "stored" not in result.stdout


def test_remote_subscription_requests_recording_without_unmetered_proof(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ssh"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, re, sys\n"
        "command = sys.argv[-1]\n"
        "if 'base64 -d' in command:\n"
        "    match = re.search(r\"printf %s '([^']+)'\", command)\n"
        "    script = base64.b64decode(match.group(1)).decode() if match else ''\n"
        "    if 'prove' not in script or 'false' not in script:\n"
        "        sys.exit(2)\n"
        "    print('STORE:{\"ok\":true}')\n"
        "    print('CONNECT:{\"ok\":true,\"connected\":true,"
        "\"entitlement_recorded\":true,\"proof\":{\"ok\":true,\"skipped\":true}}')\n"
    )
    fake.chmod(0o700)
    token = "test-token-should-not-appear-in-output"
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts/install/remote.sh"),
            "--host", "example.invalid", "subscription", "--provider", "claude_code",
            "--accept-database-fallback",
        ],
        input=token + "\n",
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "remains unverified" in result.stdout
    assert token not in result.stdout + result.stderr


def test_installer_rerun_keeps_headless_choice_and_credentials(tmp_path: Path) -> None:
    """Exercise the real installer with local Git and inert Docker/API commands."""
    checkout = tmp_path / "checkout"
    helpers = checkout / "scripts/install"
    helpers.mkdir(parents=True)
    for name in ("restore_preserved_env.sh", "reconcile_api_credentials.py"):
        shutil.copy2(ROOT / "scripts/install" / name, helpers / name)
    (helpers / "community-compose.yml").write_text("name: boss-community\n")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=Installer Test",
         "-c", "user.email=installer@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", str(checkout)],
        check=True,
    )
    env_file = checkout / ".env"
    env_file.write_text(
        "BOSS_API_KEY=kept-owner\nBOSS_WORKER_API_KEY=kept-worker\n"
        "BOSS_API_KEYS=kept-owner,kept-worker\n"
        "BOSS_API_KEY_PRINCIPALS=kept-owner:ceo,kept-worker:system\n"
        "BOSS_DB_PASSWORD=kept-db\n"
        "BOSS_CONSOLE_PASSWORD=kept-console\n"
        "BOSS_CONSOLE_SESSION_SECRET=kept-session\n"
        "BOSS_API_PORT=49999\nBOSS_WEB_PORT=49998\nBOSS_POSTGRES_PORT=49997\n"
        "BOSS_COMMUNITY_WEB_ENABLED=0\n"
        "INFISICAL_ENCRYPTION_KEY=kept-encryption\n"
        "INFISICAL_AUTH_SECRET=kept-auth-secret\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in *' version '*) echo 'Docker Compose test'; exit 0 ;; esac\n"
        "printf '%s\\n' \"$*\" >> \"$BOSS_TEST_DOCKER_CALLS\"\n"
        "exit 0\n"
    )
    docker.chmod(0o700)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args=sys.argv[1:]\n"
        "if '/v1/edition' in ' '.join(args):\n"
        "    out=args[args.index('-o')+1]\n"
        "    open(out,'w').write('{\"edition\":\"community\"}')\n"
        "    print('200',end='')\n"
        "elif '/v1/providers' in ' '.join(args):\n"
        "    print('200',end='')\n"
    )
    curl.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "BOSS_TEST_DOCKER_CALLS": str(docker_calls),
    }
    for extra, expected in (([], "0"), (["--web"], "1")):
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--dir", str(checkout),
             "--yes", "--skip-model-key", "--ref", "HEAD", *extra],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(
            line.split("=", 1) for line in env_file.read_text().splitlines()
            if "=" in line and not line.startswith("#")
        )
        assert values["BOSS_COMMUNITY_WEB_ENABLED"] == expected
        assert values["BOSS_API_KEY"] == "kept-owner"
        assert values["BOSS_DB_PASSWORD"] == "kept-db"
        assert values["BOSS_CONSOLE_PASSWORD"] == "kept-console"
        calls = docker_calls.read_text().splitlines()
        if expected == "0":
            assert any("--profile web stop web" in call for call in calls)
            assert not any("--profile web" in call and " build" in call for call in calls)
        else:
            assert any("--profile web" in call and " build" in call for call in calls)

    failed_store = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--dir", str(checkout),
         "--yes", "--skip-model-key", "--ref", "HEAD", "--secret-store", "infisical"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert failed_store.returncode != 0
    assert "selected secret store cannot be provisioned" in failed_store.stderr
    assert "BOSS Community is running" not in failed_store.stdout
    assert "BOSS_COMMUNITY_SECRET_STORE=infisical" in env_file.read_text()

    rerun = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--dir", str(checkout),
         "--yes", "--skip-model-key", "--ref", "HEAD"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rerun.returncode != 0
    assert "selected secret store cannot be provisioned" in rerun.stderr
