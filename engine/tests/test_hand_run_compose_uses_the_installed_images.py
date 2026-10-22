"""A docker compose command typed from the docs must start the images the installer built.

The installer exports BOSS_COMMUNITY_IMAGE_TAG (default: the compose project, boss-community)
and builds boss-community-*:boss-community. The compose file fell back to ":local" when the
variable was unset — and it is unset for every command the docs tell an operator to run by hand,
such as the "add a hostname to a running install" recreate. On a VM installed with v1.0-rc1 and
then upgraded, that recreate quietly put the month-old rc1 console back up (no sign-in page);
on a fresh install it fails for want of an image. Found 2026-09-30.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROLES = ("api", "worker", "mcp-tools", "egress", "web")
RECOVERY_BACKEND_ROLES = ("api", "worker", "mcp-tools", "egress")


def _installer_default_tag():
    installer = (ROOT / "install.sh").read_text()
    project = re.search(r'COMPOSE_PROJECT_NAME="\$\{COMPOSE_PROJECT_NAME:-([^}]+)\}"', installer).group(1)
    assert 'BOSS_COMMUNITY_IMAGE_TAG="${BOSS_COMMUNITY_IMAGE_TAG:-$COMPOSE_PROJECT_NAME}"' in installer
    return project


def _resolved_images(tmp_path, **overrides):
    """Use the real Compose parser, with no daemon, operator env, or credential files."""
    docker = shutil.which("docker")
    standalone = shutil.which("docker-compose")
    candidates = ([[docker, "compose"]] if docker else []) + ([[standalone]] if standalone else [])
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DOCKER_CONFIG": str(docker_config),
        "DOCKER_HOST": f"unix://{tmp_path / 'no-daemon.sock'}",
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "BOSS_DB_PASSWORD": "synthetic-compose-database-fixture",
        "BOSS_API_KEY": "synthetic-compose-api-fixture",
        "BOSS_WORKER_API_KEY": "synthetic-compose-worker-fixture",
        "BOSS_CONSOLE_PASSWORD": "synthetic-compose-console-fixture",
        "BOSS_CONSOLE_SESSION_SECRET": "synthetic-compose-session-fixture",
        "INFISICAL_ENCRYPTION_KEY": "synthetic-compose-encryption-fixture",
        "INFISICAL_AUTH_SECRET": "synthetic-compose-auth-fixture",
        **overrides,
    }
    for compose in candidates:
        version = subprocess.run(
            [*compose, "version", "--short"], env=env, cwd=tmp_path,
            capture_output=True, text=True, timeout=20,
        )
        if version.returncode == 0:
            break
    else:
        pytest.skip("Docker Compose CLI is required; no Docker daemon is needed")
    result = subprocess.run(
        [*compose, "--env-file", str(empty_env), "--profile", "web",
         "-f", str(ROOT / "scripts/install/community-compose.yml"),
         "config", "--format", "json"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    # Keep the resolved environment private even on failure; assertions receive only
    # the image/pull-policy projection, never the full Compose JSON or stderr.
    assert result.returncode == 0, f"Compose config failed with exit {result.returncode}"
    services = json.loads(result.stdout)["services"]
    return {
        role: {key: services[role].get(key) for key in ("image", "pull_policy")}
        for role in (*PRODUCT_ROLES, "db")
    }


@pytest.mark.parametrize("tag", [None, "explicit-install-fixture"])
def test_compose_uses_the_installed_tag_for_every_product_role(tmp_path, tag):
    default = _installer_default_tag()
    resolved = _resolved_images(tmp_path, **({"BOSS_COMMUNITY_IMAGE_TAG": tag} if tag else {}))
    for role in PRODUCT_ROLES:
        assert resolved[role]["image"] == f"boss-community-{role}:{tag or default}"
    for role in (*RECOVERY_BACKEND_ROLES, "db"):
        assert resolved[role]["pull_policy"] == "missing"
    assert resolved["db"]["image"] == "boss-community-db:pg16.12-vector0.8.1-age1.6.0"   # one database image, pinned by tag


def test_recovery_immutable_images_override_tags_and_never_pull(tmp_path):
    api_image = "sha256:" + "1" * 64
    db_image = "sha256:" + "2" * 64
    resolved = _resolved_images(
        tmp_path, BOSS_COMMUNITY_IMAGE_TAG="ignored-for-recovery",
        BOSS_RECOVERY_API_IMAGE_ID=api_image,
        BOSS_RECOVERY_DB_IMAGE_ID=db_image,
        BOSS_RECOVERY_PULL_POLICY="never",
    )
    for role in RECOVERY_BACKEND_ROLES:
        assert resolved[role] == {"image": api_image, "pull_policy": "never"}
    assert resolved["db"] == {"image": db_image, "pull_policy": "never"}
    # The web image is not one of recovery's backend/database overrides.
    assert resolved["web"]["image"] == "boss-community-web:ignored-for-recovery"
